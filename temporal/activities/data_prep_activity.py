"""Data validation and preprocessing activity (Step 1 of FineTuneWorkflow).

Implements §3.4 Step 1 and §5 of ARCHITECTURE.md.

If data.processed_uri already contains valid train.jsonl + val.jsonl whose
manifest hash matches the raw input listing, preparation is skipped (idempotent).
Otherwise the data_prep pipeline is invoked against data.raw_uri.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from google.cloud import storage
from temporalio import activity

from temporal.workflows.shared import DataConfig, DataPrepResult, DataValidationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: GCS utilities
# ---------------------------------------------------------------------------


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


def _list_blobs(storage_client: storage.Client, uri: str) -> list[storage.Blob]:
    bucket_name, prefix = _parse_gcs_uri(uri)
    return list(storage_client.list_blobs(bucket_name, prefix=prefix + "/"))


def _blob_listing_sha256(blobs: list[storage.Blob]) -> str:
    """Stable SHA-256 of the (name, size, updated) listing for idempotency."""
    payload = json.dumps(
        sorted(
            [
                {"name": b.name, "size": b.size, "updated": str(b.updated)}
                for b in blobs
                if not b.name.endswith("/")
            ],
            key=lambda x: x["name"],
        )
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _derived_processed_uri(raw_uri: str, job_name: str) -> str:
    """Derive a processed_uri from the raw_uri bucket + job_name."""
    bucket_name, _ = _parse_gcs_uri(raw_uri)
    project_id = bucket_name.split("-")[0] if "-" in bucket_name else bucket_name
    return f"gs://{project_id}-processed-datasets/{job_name}"


def _check_existing_processed(
    storage_client: storage.Client,
    processed_uri: str,
    input_sha: str,
) -> Optional[DataPrepResult]:
    """Return DataPrepResult if valid processed data already exists and hashes match."""
    bucket_name, prefix = _parse_gcs_uri(processed_uri)
    bucket = storage_client.bucket(bucket_name)

    manifest_blob = bucket.blob(f"{prefix}/manifest.json")
    train_blob = bucket.blob(f"{prefix}/train.jsonl")
    val_blob = bucket.blob(f"{prefix}/val.jsonl")
    sample_blob = bucket.blob(f"{prefix}/sample.jsonl")

    if not all(b.exists() for b in [manifest_blob, train_blob, val_blob]):
        return None

    try:
        manifest = json.loads(manifest_blob.download_as_text())
    except Exception:
        return None

    if manifest.get("input_listing_sha256") != input_sha:
        logger.warning(
            "processed_uri '%s' has mismatched hash (expected %s, got %s); "
            "will re-prepare.",
            processed_uri,
            input_sha,
            manifest.get("input_listing_sha256"),
        )
        return None

    return DataPrepResult(
        processed_uri=processed_uri,
        train_rows=manifest.get("n_train", 0),
        val_rows=manifest.get("n_val", 0),
        sample_uri=f"{processed_uri}/sample.jsonl" if sample_blob.exists() else "",
    )


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


@activity.defn
async def validate_and_preprocess(
    data_cfg: DataConfig,
    job_name: str,
    project: str,
) -> DataPrepResult:
    """Validate raw documents and produce processed train/val JSONL files.

    Retry policy: DATA_PREP_RETRY (3 attempts, non-retryable DataValidationError).
    Heartbeats every 30 s are emitted during long extraction loops.
    """
    storage_client = storage.Client(project=project)

    # Resolve the processed_uri
    processed_uri = data_cfg.processed_uri or _derived_processed_uri(
        data_cfg.raw_uri, job_name
    )

    # List raw inputs
    raw_blobs = _list_blobs(storage_client, data_cfg.raw_uri)
    raw_files = [b for b in raw_blobs if not b.name.endswith("/")]

    if not raw_files:
        raise DataValidationError(
            f"No input files found under {data_cfg.raw_uri!r}. "
            "Upload raw documents before submitting a fine-tune job."
        )

    if len(raw_files) == 1:
        logger.warning("WARN: dataset_size_low (n=1) under '%s'", data_cfg.raw_uri)

    # Compute input listing hash
    input_sha = _blob_listing_sha256(raw_files)

    # Idempotency: skip prep if valid output already exists with matching hash
    existing = _check_existing_processed(storage_client, processed_uri, input_sha)
    if existing:
        logger.info(
            "Skipping data prep — valid processed output already at '%s' "
            "(hash=%s matches).",
            processed_uri,
            input_sha,
        )
        return existing

    # Run data prep pipeline
    logger.info(
        "Running data_prep pipeline: %d files → '%s'",
        len(raw_files),
        processed_uri,
    )

    # Heartbeat before starting the extraction loop
    activity.heartbeat(
        {
            "stage": "starting",
            "files_total": len(raw_files),
            "processed_uri": processed_uri,
        }
    )

    # Invoke the data_prep pipeline module
    try:
        from data_prep.pipeline import run_pipeline  # type: ignore[import]

        result = run_pipeline(
            raw_uri=data_cfg.raw_uri,
            processed_uri=processed_uri,
            data_format=data_cfg.format.value,
            train_split=data_cfg.train_split,
            validation_split=data_cfg.validation_split,
            job_name=job_name,
            heartbeat_fn=lambda progress: activity.heartbeat(
                {
                    "stage": "extract",
                    "files_done": progress,
                    "files_total": len(raw_files),
                }
            ),
        )
    except DataValidationError:
        raise
    except Exception as exc:
        raise RuntimeError(f"data_prep.pipeline failed: {exc}") from exc

    return DataPrepResult(
        processed_uri=processed_uri,
        train_rows=result["n_train"],
        val_rows=result["n_val"],
        sample_uri=f"{processed_uri}/sample.jsonl",
    )
