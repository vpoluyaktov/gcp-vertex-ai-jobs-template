"""Dataset validation, deduplication, splitting, and manifest writing.

Implements §5.1 stages 6–9 and all §5.4 edge cases of ARCHITECTURE.md:

  6. Validate every row with the appropriate Pydantic schema; bad rows go to
     rejected.jsonl with a ``_reason`` field.
  7. Split into train.jsonl / val.jsonl with a reproducible seed derived from
     the job_name SHA-256 (§5.1).
  8. Write sample.jsonl (first 5 rows of the training set).
  9. Write manifest.json: input_listing_sha256, n_train, n_val, n_rejected,
     format, tokenizer_validation_pass, pipeline_version.

Edge cases handled (§5.4):
  - Empty row list → DataValidationError
  - All rows extract to empty text → DataValidationError
  - Duplicate content (SHA-256) → first occurrence kept; rest counted in n_rejected
  - Surrogate pair / encoding errors → row rejected with reason ``invalid_encoding``
  - train_split + validation_split != 1.0 → DataValidationError (pre-pipeline check)
  - n_train == 0 after splitting → DataValidationError("empty training split")

Usage (library)::

    validator = DatasetValidator(data_format="chat", job_name="invoices-v1")
    result = validator.run(rows)
    result.write_to_gcs("gs://bucket/processed-datasets/invoices-v1/")

Usage (CLI)::

    python -m data_prep.validate_dataset \\
        --input  gs://bucket/processed-datasets/invoices-v1/raw.jsonl \\
        --output gs://bucket/processed-datasets/invoices-v1/ \\
        --format chat \\
        --job-name invoices-v1 \\
        --train-split 0.95
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "1.0.0"
_SAMPLE_SIZE = 5


# ---------------------------------------------------------------------------
# Custom exception (non-retryable in Temporal context)
# ---------------------------------------------------------------------------


class DataValidationError(Exception):
    """Critical validation failure — must not be retried by the workflow."""


# ---------------------------------------------------------------------------
# Pydantic row schemas
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str = Field(..., pattern=r"^(system|user|assistant)$")
    content: str = Field(..., min_length=1)

    @field_validator("content")
    @classmethod
    def no_surrogates(cls, v: str) -> str:
        try:
            v.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"Content contains invalid encoding: {exc}") from exc
        return v


class ChatRow(BaseModel):
    """Validates a single chat-format JSONL row."""

    messages: list[ChatMessage] = Field(..., min_length=1)

    @model_validator(mode="after")
    def has_required_roles(self) -> "ChatRow":
        roles = {m.role for m in self.messages}
        if "user" not in roles:
            raise ValueError("Chat row must contain at least one user message")
        if "assistant" not in roles:
            raise ValueError("Chat row must contain at least one assistant message")
        return self

    def content_hash(self) -> str:
        payload = json.dumps(
            [{"role": m.role, "content": m.content} for m in self.messages],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class InstructRow(BaseModel):
    """Validates a single instruct-format JSONL row."""

    instruction: str = Field(..., min_length=1)
    input: str = ""
    output: str = Field(..., min_length=1)

    @field_validator("instruction", "input", "output")
    @classmethod
    def no_surrogates(cls, v: str) -> str:
        try:
            v.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"Field contains invalid encoding: {exc}") from exc
        return v

    def content_hash(self) -> str:
        payload = json.dumps(
            {"instruction": self.instruction, "input": self.input, "output": self.output},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    train_rows: list[dict]
    val_rows: list[dict]
    rejected_rows: list[dict]
    sample_rows: list[dict]
    n_train: int
    n_val: int
    n_rejected: int
    n_duplicates: int
    data_format: str
    job_name: str
    input_listing_sha256: str = ""
    tokenizer_validation_pass: bool = True
    pipeline_version: str = PIPELINE_VERSION

    def manifest(self) -> dict:
        return {
            "input_listing_sha256": self.input_listing_sha256,
            "n_train": self.n_train,
            "n_val": self.n_val,
            "n_rejected": self.n_rejected,
            "n_duplicates": self.n_duplicates,
            "format": self.data_format,
            "tokenizer_validation_pass": self.tokenizer_validation_pass,
            "pipeline_version": self.pipeline_version,
            "job_name": self.job_name,
        }

    # ------------------------------------------------------------------
    # GCS write helpers
    # ------------------------------------------------------------------

    def write_to_gcs(self, processed_uri: str, project: Optional[str] = None) -> None:
        """Upload all output files to the processed_uri GCS prefix."""
        from google.cloud import storage

        client = storage.Client(project=project)
        bucket_name, prefix = _parse_gcs_uri(processed_uri)
        bucket = client.bucket(bucket_name)

        def _upload(blob_name: str, content: str) -> None:
            bucket.blob(blob_name).upload_from_string(
                content.encode("utf-8"), content_type="application/json"
            )

        _upload(f"{prefix}/train.jsonl", _to_jsonl(self.train_rows))
        _upload(f"{prefix}/val.jsonl", _to_jsonl(self.val_rows))
        _upload(f"{prefix}/rejected.jsonl", _to_jsonl(self.rejected_rows))
        _upload(f"{prefix}/sample.jsonl", _to_jsonl(self.sample_rows))
        _upload(f"{prefix}/manifest.json", json.dumps(self.manifest(), indent=2))
        logger.info(
            "Wrote dataset to %s: train=%d val=%d rejected=%d",
            processed_uri,
            self.n_train,
            self.n_val,
            self.n_rejected,
        )

    def write_to_dir(self, output_dir: str) -> None:
        """Write all output files to a local directory."""
        base = Path(output_dir)
        base.mkdir(parents=True, exist_ok=True)
        (base / "train.jsonl").write_text(_to_jsonl(self.train_rows), encoding="utf-8")
        (base / "val.jsonl").write_text(_to_jsonl(self.val_rows), encoding="utf-8")
        (base / "rejected.jsonl").write_text(_to_jsonl(self.rejected_rows), encoding="utf-8")
        (base / "sample.jsonl").write_text(_to_jsonl(self.sample_rows), encoding="utf-8")
        (base / "manifest.json").write_text(
            json.dumps(self.manifest(), indent=2), encoding="utf-8"
        )
        logger.info(
            "Wrote dataset to %s: train=%d val=%d rejected=%d",
            output_dir,
            self.n_train,
            self.n_val,
            self.n_rejected,
        )


# ---------------------------------------------------------------------------
# DatasetValidator
# ---------------------------------------------------------------------------


class DatasetValidator:
    """Validates, deduplicates, and splits a list of JSONL row dicts.

    Parameters
    ----------
    data_format:
        ``"chat"`` or ``"instruct"``
    job_name:
        Used to derive the reproducible random seed for splitting (§5.1).
    train_split:
        Fraction of rows for training (e.g. 0.95).
    validation_split:
        Fraction of rows for validation (e.g. 0.05).
        Must equal 1.0 - train_split (validated in __init__).
    input_listing_sha256:
        SHA-256 of the raw input file listing — written into manifest.json.
    """

    def __init__(
        self,
        data_format: str = "chat",
        job_name: str = "job",
        train_split: float = 0.95,
        validation_split: float = 0.05,
        input_listing_sha256: str = "",
    ) -> None:
        if data_format not in ("chat", "instruct"):
            raise ValueError(f"data_format must be 'chat' or 'instruct', got {data_format!r}")
        self.data_format = data_format
        self.job_name = job_name
        self.train_split = train_split
        self.validation_split = validation_split
        self.input_listing_sha256 = input_listing_sha256
        self._validate_splits()

    def _validate_splits(self) -> None:
        total = round(self.train_split + self.validation_split, 10)
        if not math.isclose(total, 1.0, rel_tol=1e-6):
            raise DataValidationError(
                f"train_split ({self.train_split}) + validation_split "
                f"({self.validation_split}) = {total}, must equal 1.0"
            )

    # ------------------------------------------------------------------
    # Single-row validation
    # ------------------------------------------------------------------

    def validate_row(self, row: dict) -> tuple[bool, Optional[str]]:
        """Return (is_valid, reason_or_None).

        Validates encoding, schema, and non-empty content.
        """
        # Encoding guard: catch surrogates before Pydantic sees the data
        try:
            json.dumps(row, ensure_ascii=False).encode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            return False, f"invalid_encoding: {exc}"

        if self.data_format == "chat":
            try:
                ChatRow.model_validate(row)
            except Exception as exc:
                return False, f"schema_error: {exc}"
            # Check that user and assistant messages are non-empty
            messages = row.get("messages", [])
            for msg in messages:
                if not isinstance(msg.get("content", ""), str) or not msg["content"].strip():
                    return False, "empty_message_content"
        else:
            try:
                InstructRow.model_validate(row)
            except Exception as exc:
                return False, f"schema_error: {exc}"
            if not str(row.get("output", "")).strip():
                return False, "empty_output"

        return True, None

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def deduplicate(
        self, rows: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """Return (unique_rows, duplicate_rows).

        Deduplication key: SHA-256 of the serialised content (§5.4).
        First occurrence is kept.
        """
        seen: set[str] = set()
        unique: list[dict] = []
        dupes: list[dict] = []

        for row in rows:
            key = _row_content_hash(row, self.data_format)
            if key in seen:
                dupes.append({**row, "_reason": "duplicate_content"})
            else:
                seen.add(key)
                unique.append(row)

        if dupes:
            logger.info(
                "Removed %d duplicate rows (kept %d unique)", len(dupes), len(unique)
            )
        return unique, dupes

    # ------------------------------------------------------------------
    # Split
    # ------------------------------------------------------------------

    def split(
        self, rows: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """Split rows into (train, val) with a reproducible seed.

        Seed = int(sha256(job_name)[:8], 16) as specified in §5.1.
        """
        seed = int(hashlib.sha256(self.job_name.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        shuffled = list(rows)
        rng.shuffle(shuffled)

        n_train = int(len(shuffled) * self.train_split)
        train = shuffled[:n_train]
        val = shuffled[n_train:]
        return train, val

    # ------------------------------------------------------------------
    # Tokenizer validation (optional, best-effort)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_tokenizer(rows: list[dict], max_rows: int = 50) -> bool:
        """Return True if a sample of rows can be tokenised without error.

        Uses the default tokenizer for Llama 3.1 if available; returns True
        (pass) if the tokenizer is not installed.
        """
        try:
            from transformers import AutoTokenizer  # type: ignore[import]

            tokenizer = AutoTokenizer.from_pretrained(
                "unsloth/Meta-Llama-3.1-8B-Instruct", trust_remote_code=True
            )
            sample = rows[: min(max_rows, len(rows))]
            for row in sample:
                if "messages" in row:
                    text = tokenizer.apply_chat_template(
                        row["messages"], tokenize=False, add_generation_prompt=False
                    )
                else:
                    text = f"{row.get('instruction', '')} {row.get('input', '')} {row.get('output', '')}"
                tokenizer(text, truncation=True, max_length=4096)
            return True
        except ImportError:
            return True  # transformers not installed in data-prep env — skip
        except Exception as exc:
            logger.warning("Tokenizer validation failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        rows: list[dict],
        run_tokenizer_check: bool = False,
    ) -> ValidationResult:
        """Full validation pipeline: validate → dedup → split → sample.

        Raises DataValidationError for critical failures (§5.4):
          - No rows passed validation
          - All rows had empty text
          - n_train == 0 after splitting
        """
        if not rows:
            raise DataValidationError(
                "No input rows provided to the dataset validator."
            )

        valid_rows: list[dict] = []
        rejected_rows: list[dict] = []

        # Step 1: per-row validation
        for row in rows:
            ok, reason = self.validate_row(row)
            if ok:
                valid_rows.append(row)
            else:
                rejected_rows.append({**row, "_reason": reason})

        if not valid_rows:
            raise DataValidationError(
                "All rows were rejected during validation. "
                f"First reason: {rejected_rows[0].get('_reason') if rejected_rows else 'unknown'}"
            )

        # Check for all-empty-text condition
        if _all_empty_text(valid_rows, self.data_format):
            raise DataValidationError("all documents extracted to empty text")

        # Step 2: deduplication
        unique_rows, dupe_rows = self.deduplicate(valid_rows)
        rejected_rows.extend(dupe_rows)

        if not unique_rows:
            raise DataValidationError(
                "No unique rows remain after deduplication."
            )

        # Step 3: split
        train_rows, val_rows = self.split(unique_rows)

        if not train_rows:
            raise DataValidationError(
                f"empty training split: {len(unique_rows)} rows with "
                f"train_split={self.train_split} produced 0 training examples."
            )

        # Step 4: tokenizer check (optional)
        tokenizer_pass = True
        if run_tokenizer_check:
            tokenizer_pass = self._check_tokenizer(train_rows)

        # Step 5: sample
        sample_rows = train_rows[:_SAMPLE_SIZE]

        logger.info(
            "Validation complete: %d train, %d val, %d rejected (%d dupes)",
            len(train_rows),
            len(val_rows),
            len(rejected_rows),
            len(dupe_rows),
        )

        return ValidationResult(
            train_rows=train_rows,
            val_rows=val_rows,
            rejected_rows=rejected_rows,
            sample_rows=sample_rows,
            n_train=len(train_rows),
            n_val=len(val_rows),
            n_rejected=len(rejected_rows),
            n_duplicates=len(dupe_rows),
            data_format=self.data_format,
            job_name=self.job_name,
            input_listing_sha256=self.input_listing_sha256,
            tokenizer_validation_pass=tokenizer_pass,
            pipeline_version=PIPELINE_VERSION,
        )


# ---------------------------------------------------------------------------
# Standalone validation (called from data_prep_activity without a full run)
# ---------------------------------------------------------------------------


def validate_train_val_splits(train_split: float, validation_split: float) -> None:
    """Raise DataValidationError if splits don't sum to 1.0.

    Called at workflow request validation time, before Step 1 runs (§5.4).
    """
    total = round(train_split + validation_split, 10)
    if not math.isclose(total, 1.0, rel_tol=1e-6):
        raise DataValidationError(
            f"train_split ({train_split}) + validation_split ({validation_split}) "
            f"= {total}, must equal 1.0"
        )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _row_content_hash(row: dict, data_format: str) -> str:
    """SHA-256 of the row's training content (messages or instruction+output)."""
    if data_format == "chat":
        messages = row.get("messages", [])
        payload = json.dumps(
            [{"role": m.get("role", ""), "content": m.get("content", "")} for m in messages],
            ensure_ascii=False,
            sort_keys=True,
        )
    else:
        payload = json.dumps(
            {
                "instruction": row.get("instruction", ""),
                "input": row.get("input", ""),
                "output": row.get("output", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _all_empty_text(rows: list[dict], data_format: str) -> bool:
    """Return True only if EVERY row has empty text in the meaningful content field."""
    for row in rows:
        if data_format == "chat":
            messages = row.get("messages", [])
            if any(m.get("content", "").strip() for m in messages):
                return False
        else:
            if (
                str(row.get("instruction", "")).strip()
                or str(row.get("output", "")).strip()
            ):
                return False
    return True


def _to_jsonl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="Validate and split a training dataset")
    parser.add_argument("--input", required=True, help="Input JSONL (local or gs://...)")
    parser.add_argument("--output", required=True, help="Output directory (local or gs://...)")
    parser.add_argument("--format", choices=["chat", "instruct"], default="chat")
    parser.add_argument("--job-name", default="job", help="Job name (for reproducible seed)")
    parser.add_argument("--train-split", type=float, default=0.95)
    parser.add_argument("--validation-split", type=float, default=0.05)
    parser.add_argument("--tokenizer-check", action="store_true", help="Run tokenizer validation")
    return parser.parse_args()


def main() -> None:
    import sys

    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )
    args = _parse_args()

    # Read input
    input_path = args.input
    if input_path.startswith("gs://"):
        from google.cloud import storage

        bucket_name, blob_name = _parse_gcs_uri(input_path)
        data = storage.Client().bucket(bucket_name).blob(blob_name).download_as_bytes()
    else:
        data = Path(input_path).read_bytes()

    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]

    validator = DatasetValidator(
        data_format=args.format,
        job_name=args.job_name,
        train_split=args.train_split,
        validation_split=args.validation_split,
    )

    result = validator.run(rows, run_tokenizer_check=args.tokenizer_check)

    output = args.output
    if output.startswith("gs://"):
        result.write_to_gcs(output)
    else:
        result.write_to_dir(output)


if __name__ == "__main__":
    main()
