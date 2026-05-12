"""Vertex AI activities: submit a CustomJob and poll it to completion.

Implements Steps 3 and 4 of the FineTuneWorkflow (§3.4 / §4 of ARCHITECTURE.md).

Key invariants:
- machine_spec is built CONDITIONALLY on use_gpu (§4.1).
- Spot scheduling is only attempted when use_gpu=true (§4.3 / §4.4).
- Step 3 deduplicates by display_name to survive Temporal retries (§3.4 Step 3).
- Step 4 heartbeats every 30 s; heartbeat_timeout is set by the workflow call-site.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Optional

from google.api_core import exceptions as gapi_exc
from google.cloud import aiplatform, storage
from temporalio import activity

from temporal.workflows.shared import (
    AmbiguousVertexJob,
    FineTuneRequest,
    MonitorResult,
    QuotaExceeded,
    SpotUnavailable,
    SubmitResult,
    VertexJobFailed,
)

logger = logging.getLogger(__name__)

# Vertex AI job states
_TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_PAUSED",
    "JOB_STATE_EXPIRED",
}
_RUNNING_STATES = {
    "JOB_STATE_QUEUED",
    "JOB_STATE_PENDING",
    "JOB_STATE_RUNNING",
    "JOB_STATE_CANCELLING",
}

# How often to poll and emit a heartbeat during monitor_training
_POLL_INTERVAL_SECONDS = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_machine_spec(req: FineTuneRequest) -> dict:
    """Return the machine_spec dict for worker_pool_specs.

    CPU-only (use_gpu=false): no accelerator fields at all (§4.1.a).
    GPU (use_gpu=true): include accelerator_type and accelerator_count (§4.1.b).
    """
    if not req.infrastructure.use_gpu:
        return {
            "machine_type": req.infrastructure.machine_type or "n1-standard-8",
        }
    return {
        "machine_type": req.infrastructure.machine_type or "a2-highgpu-1g",
        "accelerator_type": req.infrastructure.accelerator_type or "NVIDIA_TESLA_A100",
        "accelerator_count": req.infrastructure.accelerator_count or 1,
    }


def _scheduling_strategy(use_spot: bool) -> str:
    return "SPOT" if use_spot else "STANDARD"


def _find_active_job(
    aiplatform_client: aiplatform.gapic.JobServiceClient,
    project: str,
    location: str,
    display_name: str,
) -> Optional[str]:
    """Return vertex_job_id if exactly one active job matches display_name.

    Raises AmbiguousVertexJob if >1 matches exist (operator must clean up).
    Returns None if no active job is found.
    """
    parent = f"projects/{project}/locations/{location}"
    filter_str = (
        f'display_name="{display_name}" AND '
        f"state=JOB_STATE_QUEUED OR state=JOB_STATE_PENDING OR state=JOB_STATE_RUNNING"
    )
    jobs = list(
        aiplatform_client.list_custom_jobs(parent=parent, filter=filter_str)
    )
    active = [j for j in jobs if j.state.name in _RUNNING_STATES]
    if len(active) == 0:
        return None
    if len(active) > 1:
        raise AmbiguousVertexJob(
            f"Found {len(active)} active Vertex jobs with display_name='{display_name}'. "
            "Operator must cancel duplicates before retrying."
        )
    return active[0].name


def _submit_job(
    project: str,
    location: str,
    display_name: str,
    req: FineTuneRequest,
    image_uri: str,
    use_spot: bool,
    tensorboard_resource_name: Optional[str] = None,
) -> str:
    """Create a Vertex AI CustomJob and return its resource name."""
    machine_spec = _build_machine_spec(req)
    worker_pool_spec = {
        "machine_spec": machine_spec,
        "replica_count": 1,
        "disk_spec": {"boot_disk_type": "pd-ssd", "boot_disk_size_gb": 500},
        "container_spec": {
            "image_uri": image_uri,
            "command": ["python", "-m", "training.train"],
            "args": [
                f"+job.config_uri={req.config_uri}",
                f"+job.checkpoint_uri={req.artifacts.checkpoint_uri}",
                f"+job.output_uri={req.artifacts.output_uri}",
                f"+job.job_name={req.job_name}",
            ],
            "env": [
                {"name": "HF_TOKEN", "value": "$$HF_TOKEN"},
                {"name": "WANDB_API_KEY", "value": "$$WANDB_API_KEY"},
                {"name": "TRANSFORMERS_CACHE", "value": "/gcs-fuse/model-cache"},
            ],
        },
    }

    job_spec: dict = {
        "worker_pool_specs": [worker_pool_spec],
        "scheduling": {
            "strategy": _scheduling_strategy(use_spot),
            "timeout": "82800s",  # 23 h
            "restart_job_on_worker_restart": True,
        },
        "base_output_directory": {
            "output_uri_prefix": req.artifacts.checkpoint_uri,
        },
        "enable_web_access": False,
        "labels": {
            "peft_type": req.peft.type.value,
            "base_model": req.model.base_model_id.replace("/", "-").lower()[:63],
        },
    }
    if tensorboard_resource_name:
        job_spec["tensorboard"] = tensorboard_resource_name

    custom_job = aiplatform.CustomJob(
        display_name=display_name,
        worker_pool_specs=job_spec["worker_pool_specs"],
        project=project,
        location=location,
    )
    custom_job._gca_resource.job_spec.update(job_spec)  # type: ignore[attr-defined]
    custom_job.submit()
    return custom_job.resource_name


# ---------------------------------------------------------------------------
# Activity: submit_training_job (Step 3)
# ---------------------------------------------------------------------------


@activity.defn
async def submit_training_job(
    req: FineTuneRequest,
    image_uri: str,
    job_display_name: str,
    project: str,
    location: str,
    tensorboard_resource_name: Optional[str] = None,
) -> SubmitResult:
    """Submit a Vertex AI CustomJob, with Spot-to-STANDARD fallback (§4.3).

    Spot is only attempted when use_gpu=true.  CPU jobs always use STANDARD.
    On retry the activity first checks for an existing active job by display_name
    to avoid duplicate submissions.
    """
    use_gpu = req.infrastructure.use_gpu
    use_spot = use_gpu and req.infrastructure.spot

    # --- Idempotency: look for an already-submitted job before creating a new one ---
    try:
        from google.cloud.aiplatform_v1 import JobServiceClient
        from google.cloud.aiplatform_v1.services.job_service import transports

        client_options = {"api_endpoint": f"{location}-aiplatform.googleapis.com"}
        client = JobServiceClient(client_options=client_options)  # type: ignore[arg-type]
        existing_id = _find_active_job(client, project, location, job_display_name)
        if existing_id:
            logger.info(
                "Dedup: found existing active job '%s' — skipping re-submit.",
                existing_id,
            )
            strategy = "SPOT" if use_spot else "STANDARD"
            return SubmitResult(vertex_job_id=existing_id, scheduling_strategy=strategy)
    except (AmbiguousVertexJob, Exception) as exc:
        if isinstance(exc, AmbiguousVertexJob):
            raise
        logger.warning("Dedup listing failed (non-fatal): %s", exc)

    # --- Submit ---
    strategy = "SPOT" if use_spot else "STANDARD"
    try:
        vertex_job_id = _submit_job(
            project=project,
            location=location,
            display_name=job_display_name,
            req=req,
            image_uri=image_uri,
            use_spot=use_spot,
            tensorboard_resource_name=tensorboard_resource_name,
        )
        logger.info(
            "Submitted Vertex job '%s' (strategy=%s)", vertex_job_id, strategy
        )
        return SubmitResult(vertex_job_id=vertex_job_id, scheduling_strategy=strategy)

    except gapi_exc.ResourceExhausted as exc:
        # Spot capacity unavailable — attempt On-Demand fallback if configured
        if use_spot and req.infrastructure.fallback_on_demand:
            logger.warning(
                "Spot ResourceExhausted; falling back to STANDARD. Original: %s", exc
            )
            vertex_job_id = _submit_job(
                project=project,
                location=location,
                display_name=job_display_name,
                req=req,
                image_uri=image_uri,
                use_spot=False,
                tensorboard_resource_name=tensorboard_resource_name,
            )
            logger.info(
                "Fallback job submitted: '%s' (strategy=STANDARD)", vertex_job_id
            )
            return SubmitResult(
                vertex_job_id=vertex_job_id, scheduling_strategy="STANDARD"
            )
        raise SpotUnavailable(
            f"Spot capacity unavailable and fallback_on_demand=false: {exc}"
        ) from exc

    except gapi_exc.PermissionDenied as exc:
        raise PermissionError(str(exc)) from exc

    except gapi_exc.BadRequest as exc:
        msg = str(exc)
        if "quota" in msg.lower():
            raise QuotaExceeded(f"GPU quota exceeded: {exc}") from exc
        raise ValueError(f"InvalidArgument from Vertex AI: {exc}") from exc


# ---------------------------------------------------------------------------
# Activity: monitor_training_job (Step 4)
# ---------------------------------------------------------------------------


@activity.defn
async def monitor_training_job(
    vertex_job_id: str,
    checkpoint_uri: str,
) -> MonitorResult:
    """Poll a Vertex AI CustomJob until it reaches a terminal state.

    Emits a Temporal heartbeat every 30 s containing the current job state,
    step counter (from the GCS _progress.json sentinel), and last loss values.

    heartbeat_timeout on the call-site must be set to 2 min.
    start_to_close_timeout must be set to at least 24 h (§3.4 Step 4).
    """
    # Parse project and location from the resource name
    # Format: projects/{project}/locations/{location}/customJobs/{id}
    parts = vertex_job_id.split("/")
    project = parts[1]
    location = parts[3]

    aiplatform.init(project=project, location=location)
    storage_client = storage.Client(project=project)

    preemption_count = 0
    start_time = time.monotonic()
    current_state = "JOB_STATE_PENDING"
    last_step = 0
    last_train_loss: Optional[float] = None
    last_eval_loss: Optional[float] = None
    eval_score: Optional[float] = None

    logger.info("Monitoring Vertex job '%s'", vertex_job_id)

    while True:
        # Poll job state
        try:
            job = aiplatform.CustomJob.get(vertex_job_id)
            current_state = job.state.name
        except Exception as exc:
            logger.warning("Failed to get job state (will retry): %s", exc)
            activity.heartbeat(
                {
                    "vertex_job_id": vertex_job_id,
                    "state": current_state,
                    "step": last_step,
                    "error": str(exc),
                }
            )
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue

        # Read progress sentinel from GCS (best-effort)
        try:
            progress = _read_progress_json(storage_client, checkpoint_uri)
            if progress:
                last_step = progress.get("step", last_step)
                last_train_loss = progress.get("loss", last_train_loss)
        except Exception as exc:
            logger.debug("Could not read _progress.json: %s", exc)

        # Read eval_score.json if available (best-effort)
        try:
            es = _read_eval_score_json(storage_client, checkpoint_uri)
            if es is not None:
                eval_score = es
        except Exception:
            pass

        # Emit heartbeat
        activity.heartbeat(
            {
                "vertex_job_id": vertex_job_id,
                "state": current_state,
                "step": last_step,
                "train_loss": last_train_loss,
                "eval_loss": last_eval_loss,
                "eval_score": eval_score,
            }
        )

        logger.info(
            "Job '%s': state=%s step=%d loss=%s",
            vertex_job_id,
            current_state,
            last_step,
            last_train_loss,
        )

        if current_state in _TERMINAL_STATES:
            break

        time.sleep(_POLL_INTERVAL_SECONDS)

    wall_clock_seconds = int(time.monotonic() - start_time)

    if current_state == "JOB_STATE_SUCCEEDED":
        return MonitorResult(
            final_state=current_state,
            final_step=last_step,
            train_loss=last_train_loss,
            eval_loss=last_eval_loss,
            eval_score=eval_score,
            preemption_count=preemption_count,
            preempted=False,
            wall_clock_seconds=wall_clock_seconds,
        )

    # Check for preemption
    error_code = ""
    try:
        error_code = job.error.code if job.error else ""  # type: ignore[union-attr]
    except Exception:
        pass

    is_preempted = (
        current_state == "JOB_STATE_FAILED"
        and "PREEMPTED" in str(error_code).upper()
    )

    if is_preempted:
        preemption_count += 1
        logger.info(
            "Job '%s' preempted (count=%d)", vertex_job_id, preemption_count
        )
        return MonitorResult(
            final_state=current_state,
            final_step=last_step,
            train_loss=last_train_loss,
            eval_loss=last_eval_loss,
            preemption_count=preemption_count,
            preempted=True,
            wall_clock_seconds=wall_clock_seconds,
        )

    # Any other failure state
    error_msg = getattr(getattr(job, "error", None), "message", current_state)
    raise VertexJobFailed(
        f"CustomJob '{vertex_job_id}' exited with state {current_state}: {error_msg}"
    )


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Return (bucket, prefix) from a gs:// URI."""
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


def _read_progress_json(
    storage_client: storage.Client, checkpoint_uri: str
) -> Optional[dict]:
    """Read _progress.json written by GcsCheckpointCallback."""
    bucket_name, prefix = _parse_gcs_uri(checkpoint_uri)
    blob = storage_client.bucket(bucket_name).blob(f"{prefix}/_progress.json")
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def _read_eval_score_json(
    storage_client: storage.Client, output_uri: str
) -> Optional[float]:
    """Return eval_score from the most recent eval_score.json, or None."""
    bucket_name, prefix = _parse_gcs_uri(output_uri)
    blob = storage_client.bucket(bucket_name).blob(f"{prefix}/eval_score.json")
    if not blob.exists():
        return None
    data = json.loads(blob.download_as_text())
    return data.get("eval_score")
