"""Data preparation pipeline — ties convert_documents + validate_dataset together.

This module is the integration point imported by
``temporal.activities.data_prep_activity``.  It orchestrates the full
pipeline from raw GCS blobs → processed JSONL outputs in one call.

The ``run_pipeline`` function is designed to be called from within a Temporal
activity; it accepts a ``heartbeat_fn`` callback so the activity can emit
heartbeats every 50 files (§5.5 progress sentinel).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from typing import Any, Callable, Dict, Optional

from google.cloud import storage as _storage

from data_prep.convert_documents import DocumentConverter, load_prelabelled_jsonl
from data_prep.validate_dataset import (
    DataValidationError,
    DatasetValidator,
    validate_train_val_splits,
)

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".html", ".htm", ".json", ".jsonl", ".txt", ".md"}
_HEARTBEAT_INTERVAL = 50   # emit heartbeat every N files


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


def _blob_listing_sha256(blobs: list) -> str:
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


def _write_progress(
    storage_client: _storage.Client,
    processed_uri: str,
    stage: str,
    files_done: int,
    files_total: int,
) -> None:
    """Write the §5.5 _progress.json sentinel to GCS."""
    bucket_name, prefix = _parse_gcs_uri(processed_uri)
    payload = json.dumps({
        "stage": stage,
        "files_done": files_done,
        "files_total": files_total,
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    try:
        storage_client.bucket(bucket_name).blob(
            f"{prefix}/_progress.json"
        ).upload_from_string(payload.encode("utf-8"), content_type="application/json")
    except Exception as exc:
        logger.debug("Failed to write _progress.json: %s", exc)


def run_pipeline(
    raw_uri: str,
    processed_uri: str,
    data_format: str,
    train_split: float,
    validation_split: float,
    job_name: str,
    project: Optional[str] = None,
    heartbeat_fn: Optional[Callable[[int], None]] = None,
    strict_extensions: bool = False,
    run_tokenizer_check: bool = False,
) -> Dict[str, Any]:
    """Full data preparation pipeline from raw GCS input to processed outputs.

    Parameters
    ----------
    raw_uri:
        GCS URI prefix containing raw source documents.
    processed_uri:
        GCS URI prefix for output JSONL files (train, val, rejected, sample,
        manifest).
    data_format:
        ``"chat"`` or ``"instruct"``
    train_split, validation_split:
        Partition ratios.  Must sum to 1.0 (validated before processing starts).
    job_name:
        Used for the reproducible split seed and manifest.
    project:
        GCP project ID for the storage client.
    heartbeat_fn:
        Called with the count of files processed so far.  Used by the Temporal
        activity to emit heartbeats.
    strict_extensions:
        If True, unknown file extensions raise an error.  Otherwise they are
        skipped with a WARN.

    Returns
    -------
    dict with keys: ``n_train``, ``n_val``, ``n_rejected``, ``manifest_uri``
    """
    # Pre-flight checks
    validate_train_val_splits(train_split, validation_split)

    storage_client = _storage.Client(project=project)
    bucket_name, prefix = _parse_gcs_uri(raw_uri)

    # List raw blobs
    all_blobs = list(storage_client.list_blobs(bucket_name, prefix=prefix + "/"))
    raw_blobs = [
        b for b in all_blobs
        if not b.name.endswith("/")
    ]

    if not raw_blobs:
        raise DataValidationError(
            f"No input files found under {raw_uri!r}. "
            "Upload raw documents before submitting a fine-tune job."
        )

    if len(raw_blobs) == 1:
        logger.warning("WARN: dataset_size_low (n=1) under '%s'", raw_uri)

    input_sha = _blob_listing_sha256(raw_blobs)
    files_total = len(raw_blobs)

    logger.info(
        "Starting data prep: %d files, format=%s, job=%s",
        files_total,
        data_format,
        job_name,
    )

    converter = DocumentConverter(
        data_format=data_format,
        strict=strict_extensions,
    )

    rows: list[dict] = []
    files_done = 0

    for blob in raw_blobs:
        ext = "." + blob.name.rsplit(".", 1)[-1].lower() if "." in blob.name else ""

        try:
            # Pre-labelled JSONL → passthrough validation
            if ext in (".jsonl", ".json"):
                data = blob.download_as_bytes()
                valid, rejected_pre = load_prelabelled_jsonl(data, data_format)
                rows.extend(valid)
                if rejected_pre:
                    logger.warning(
                        "%d pre-labelled rows rejected in '%s'",
                        len(rejected_pre),
                        blob.name,
                    )
            elif ext in _SUPPORTED_EXTENSIONS:
                blob_rows = list(converter.convert_gcs_blob(blob))
                rows.extend(blob_rows)
            else:
                if strict_extensions:
                    raise DataValidationError(
                        f"Unsupported extension {ext!r} for file {blob.name!r}"
                    )
                logger.warning(
                    "Skipping unsupported extension %r (%s)", ext, blob.name
                )

        except DataValidationError:
            raise
        except Exception as exc:
            logger.warning("Error processing '%s': %s — skipping", blob.name, exc)

        files_done += 1

        # Progress sentinel + heartbeat
        if files_done % _HEARTBEAT_INTERVAL == 0 or files_done == files_total:
            _write_progress(
                storage_client, processed_uri, "extract", files_done, files_total
            )
            if heartbeat_fn:
                heartbeat_fn(files_done)

    logger.info("Extracted %d rows from %d files", len(rows), files_total)

    if not rows:
        raise DataValidationError(
            f"All {files_total} files extracted to empty text. "
            "Check that source documents contain readable text."
        )

    # Validate + dedup + split + write
    validator = DatasetValidator(
        data_format=data_format,
        job_name=job_name,
        train_split=train_split,
        validation_split=validation_split,
        input_listing_sha256=input_sha,
    )

    result = validator.run(rows, run_tokenizer_check=run_tokenizer_check)
    result.write_to_gcs(processed_uri, project=project)

    manifest_uri = f"{processed_uri}/manifest.json"
    logger.info(
        "Pipeline complete: %d train, %d val, %d rejected. Manifest: %s",
        result.n_train,
        result.n_val,
        result.n_rejected,
        manifest_uri,
    )

    return {
        "n_train": result.n_train,
        "n_val": result.n_val,
        "n_rejected": result.n_rejected,
        "manifest_uri": manifest_uri,
    }
