"""CLI to start a FineTuneWorkflow against a deployed Temporal server.

Used in two contexts:

1. Cloud Build post-deploy smoke-submit (see
   `cloudbuild/cloudbuild-trigger-workflow.yaml`), which submits a `debug`
   workflow to validate the freshly-deployed stack.
2. Operators triggering production fine-tunes by hand:
       python -m temporal.client.trigger_workflow \\
           --config configs/training-job-llama3-8b.yaml \\
           --env stage \\
           --wait

The job YAML schema mirrors `temporal.workflows.shared.FineTuneRequest`
(Pydantic). Local files and gs:// URIs are both supported.

Defaults match the infrastructure provisioned by
`terraform/modules/temporal_server`:

    TEMPORAL_ADDRESS   = temporal-server.<env>.internal:443
    TEMPORAL_NAMESPACE = default
    TEMPORAL_TASK_QUEUE = vertex-finetune-<env>

Each can be overridden via env vars or CLI flags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError
from temporalio.client import Client, WorkflowFailureError

# Importing the workflow class directly lets us start it by reference rather
# than by stringly-typed name. Falling back to "FineTuneWorkflow" by name keeps
# the script usable from an image that doesn't ship the workflow code.
try:
    from temporal.workflows.fine_tuning_workflow import FineTuneWorkflow
    _WORKFLOW_REF: Any = FineTuneWorkflow.run
except Exception:  # pragma: no cover — defensive fallback
    _WORKFLOW_REF = "FineTuneWorkflow"

from temporal.workflows.shared import FineTuneRequest

logger = logging.getLogger("trigger_workflow")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _read_local(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _read_gcs(uri: str) -> str:
    # Lazy import: trigger_workflow is sometimes invoked in environments
    # without google-cloud-storage when the config lives locally.
    from google.cloud import storage  # type: ignore

    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc or not parsed.path:
        raise ValueError(f"malformed GCS URI: {uri}")
    bucket_name = parsed.netloc
    blob_name = parsed.path.lstrip("/")
    client = storage.Client()
    return client.bucket(bucket_name).blob(blob_name).download_as_text()


def load_job_config(uri: str) -> dict[str, Any]:
    """Load a job YAML from a local path or gs:// URI."""
    if uri.startswith("gs://"):
        raw = _read_gcs(uri)
    else:
        raw = _read_local(uri)
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"job config at {uri} must be a YAML mapping, got {type(parsed).__name__}")
    return parsed


# ---------------------------------------------------------------------------
# Workflow id derivation
# ---------------------------------------------------------------------------

def derive_workflow_id(request: FineTuneRequest, override: str | None) -> str:
    """Stable, human-readable id. Includes the job_name so a glance at the
    Temporal UI tells you what's running."""
    if override:
        return override
    return f"finetune-{request.job_name}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trigger_workflow",
        description="Start a FineTuneWorkflow run against the env's Temporal server.",
    )
    p.add_argument(
        "--config",
        required=True,
        help="Path to job YAML — local filesystem or gs:// URI",
    )
    p.add_argument(
        "--env",
        required=True,
        choices=("stage", "prod"),
        help="Target environment — drives default Temporal address and task queue",
    )
    p.add_argument(
        "--workflow-id",
        default=None,
        help="Workflow id (default: finetune-<job_name>-<uuid8>)",
    )
    p.add_argument(
        "--temporal-address",
        default=os.environ.get("TEMPORAL_ADDRESS"),
        help="Temporal frontend address. Defaults to env TEMPORAL_ADDRESS, then to "
             "temporal-server.<env>.internal:443.",
    )
    p.add_argument(
        "--namespace",
        default=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        help="Temporal namespace (default: env TEMPORAL_NAMESPACE or `default`)",
    )
    p.add_argument(
        "--task-queue",
        default=os.environ.get("TEMPORAL_TASK_QUEUE"),
        help="Task queue (default: env TEMPORAL_TASK_QUEUE or vertex-finetune-<env>)",
    )
    p.add_argument(
        "--wait",
        action="store_true",
        help="Block until the workflow completes and emit the result as JSON",
    )
    p.add_argument(
        "--wait-seconds",
        type=int,
        default=300,
        help="Timeout when --wait is set (default: 300s — connection sanity, not full run)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the config and resolve defaults, but do not contact Temporal",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="DEBUG logging",
    )
    return p


def _resolve_defaults(args: argparse.Namespace) -> argparse.Namespace:
    if not args.temporal_address:
        args.temporal_address = f"temporal-server.{args.env}.internal:443"
    if not args.task_queue:
        args.task_queue = f"vertex-finetune-{args.env}"
    return args


async def _start(args: argparse.Namespace, request: FineTuneRequest, workflow_id: str) -> int:
    logger.info(
        "connecting to temporal: address=%s namespace=%s",
        args.temporal_address,
        args.namespace,
    )
    client = await Client.connect(args.temporal_address, namespace=args.namespace)

    logger.info(
        "starting workflow: id=%s task_queue=%s job_name=%s",
        workflow_id,
        args.task_queue,
        request.job_name,
    )
    handle = await client.start_workflow(
        _WORKFLOW_REF,
        args=[request],
        id=workflow_id,
        task_queue=args.task_queue,
    )
    # Always emit the handle details so the caller can correlate with Temporal UI.
    sys.stdout.write(
        json.dumps(
            {
                "workflow_id": handle.id,
                "run_id": handle.first_execution_run_id,
                "task_queue": args.task_queue,
                "namespace": args.namespace,
                "temporal_address": args.temporal_address,
            },
            indent=2,
        )
        + "\n"
    )

    if not args.wait:
        return 0

    logger.info("waiting up to %ds for workflow completion", args.wait_seconds)
    try:
        # The temporalio SDK has no per-call timeout — wrap the await.
        result = await asyncio.wait_for(handle.result(), timeout=args.wait_seconds)
    except asyncio.TimeoutError:
        logger.error("workflow did not finish within %ds — not failing CI", args.wait_seconds)
        return 0
    except WorkflowFailureError as exc:
        logger.error("workflow failed: %s", exc)
        return 1
    # FineTuneWorkflow returns a WorkflowResult Pydantic model.
    if hasattr(result, "model_dump"):
        sys.stdout.write(json.dumps(result.model_dump(), indent=2, default=str) + "\n")
    else:
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _resolve_defaults(args)

    logger.info("loading job config: %s", args.config)
    config = load_job_config(args.config)

    # FineTuneRequest carries the URI of its own source so the workflow can
    # re-read the config on retry without depending on the original CLI host.
    # YAMLs don't normally include this (it would be self-referential) — we
    # inject it from --config unless explicitly set in the file.
    config.setdefault("config_uri", args.config)

    try:
        request = FineTuneRequest.model_validate(config)
    except ValidationError as exc:
        logger.error("config validation failed:\n%s", exc)
        return 2

    workflow_id = derive_workflow_id(request, args.workflow_id)

    if args.dry_run:
        sys.stdout.write(
            json.dumps(
                {
                    "dry_run": True,
                    "workflow_id": workflow_id,
                    "task_queue": args.task_queue,
                    "temporal_address": args.temporal_address,
                    "namespace": args.namespace,
                    "job_name": request.job_name,
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    return asyncio.run(_start(args, request, workflow_id))


if __name__ == "__main__":
    sys.exit(main())
