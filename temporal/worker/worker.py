"""Temporal worker entrypoint — Cloud Run Job execution model.

Reads connection parameters from environment variables:
  TEMPORAL_ADDRESS      — gRPC address of the self-hosted Temporal server
                          (e.g. temporal-server.stage.internal:7233)
  TEMPORAL_NAMESPACE    — Temporal namespace (default: "default")
  TEMPORAL_TASK_QUEUE   — task queue to poll (e.g. vertex-finetune-stage)
  GCP_PROJECT_ID        — GCP project for Vertex AI / GCS calls
  GCP_REGION            — default region (default: "us-central1")
  LOG_LEVEL             — log level (default: INFO)

No mTLS / API key — the worker reaches the Temporal server over the private
VPC connector (§12.6 of ARCHITECTURE.md).  Security is at the network layer.

Run via:
  docker run <worker-image> python -m temporal.worker.worker
or:
  gcloud run jobs execute temporal-worker-stage ...
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from temporalio.client import Client
from temporalio.worker import Worker

# Workflow and activity registrations
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
from temporal.workflows.fine_tuning_workflow import (
    FineTuneWorkflow,
    _save_artifacts_activity,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_TEMPORAL_ADDRESS = os.environ.get(
    "TEMPORAL_ADDRESS", "temporal-server.stage.internal:7233"
)
_TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
_TEMPORAL_TASK_QUEUE = os.environ.get(
    "TEMPORAL_TASK_QUEUE", "vertex-finetune-stage"
)
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker main
# ---------------------------------------------------------------------------


async def _run_worker() -> None:
    logger.info(
        "Connecting to Temporal at %s (namespace=%s, queue=%s)",
        _TEMPORAL_ADDRESS,
        _TEMPORAL_NAMESPACE,
        _TEMPORAL_TASK_QUEUE,
    )

    client = await Client.connect(
        _TEMPORAL_ADDRESS,
        namespace=_TEMPORAL_NAMESPACE,
    )

    worker = Worker(
        client,
        task_queue=_TEMPORAL_TASK_QUEUE,
        workflows=[FineTuneWorkflow],
        activities=[
            validate_and_preprocess,
            build_training_image,
            submit_training_job,
            monitor_training_job,
            _save_artifacts_activity,
            register_model,
            upload_to_hf_hub,
            prepare_serving_artifacts,
            send_notification,
        ],
        # Single Cloud Run Job execution — run until the workflow completes
        # or the Cloud Run Job timeout is hit.
        max_concurrent_workflow_tasks=10,
        max_concurrent_activities=10,
    )

    logger.info("Worker started.  Polling task queue '%s'...", _TEMPORAL_TASK_QUEUE)
    await worker.run()


def main() -> None:
    try:
        asyncio.run(_run_worker())
    except KeyboardInterrupt:
        logger.info("Worker stopped by interrupt.")


if __name__ == "__main__":
    main()
