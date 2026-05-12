"""FineTuneWorkflow — 9-step supervised fine-tuning orchestration.

Implements §3 of ARCHITECTURE.md (workflow contracts, retry policies,
conditional logic, query/signal handlers).

Step execution order:
  1. data_validation_and_prep
  2. build_training_image_if_needed  (conditional: if image_uri not pinned)
  3. submit_training  (Spot + On-Demand fallback; preemption retry loop)
  4. monitor_training  (polling + heartbeats)
  5. save_artifacts
  6. register_model  (conditional: register_in_vertex)
  7. upload_to_hf_hub  (conditional: upload_hf_hub)
  8. prepare_serving_artifacts  (conditional: prepare_serving_image)
  9. notify  (best-effort, swallows errors)
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    # Heavy imports are allowed here because they execute outside the sandbox
    from temporal.activities.cloud_build_activity import (
        build_training_image,
        prepare_serving_artifacts,
    )
    from temporal.activities.data_prep_activity import validate_and_preprocess
    from temporal.activities.hf_hub_activity import upload_to_hf_hub
    from temporal.activities.model_registry_activity import register_model
    from temporal.activities.notification_activity import send_notification
    from temporal.activities.vertex_ai_activity import (
        monitor_training_job,
        submit_training_job,
    )
    from temporal.policies import (
        ARTIFACT_RETRY,
        BUILD_RETRY,
        DATA_PREP_RETRY,
        DEFAULT_RETRY,
        HF_RETRY,
        LONG_POLL_RETRY,
        NOTIFY_RETRY,
    )
    from temporal.workflows.shared import (
        ArtifactConfig,
        BuildResult,
        DataPrepResult,
        FineTuneRequest,
        MonitorResult,
        NotificationConfig,
        RegisterResult,
        SaveResult,
        ServingResult,
        VertexJobFailed,
        WorkflowProgress,
        WorkflowResult,
    )

logger = logging.getLogger(__name__)


@workflow.defn
class FineTuneWorkflow:
    """Durable 9-step SFT workflow with heartbeats, preemption retries,
    eval-score gating, and manual cancellation signal.

    Workflow ID format: ft-{config_hash[:8]}-{yyyymmdd-hhmmss}
    Task queue:         vertex-finetune-{environment}
    Execution timeout:  24 h (default, configurable)
    Run timeout:        23 h
    """

    def __init__(self) -> None:
        # Mutable state visible to query handlers
        self._current_step: str = "initialising"
        self._vertex_state: str = ""
        self._step_count: int = 0
        self._preemptions: int = 0
        self._cancelled: bool = False
        self._cancel_reason: str = ""
        self._vertex_job_id: Optional[str] = None

    # -----------------------------------------------------------------------
    # Query handler — §3.5
    # -----------------------------------------------------------------------

    @workflow.query
    def get_progress(self) -> WorkflowProgress:
        return WorkflowProgress(
            current_step=self._current_step,
            vertex_state=self._vertex_state,
            step_count=self._step_count,
            preemptions=self._preemptions,
        )

    # -----------------------------------------------------------------------
    # Signal handler — manual cancellation §3.5
    # -----------------------------------------------------------------------

    @workflow.signal
    def cancel_with_reason(self, reason: str) -> None:
        logger.info("Received cancel_with_reason signal: %s", reason)
        self._cancelled = True
        self._cancel_reason = reason

    # -----------------------------------------------------------------------
    # Main workflow execution
    # -----------------------------------------------------------------------

    @workflow.run
    async def run(self, req: FineTuneRequest) -> WorkflowResult:
        started_at = workflow.now().isoformat()
        self._current_step = "started"

        project = _project_from_uri(req.artifacts.output_uri)
        location = req.infrastructure.region
        environment = _environment_from_uri(req.artifacts.output_uri)
        tensorboard_resource_name: Optional[str] = None  # injected via config if set

        result = WorkflowResult(
            status="succeeded",
            workflow_id=workflow.info().workflow_id,
            started_at=started_at,
        )

        try:
            # -------------------------------------------------------------------
            # Step 1 — data validation and preprocessing
            # -------------------------------------------------------------------
            self._current_step = "data_validation_and_prep"
            data_result: DataPrepResult = await workflow.execute_activity(
                validate_and_preprocess,
                args=[req.data, req.job_name, project],
                start_to_close_timeout=timedelta(hours=4),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=DATA_PREP_RETRY,
            )
            logger.info(
                "Step 1 done: %d train rows, %d val rows",
                data_result.train_rows,
                data_result.val_rows,
            )

            # Check cancellation after each long step
            if self._cancelled:
                return await self._handle_cancel(req, result, project)

            # -------------------------------------------------------------------
            # Step 2 — build training image (conditional)
            # -------------------------------------------------------------------
            self._current_step = "build_training_image"
            image_uri = req.image_uri or ""
            if not image_uri:
                build_result: BuildResult = await workflow.execute_activity(
                    build_training_image,
                    args=[project, environment, workflow.info().workflow_id],
                    start_to_close_timeout=timedelta(hours=2),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=BUILD_RETRY,
                )
                image_uri = build_result.image_uri
                logger.info("Step 2 done: image_uri=%s", image_uri)
            else:
                logger.info("Step 2 skipped: image_uri pre-supplied '%s'", image_uri)

            if self._cancelled:
                return await self._handle_cancel(req, result, project)

            # -------------------------------------------------------------------
            # Step 3 + 4 — submit and monitor (with preemption retry loop)
            # -------------------------------------------------------------------
            effective_lr = req.training.learning_rate
            retry_round = 0
            monitor_result: Optional[MonitorResult] = None

            submit_and_monitor_loop = True
            while submit_and_monitor_loop:
                # Build a (possibly mutated) copy of infrastructure config for
                # On-Demand fallback tracking.
                job_display_name = (
                    f"ft-{req.job_name}-"
                    f"{workflow.now().strftime('%Y%m%d-%H%M%S')}"
                )
                if self._preemptions > 0:
                    job_display_name += f"-retry{self._preemptions}"

                self._current_step = "submit_training"
                submit_result = await workflow.execute_activity(
                    submit_training_job,
                    args=[req, image_uri, job_display_name, project, location, tensorboard_resource_name],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=5),
                        backoff_coefficient=2.0,
                        maximum_interval=timedelta(minutes=5),
                        maximum_attempts=req.infrastructure.max_preemptions + 1,
                        non_retryable_error_types=[
                            "SpotUnavailable",
                            "QuotaExceeded",
                            "AmbiguousVertexJob",
                            "PermissionError",
                            "ValueError",
                        ],
                    ),
                )
                self._vertex_job_id = submit_result.vertex_job_id
                result.vertex_job_id = submit_result.vertex_job_id
                if submit_result.scheduling_strategy == "STANDARD" and req.infrastructure.use_gpu:
                    result.fallback_to_on_demand = True

                logger.info(
                    "Step 3 done: vertex_job_id=%s strategy=%s",
                    submit_result.vertex_job_id,
                    submit_result.scheduling_strategy,
                )

                if self._cancelled:
                    return await self._handle_cancel(req, result, project)

                # --- Step 4 — monitor ---
                self._current_step = "monitor_training"
                try:
                    monitor_result = await workflow.execute_activity(
                        monitor_training_job,
                        args=[submit_result.vertex_job_id, req.artifacts.checkpoint_uri],
                        start_to_close_timeout=timedelta(hours=24),
                        heartbeat_timeout=timedelta(minutes=2),
                        retry_policy=LONG_POLL_RETRY,
                    )
                except VertexJobFailed as exc:
                    raise

                self._step_count = monitor_result.final_step
                self._vertex_state = monitor_result.final_state
                result.preemption_count = monitor_result.preemption_count
                self._preemptions += monitor_result.preemption_count

                logger.info(
                    "Step 4 done: state=%s step=%d preemptions=%d",
                    monitor_result.final_state,
                    monitor_result.final_step,
                    monitor_result.preemption_count,
                )

                if self._cancelled:
                    return await self._handle_cancel(req, result, project)

                # --- Preemption retry decision ---
                if monitor_result.preempted:
                    cumulative_preemptions = self._preemptions
                    max_p = req.infrastructure.max_preemptions
                    if cumulative_preemptions < max_p:
                        logger.info(
                            "Preempted (%d/%d) — re-submitting SPOT",
                            cumulative_preemptions,
                            max_p,
                        )
                        continue  # loop back to Step 3 with Spot
                    elif req.infrastructure.fallback_on_demand:
                        logger.info(
                            "Preemption cap reached — switching to On-Demand"
                        )
                        # Mutate infra config to force STANDARD on next submit
                        req = req.model_copy(
                            update={
                                "infrastructure": req.infrastructure.model_copy(
                                    update={"spot": False}
                                )
                            }
                        )
                        result.fallback_to_on_demand = True
                        continue
                    else:
                        raise VertexJobFailed(
                            f"Preemption cap ({max_p}) reached and "
                            "fallback_on_demand=false."
                        )

                # Not preempted — exit loop
                submit_and_monitor_loop = False

            assert monitor_result is not None

            # -------------------------------------------------------------------
            # Low eval-score retry (§3.5)
            # -------------------------------------------------------------------
            eval_score = monitor_result.eval_score
            if (
                eval_score is not None
                and req.evaluation.retry_on_low_score
                and req.evaluation.min_eval_score is not None
                and eval_score < req.evaluation.min_eval_score
                and retry_round < 1
            ):
                retry_round += 1
                new_lr = req.training.learning_rate * req.evaluation.retry_lr_multiplier
                logger.warning(
                    "Eval score %.4f < threshold %.4f — retrying with lr=%s",
                    eval_score,
                    req.evaluation.min_eval_score,
                    new_lr,
                )
                req = req.model_copy(
                    update={
                        "training": req.training.model_copy(
                            update={"learning_rate": new_lr}
                        )
                    }
                )
                # Re-enter the submit+monitor loop
                submit_and_monitor_loop = True
                while submit_and_monitor_loop:
                    job_display_name_retry = (
                        f"ft-{req.job_name}-retry-{workflow.now().strftime('%Y%m%d-%H%M%S')}"
                    )
                    submit_result = await workflow.execute_activity(
                        submit_training_job,
                        args=[req, image_uri, job_display_name_retry, project, location, tensorboard_resource_name],
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=DEFAULT_RETRY,
                    )
                    self._vertex_job_id = submit_result.vertex_job_id
                    result.vertex_job_id = submit_result.vertex_job_id

                    monitor_result = await workflow.execute_activity(
                        monitor_training_job,
                        args=[submit_result.vertex_job_id, req.artifacts.checkpoint_uri],
                        start_to_close_timeout=timedelta(hours=24),
                        heartbeat_timeout=timedelta(minutes=2),
                        retry_policy=LONG_POLL_RETRY,
                    )
                    self._step_count = monitor_result.final_step
                    self._vertex_state = monitor_result.final_state
                    submit_and_monitor_loop = False

            # -------------------------------------------------------------------
            # Step 5 — save artifacts
            # -------------------------------------------------------------------
            self._current_step = "save_artifacts"
            save_result: SaveResult = await workflow.execute_activity(
                _save_artifacts_activity,
                args=[req.artifacts, monitor_result.final_step, project],
                start_to_close_timeout=timedelta(hours=2),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=ARTIFACT_RETRY,
            )
            logger.info(
                "Step 5 done: adapter_uri=%s", save_result.adapter_uri
            )

            # -------------------------------------------------------------------
            # Step 6 — register model (conditional)
            # -------------------------------------------------------------------
            register_result: Optional[RegisterResult] = None
            if req.artifacts.register_in_vertex:
                self._current_step = "register_model"
                register_result = await workflow.execute_activity(
                    register_model,
                    args=[save_result, req],
                    start_to_close_timeout=timedelta(minutes=30),
                    retry_policy=DEFAULT_RETRY,
                )
                logger.info(
                    "Step 6 done: model_resource=%s version=%s",
                    register_result.model_resource_name,
                    register_result.version_id,
                )

            # -------------------------------------------------------------------
            # Step 7 — upload to HF Hub (conditional)
            # -------------------------------------------------------------------
            hf_result = None
            if req.artifacts.upload_hf_hub:
                self._current_step = "upload_to_hf_hub"
                hf_result = await workflow.execute_activity(
                    upload_to_hf_hub,
                    args=[save_result, req],
                    start_to_close_timeout=timedelta(hours=2),
                    retry_policy=HF_RETRY,
                )
                logger.info("Step 7 done: repo_url=%s", hf_result.repo_url)

            # -------------------------------------------------------------------
            # Step 8 — prepare serving artifacts (conditional)
            # -------------------------------------------------------------------
            serving_result: Optional[ServingResult] = None
            if req.artifacts.prepare_serving_image:
                self._current_step = "prepare_serving_artifacts"
                version_id = register_result.version_id if register_result else "1"
                serving_result = await workflow.execute_activity(
                    prepare_serving_artifacts,
                    args=[save_result.adapter_uri, req.model.base_model_id, version_id, project, environment],
                    start_to_close_timeout=timedelta(hours=2),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=BUILD_RETRY,
                )
                logger.info(
                    "Step 8 done: serving_image=%s", serving_result.image_uri
                )

            # -------------------------------------------------------------------
            # Assemble final result
            # -------------------------------------------------------------------
            completed_at = workflow.now().isoformat()
            result.status = "succeeded"
            result.completed_at = completed_at
            result.model = {
                "base_model_id": req.model.base_model_id,
                "adapter_uri": save_result.adapter_uri,
                "merged_uri": save_result.merged_uri,
                "vertex_model_resource": (
                    register_result.model_resource_name if register_result else None
                ),
                "hf_repo_id": (hf_result.repo_url if hf_result else None),
            }
            result.metrics = {
                "train_loss_final": monitor_result.train_loss,
                "eval_loss_final": monitor_result.eval_loss,
                "eval_score": monitor_result.eval_score,
                "steps_completed": monitor_result.final_step,
                "wall_clock_seconds": monitor_result.wall_clock_seconds,
            }
            if serving_result:
                result.serving_image = serving_result.image_uri

        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.failure_step = self._current_step
            result.error_type = type(exc).__name__
            result.message = str(exc)
            result.completed_at = workflow.now().isoformat()
            logger.exception(
                "Workflow '%s' failed at step '%s': %s",
                workflow.info().workflow_id,
                self._current_step,
                exc,
            )

        # -------------------------------------------------------------------
        # Step 9 — notify (best-effort, always runs)
        # -------------------------------------------------------------------
        self._current_step = "notify"
        await self._send_notification(result, req.notifications)

        return result

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    async def _send_notification(
        self,
        result: WorkflowResult,
        notifications: NotificationConfig,
    ) -> None:
        """Run Step 9 and swallow any remaining errors (§3.4 Step 9)."""
        try:
            await workflow.execute_activity(
                send_notification,
                args=[result, notifications],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=NOTIFY_RETRY,
            )
        except Exception as exc:
            # Notification failure must NOT fail the workflow
            logger.warning(
                "Step 9 notification failed (swallowed): %s", exc
            )

    async def _handle_cancel(
        self,
        req: FineTuneRequest,
        result: WorkflowResult,
        project: str,
    ) -> WorkflowResult:
        """Handle a cancel_with_reason signal — cancel the Vertex job and notify."""
        logger.info(
            "Cancelling workflow '%s': %s",
            workflow.info().workflow_id,
            self._cancel_reason,
        )
        # Attempt to cancel the in-flight Vertex job
        if self._vertex_job_id:
            try:
                from google.cloud import aiplatform as _aip  # type: ignore

                with workflow.unsafe.sandbox_unrestricted():
                    job = _aip.CustomJob.get(self._vertex_job_id)
                    job.cancel()
            except Exception as exc:
                logger.warning("Could not cancel Vertex job: %s", exc)

        result.status = "cancelled"
        result.completed_at = workflow.now().isoformat()
        result.message = self._cancel_reason

        await self._send_notification(result, req.notifications)
        return result


# ---------------------------------------------------------------------------
# Inline save_artifacts activity (Step 5)
# Implemented here to avoid a separate import cycle; could be moved to its own
# module if it grows complex.
# ---------------------------------------------------------------------------

from temporalio import activity as _activity  # noqa: E402  (must follow workflow defs)
from google.cloud import storage as _storage   # noqa: E402


@_activity.defn(name="save_artifacts_activity")
async def _save_artifacts_activity(
    artifact_cfg: ArtifactConfig,
    final_step: int,
    project: str,
) -> SaveResult:
    """Verify adapter files exist under output_uri and return their paths.

    Implements §3.4 Step 5.  For bulk-delete cleanup of old checkpoint fragments,
    use storage.Client.bucket.delete_blobs in batches of 100 (GCS BulkWriter
    equivalent) — never per-object loops.
    """
    from temporal.workflows.shared import ArtifactsMissing

    storage_client = _storage.Client(project=project)

    output_uri = artifact_cfg.output_uri
    bucket_name, prefix = _parse_gcs_uri(output_uri)
    adapter_prefix = f"{prefix}/adapter"
    bucket = storage_client.bucket(bucket_name)

    required_files = [
        f"{adapter_prefix}/adapter_model.safetensors",
        f"{adapter_prefix}/adapter_config.json",
        f"{adapter_prefix}/tokenizer.json",
    ]

    missing = [f for f in required_files if not bucket.blob(f).exists()]
    if missing:
        raise ArtifactsMissing(
            f"Required adapter files not found under {output_uri!r}: {missing}"
        )

    adapter_uri = f"{output_uri}/adapter"
    merged_uri: Optional[str] = None

    # Check if merged weights exist
    merged_index = bucket.blob(f"{prefix}/merged/model.safetensors.index.json")
    if merged_index.exists():
        merged_uri = f"{output_uri}/merged"

    # Read training_metrics.json if present
    metrics: dict = {}
    metrics_blob = bucket.blob(f"{prefix}/training_metrics.json")
    if metrics_blob.exists():
        import json

        metrics = json.loads(metrics_blob.download_as_text())

    _activity.heartbeat({"stage": "save_artifacts", "step": final_step})

    return SaveResult(
        adapter_uri=adapter_uri,
        merged_uri=merged_uri,
        metrics=metrics,
    )


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


def _project_from_uri(uri: str) -> str:
    bucket = uri.removeprefix("gs://").split("/")[0]
    for suffix in ("-final-models", "-checkpoints", "-processed", "-raw"):
        if suffix in bucket:
            return bucket[: bucket.index(suffix)]
    return bucket.split("-")[0]


def _environment_from_uri(uri: str) -> str:
    """Infer environment (stage | prod) from the GCS bucket name."""
    bucket = uri.removeprefix("gs://").split("/")[0]
    if "stage" in bucket or "dfh-stage" in bucket:
        return "stage"
    if "prod" in bucket or "dfh-prod" in bucket:
        return "prod"
    return "stage"
