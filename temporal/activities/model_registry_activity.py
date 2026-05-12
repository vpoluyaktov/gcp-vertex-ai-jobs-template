"""Model Registry activity (Step 6 of FineTuneWorkflow).

Registers the fine-tuned adapter as a new version in Vertex AI Model Registry.
Implements §3.4 Step 6 of ARCHITECTURE.md.

Skip condition: artifacts.register_in_vertex == false.
Edge cases:
  - First version of a new model: creates Model with alias 'default'.
  - 100-version cap: raises VersionLimitReached (non-retryable).
"""

from __future__ import annotations

import logging
import re
from typing import List

from google.cloud import aiplatform
from temporalio import activity

from temporal.workflows.shared import (
    FineTuneRequest,
    RegisterResult,
    SaveResult,
    VersionLimitReached,
)

logger = logging.getLogger(__name__)

_VERSION_CAP = 100


def _job_name_prefix(job_name: str) -> str:
    """Strip trailing -vN suffix to get the model family name."""
    return re.sub(r"-v\d+$", "", job_name)


@activity.defn
async def register_model(
    save_result: SaveResult,
    req: FineTuneRequest,
) -> RegisterResult:
    """Upload the fine-tuned adapter to Vertex AI Model Registry.

    Creates a new model version under the model whose display_name matches
    the job_name prefix (strips trailing -vN).  Sets canonical labels.

    Retry policy: DEFAULT_RETRY (3 attempts, non-retryable VersionLimitReached).
    """
    project = _project_from_uri(req.artifacts.output_uri)
    location = req.infrastructure.region

    aiplatform.init(project=project, location=location)

    model_name_prefix = _job_name_prefix(req.job_name)
    labels = {
        "peft_type": req.peft.type.value,
        "base_model": req.model.base_model_id.replace("/", "-").lower()[:63],
        "framework": "unsloth-trl",
    }

    # Try to find an existing model by display_name prefix
    existing_models = aiplatform.Model.list(
        filter=f'display_name="{model_name_prefix}"',
        project=project,
        location=location,
    )

    if existing_models:
        model = existing_models[0]

        # Check version cap
        versions = aiplatform.Model.list(
            filter=f'display_name="{model_name_prefix}"',
            project=project,
            location=location,
        )
        if len(versions) >= _VERSION_CAP:
            raise VersionLimitReached(
                f"Model '{model_name_prefix}' has reached the {_VERSION_CAP}-version cap. "
                "Prune old versions via the Vertex AI console before adding new ones."
            )

        logger.info(
            "Uploading new version to existing model '%s'", model.resource_name
        )
        new_model = aiplatform.Model.upload(
            display_name=model_name_prefix,
            artifact_uri=save_result.adapter_uri,
            serving_container_image_uri="us-docker.pkg.dev/vertex-ai/prediction/sklearn-cpu.1-0:latest",
            labels=labels,
            parent_model=model.resource_name,
            project=project,
            location=location,
        )
    else:
        # First version — create with alias 'default'
        logger.info(
            "Creating new model '%s' in Vertex AI Model Registry", model_name_prefix
        )
        new_model = aiplatform.Model.upload(
            display_name=model_name_prefix,
            artifact_uri=save_result.adapter_uri,
            serving_container_image_uri="us-docker.pkg.dev/vertex-ai/prediction/sklearn-cpu.1-0:latest",
            labels=labels,
            project=project,
            location=location,
        )

    version_id = new_model.version_id or "1"
    aliases: List[str] = list(getattr(new_model, "version_aliases", None) or [])

    logger.info(
        "Registered model version %s as '%s'", version_id, new_model.resource_name
    )

    return RegisterResult(
        model_resource_name=new_model.resource_name,
        version_id=version_id,
        aliases=aliases,
    )


def _project_from_uri(uri: str) -> str:
    """Extract project-id from a gs://<project>-<bucket-suffix>/... URI."""
    bucket = uri.removeprefix("gs://").split("/")[0]
    # Heuristic: everything before the first recognized suffix
    for suffix in ("-final-models", "-checkpoints", "-processed", "-raw"):
        if suffix in bucket:
            return bucket[: bucket.index(suffix)]
    return bucket.split("-")[0]
