"""Hugging Face Hub upload activity (Step 7 of FineTuneWorkflow).

Optionally pushes the fine-tuned LoRA adapter (or merged model) to HF Hub.
Implements §3.4 Step 7 of ARCHITECTURE.md.

Skip condition: artifacts.upload_hf_hub == false.
HF_TOKEN is fetched from Secret Manager at runtime — it is NEVER in workflow input.
HF_TOKEN is only required for pushing to HF Hub; Unsloth base models are ungated.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from google.cloud import secretmanager, storage
from temporalio import activity

from temporal.workflows.shared import (
    ArtifactConfig,
    FineTuneRequest,
    HFAuthError,
    HFResult,
    SaveResult,
)

logger = logging.getLogger(__name__)

_HF_SECRET_NAME_DEFAULT = "hf-token"


def _fetch_hf_token(project: str, secret_name: str = _HF_SECRET_NAME_DEFAULT) -> str:
    """Fetch HF_TOKEN from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    secret_path = f"projects/{project}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(name=secret_path)
    return response.payload.data.decode("utf-8").strip()


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


def _download_adapter_to_temp(
    storage_client: storage.Client, adapter_uri: str
) -> str:
    """Download the adapter directory from GCS to a local temp directory."""
    bucket_name, prefix = _parse_gcs_uri(adapter_uri)
    blobs = list(storage_client.list_blobs(bucket_name, prefix=prefix + "/"))

    tmpdir = tempfile.mkdtemp(prefix="hf_adapter_")
    for blob in blobs:
        rel_path = blob.name[len(prefix) :].lstrip("/")
        if not rel_path:
            continue
        local_path = Path(tmpdir) / rel_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))
        logger.debug("Downloaded %s → %s", blob.name, local_path)

    return tmpdir


@activity.defn
async def upload_to_hf_hub(
    save_result: SaveResult,
    req: FineTuneRequest,
) -> HFResult:
    """Push the fine-tuned adapter to Hugging Face Hub.

    Retry policy: HF_RETRY (5 attempts, exponential to 5 min, non-retryable HFAuthError).
    The HF SDK supports resumable uploads — retries are safe.
    """
    project = _project_from_uri(req.artifacts.output_uri)
    repo_id = req.artifacts.hf_repo_id
    if not repo_id:
        raise ValueError("artifacts.hf_repo_id must be set when upload_hf_hub=true")

    # Resolve HF_TOKEN — prefer env var (injected by Secret Manager ref in the
    # CustomJob env) then fall back to a direct Secret Manager fetch.
    hf_token = os.environ.get("HF_TOKEN") or _fetch_hf_token(project)
    if not hf_token:
        raise HFAuthError("HF_TOKEN is empty — check the 'hf-token' Secret Manager entry")

    # Validate token before attempting a large upload
    try:
        from huggingface_hub import HfApi

        api = HfApi(token=hf_token)
        api.whoami()  # raises ValueError / RepositoryNotFoundError on bad token
    except Exception as exc:
        if "invalid" in str(exc).lower() or "401" in str(exc):
            raise HFAuthError(f"HF token validation failed: {exc}") from exc

    storage_client = storage.Client(project=project)

    # Prefer merged weights if available; otherwise upload the raw adapter
    source_uri = save_result.merged_uri or save_result.adapter_uri
    logger.info("Downloading adapter from '%s'", source_uri)
    local_dir = _download_adapter_to_temp(storage_client, source_uri)

    try:
        from huggingface_hub import HfApi

        api = HfApi(token=hf_token)

        # Create repo if it doesn't exist
        try:
            api.repo_info(repo_id=repo_id)
        except Exception:
            logger.info(
                "Repository '%s' not found — creating (private=%s)",
                repo_id,
                req.artifacts.hf_private,
            )
            api.create_repo(
                repo_id=repo_id,
                private=req.artifacts.hf_private,
                exist_ok=True,
            )

        logger.info("Uploading to HF Hub repo '%s'", repo_id)
        commit_info = api.upload_folder(
            folder_path=local_dir,
            repo_id=repo_id,
            commit_message=f"Fine-tuned adapter: {req.job_name}",
        )

        repo_url = f"https://huggingface.co/{repo_id}"
        commit_sha = getattr(commit_info, "oid", "") or ""
        logger.info("Uploaded to %s (commit=%s)", repo_url, commit_sha)
        return HFResult(repo_url=repo_url, commit_sha=commit_sha)

    finally:
        import shutil

        shutil.rmtree(local_dir, ignore_errors=True)


def _project_from_uri(uri: str) -> str:
    bucket = uri.removeprefix("gs://").split("/")[0]
    for suffix in ("-final-models", "-checkpoints", "-processed", "-raw"):
        if suffix in bucket:
            return bucket[: bucket.index(suffix)]
    return bucket.split("-")[0]
