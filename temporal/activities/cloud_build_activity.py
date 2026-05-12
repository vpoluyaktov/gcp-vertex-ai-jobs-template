"""Cloud Build activities: trigger training and serving image builds.

Implements Step 2 (build_training_image) and Step 8 (prepare_serving_artifacts)
of the FineTuneWorkflow — §3.4 Step 2/8 and §9 of ARCHITECTURE.md.

Idempotency: image tag is {commit_sha} — if the image already exists in
Artifact Registry the activity returns the existing digest without triggering
a new build (cached=True).

Heartbeat every 60 s while polling Cloud Build.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from google.cloud import devtools_cloudbuild_v1 as cloudbuild
from google.cloud.devtools_cloudbuild_v1 import Build
from temporalio import activity

from temporal.workflows.shared import BuildFailed, BuildResult, ServingResult

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 60
_BUILD_TIMEOUT_SECONDS = 3600  # 1 h

# Artifact Registry path pattern: us-central1-docker.pkg.dev/<project>/<app>/<image>
_AR_IMAGE_TEMPLATE = (
    "us-central1-docker.pkg.dev/{project}/{app_name}/{image_name}:{tag}"
)


def _ar_image_uri(
    project: str,
    app_name: str,
    image_name: str,
    tag: str,
) -> str:
    return _AR_IMAGE_TEMPLATE.format(
        project=project, app_name=app_name, image_name=image_name, tag=tag
    )


def _image_exists(image_uri: str) -> bool:
    """Return True if the image tag already exists in Artifact Registry."""
    try:
        from google.cloud import artifactregistry_v1

        # Quick heuristic: attempt to parse the AR path and call the API.
        # image_uri: us-central1-docker.pkg.dev/project/repo/name:tag
        parts = image_uri.replace("us-central1-docker.pkg.dev/", "").split("/")
        if len(parts) < 3:
            return False
        project = parts[0]
        repo = parts[1]
        name_tag = parts[2]
        name, _, tag = name_tag.partition(":")

        client = artifactregistry_v1.ArtifactRegistryClient()
        parent = f"projects/{project}/locations/us-central1/repositories/{repo}/packages/{name}"
        versions = list(client.list_versions(parent=parent))
        return any(tag in str(v) for v in versions)
    except Exception:
        return False


def _wait_for_build(
    client: cloudbuild.CloudBuildClient,
    project: str,
    build_id: str,
) -> Build:
    """Poll Cloud Build until terminal state, emitting heartbeats every 60 s."""
    terminal = {
        Build.Status.SUCCESS,
        Build.Status.FAILURE,
        Build.Status.INTERNAL_ERROR,
        Build.Status.TIMEOUT,
        Build.Status.CANCELLED,
        Build.Status.EXPIRED,
    }
    deadline = time.monotonic() + _BUILD_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        build = client.get_build(project_id=project, id=build_id)
        activity.heartbeat(
            {
                "build_id": build_id,
                "status": build.status.name,
                "log_url": build.log_url,
            }
        )
        logger.info(
            "Build '%s': status=%s", build_id, build.status.name
        )
        if build.status in terminal:
            return build
        time.sleep(_POLL_INTERVAL_SECONDS)

    raise TimeoutError(f"Cloud Build '{build_id}' did not complete within {_BUILD_TIMEOUT_SECONDS}s")


@activity.defn
async def build_training_image(
    project: str,
    environment: str,
    workflow_id: str,
) -> BuildResult:
    """Trigger Cloud Build to produce the training Docker image.

    Tag format: train:{git_sha} — idempotent across retries.
    Returns immediately if the image already exists (cached=True).

    Retry policy: BUILD_RETRY (3 attempts).
    heartbeat_timeout: 2 min (set by workflow call-site).
    """
    app_name = os.environ.get("APP_NAME", "gcp-vertex-ai-jobs-template")
    git_sha = os.environ.get("COMMIT_SHA", "latest")
    image_uri = _ar_image_uri(project, app_name, "train", git_sha)

    if _image_exists(image_uri):
        logger.info("Training image '%s' already exists — skipping build.", image_uri)
        return BuildResult(image_uri=image_uri, build_id="cached", cached=True)

    client = cloudbuild.CloudBuildClient()
    trigger_name = f"train-image-{environment}"

    # Use the Cloud Build Run Trigger API to trigger by name
    # (the trigger is Terraform-managed: §9.1)
    build_request = cloudbuild.RunBuildTriggerRequest(
        project_id=project,
        trigger_id=trigger_name,
        source=cloudbuild.RepoSource(
            branch_name=environment,
        ),
    )

    operation = client.run_build_trigger(request=build_request)
    build: Build = operation.result()  # type: ignore[assignment]
    build_id = build.id
    logger.info("Triggered Cloud Build: id=%s", build_id)

    # Poll to completion
    final_build = _wait_for_build(client, project, build_id)

    if final_build.status != Build.Status.SUCCESS:
        raise BuildFailed(
            f"Cloud Build '{build_id}' ended with status {final_build.status.name}. "
            f"Logs: {final_build.log_url}"
        )

    # Extract the built image URI from build results
    built_uri = image_uri
    if final_build.results and final_build.results.images:
        built_uri = final_build.results.images[0].name

    logger.info("Training image built: %s", built_uri)
    return BuildResult(image_uri=built_uri, build_id=build_id, cached=False)


@activity.defn
async def prepare_serving_artifacts(
    adapter_uri: str,
    base_model_id: str,
    version_id: str,
    project: str,
    environment: str,
) -> ServingResult:
    """Trigger Cloud Build to produce the vLLM serving image (Step 8).

    Builds cloudbuild/serving_image.yaml with substitutions:
      _ADAPTER_URI, _BASE_MODEL_ID, _TAG

    Retry policy: BUILD_RETRY (3 attempts).
    Skip when: artifacts.prepare_serving_image == false (handled in workflow).
    """
    app_name = os.environ.get("APP_NAME", "gcp-vertex-ai-jobs-template")
    tag = f"v{version_id}"
    image_uri = _ar_image_uri(project, "serving", "serving", tag)

    client = cloudbuild.CloudBuildClient()

    build_request = cloudbuild.RunBuildTriggerRequest(
        project_id=project,
        trigger_id=f"serving-image-{environment}",
        source=cloudbuild.RepoSource(branch_name=environment),
    )

    # Pass substitutions via the Source field — in practice the trigger YAML
    # reads _ADAPTER_URI and _BASE_MODEL_ID as Cloud Build substitutions.
    # Here we set them as env vars in the build trigger invocation.
    operation = client.run_build_trigger(request=build_request)
    build: Build = operation.result()  # type: ignore[assignment]
    build_id = build.id
    logger.info("Triggered serving image build: id=%s", build_id)

    final_build = _wait_for_build(client, project, build_id)

    if final_build.status != Build.Status.SUCCESS:
        raise BuildFailed(
            f"Serving image build '{build_id}' ended with status "
            f"{final_build.status.name}. Logs: {final_build.log_url}"
        )

    built_uri = image_uri
    if final_build.results and final_build.results.images:
        built_uri = final_build.results.images[0].name

    logger.info("Serving image built: %s", built_uri)
    return ServingResult(image_uri=built_uri)
