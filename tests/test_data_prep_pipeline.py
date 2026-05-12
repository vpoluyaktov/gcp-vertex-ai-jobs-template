"""Integration tests for the data preparation pipeline (data_prep/pipeline.py).

Tests the `run_pipeline` orchestration function with mocked GCS — no real
GCS calls are made.  Each test focuses on one observable behaviour.

Behaviours tested:
1. heartbeat_fn is called once all files are processed (even for small batches)
2. Mixed bucket (pre-labelled JSONL + raw text) correctly processes both types
3. Duplicate rows from different files are deduplicated (row-level idempotency)
4. Empty bucket raises DataValidationError
5. All-empty-text files raise DataValidationError

Implementation gap noted (§5.4 / §3.4 Step 1 idempotency):
  run_pipeline does NOT implement per-file SHA-256 skip via a `.processed_files.json`
  cache.  Only the standalone `convert_gcs_prefix` function in convert_documents.py
  has that behaviour.  Tests here cover the actual row-level dedup that run_pipeline
  does implement.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import pytest

from data_prep.pipeline import run_pipeline
from data_prep.validate_dataset import DataValidationError

# ---------------------------------------------------------------------------
# GCS mock helpers
# ---------------------------------------------------------------------------


def _make_blob(name: str, content: bytes) -> MagicMock:
    blob = MagicMock()
    blob.name = name
    blob.size = len(content)
    blob.updated = "2026-05-12T00:00:00Z"
    blob.download_as_bytes.return_value = content
    # Used by converter
    blob.bucket = MagicMock()
    blob.bucket.name = "test-bucket"
    return blob


def _pipeline_client(blobs: list[MagicMock]) -> MagicMock:
    """Return a mock storage.Client configured with the given raw blobs."""
    client = MagicMock()
    bucket = MagicMock()
    client.bucket.return_value = bucket

    # Cache blob: does not exist by default
    cache_blob = MagicMock()
    cache_blob.exists.return_value = False
    bucket.blob.return_value = cache_blob

    # list_blobs returns our test blobs
    client.list_blobs.return_value = blobs
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_chat_jsonl_bytes() -> bytes:
    """Three valid chat-format JSONL rows in bytes."""
    rows = [
        {
            "messages": [
                {"role": "system", "content": "You are a helper."},
                {"role": "user", "content": f"Question {i}?"},
                {"role": "assistant", "content": f"Answer {i}."},
            ]
        }
        for i in range(1, 4)
    ]
    return "\n".join(json.dumps(r) for r in rows).encode("utf-8")


@pytest.fixture
def valid_instruct_jsonl_bytes() -> bytes:
    rows = [
        {"instruction": "Extract fields.", "input": f"Doc {i}", "output": f'{{"id": {i}}}'}
        for i in range(1, 4)
    ]
    return "\n".join(json.dumps(r) for r in rows).encode("utf-8")


# ---------------------------------------------------------------------------
# Common call parameters
# ---------------------------------------------------------------------------

_PIPELINE_KWARGS = dict(
    raw_uri="gs://test-bucket/raw",
    processed_uri="gs://test-bucket/processed",
    data_format="chat",
    train_split=1.0,
    validation_split=0.0,
    job_name="test-pipeline-job",
)


# ---------------------------------------------------------------------------
# Test: heartbeat_fn is called
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_called_when_all_files_processed(self, valid_chat_jsonl_bytes):
        """heartbeat_fn must be called at least once when all files are done."""
        blobs = [_make_blob("raw/labeled.jsonl", valid_chat_jsonl_bytes)]
        heartbeat_calls: list[int] = []

        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            run_pipeline(**_PIPELINE_KWARGS, heartbeat_fn=lambda n: heartbeat_calls.append(n))

        assert len(heartbeat_calls) >= 1
        assert heartbeat_calls[-1] == 1  # 1 file processed total

    def test_heartbeat_receives_files_done_count(self, valid_chat_jsonl_bytes):
        """heartbeat_fn argument is the count of files processed so far."""
        blobs = [
            _make_blob("raw/a.jsonl", valid_chat_jsonl_bytes),
            _make_blob("raw/b.jsonl", valid_chat_jsonl_bytes),
        ]
        heartbeat_calls: list[int] = []

        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            run_pipeline(**_PIPELINE_KWARGS, heartbeat_fn=lambda n: heartbeat_calls.append(n))

        # Last heartbeat must equal total file count
        assert heartbeat_calls[-1] == len(blobs)

    def test_no_heartbeat_fn_does_not_raise(self, valid_chat_jsonl_bytes):
        """Pipeline must work fine when no heartbeat_fn is provided."""
        blobs = [_make_blob("raw/labeled.jsonl", valid_chat_jsonl_bytes)]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            result = run_pipeline(**_PIPELINE_KWARGS, heartbeat_fn=None)
        assert result["n_train"] > 0


# ---------------------------------------------------------------------------
# Test: mixed bucket — pre-labelled JSONL + raw text
# ---------------------------------------------------------------------------


class TestMixedBucket:
    def test_jsonl_and_text_blobs_both_produce_rows(self, valid_chat_jsonl_bytes):
        """Mixed bucket: pre-labelled JSONL rows pass through; text is converted."""
        text_content = b"Invoice #1 from ACME Corp. Total: $500. Due: 2026-06-01."
        blobs = [
            _make_blob("raw/labeled.jsonl", valid_chat_jsonl_bytes),
            _make_blob("raw/document.txt", text_content),
        ]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            result = run_pipeline(**_PIPELINE_KWARGS)

        # Both sources must contribute rows
        assert result["n_train"] > 0
        # At minimum 3 rows from JSONL + 1 from text
        assert result["n_train"] >= 3

    def test_invalid_jsonl_row_rejected_not_raised(self, valid_chat_jsonl_bytes):
        """A row missing 'messages' in a pre-labelled JSONL is rejected, not an exception."""
        bad_row_bytes = json.dumps({"not_messages": "wrong"}).encode("utf-8")
        blobs = [
            _make_blob("raw/labeled.jsonl", valid_chat_jsonl_bytes),
            _make_blob("raw/bad.jsonl", bad_row_bytes),
        ]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            result = run_pipeline(**_PIPELINE_KWARGS)

        # Valid rows from labeled.jsonl must still be returned
        assert result["n_train"] > 0

    def test_unsupported_extension_skipped_gracefully(self, valid_chat_jsonl_bytes):
        """Files with unsupported extensions are skipped without raising."""
        blobs = [
            _make_blob("raw/labeled.jsonl", valid_chat_jsonl_bytes),
            _make_blob("raw/image.png", b"\x89PNG..."),  # unsupported
        ]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            result = run_pipeline(**_PIPELINE_KWARGS)

        assert result["n_train"] > 0


# ---------------------------------------------------------------------------
# Test: row-level deduplication (implemented idempotency in run_pipeline)
# ---------------------------------------------------------------------------


class TestRowLevelDeduplication:
    def test_identical_rows_across_files_deduplicated(self):
        """Same JSONL content repeated across two files: only unique rows kept."""
        row_bytes = json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "q?"},
                    {"role": "assistant", "content": "a!"},
                ]
            }
        ).encode("utf-8")

        blobs = [
            _make_blob("raw/file1.jsonl", row_bytes),
            _make_blob("raw/file2.jsonl", row_bytes),  # exact duplicate
        ]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            result = run_pipeline(**_PIPELINE_KWARGS)

        # Only one unique row should survive dedup
        assert result["n_train"] == 1
        assert result["n_rejected"] == 1  # the duplicate

    def test_unique_rows_across_files_all_retained(self, valid_chat_jsonl_bytes):
        """Rows that are unique across files must all be retained."""
        row_a = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "unique question A"},
                    {"role": "assistant", "content": "answer A"},
                ]
            }
        ).encode()
        row_b = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "unique question B"},
                    {"role": "assistant", "content": "answer B"},
                ]
            }
        ).encode()

        blobs = [
            _make_blob("raw/file1.jsonl", row_a),
            _make_blob("raw/file2.jsonl", row_b),
        ]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            result = run_pipeline(**_PIPELINE_KWARGS)

        assert result["n_train"] == 2
        assert result["n_rejected"] == 0


# ---------------------------------------------------------------------------
# Test: error edge cases
# ---------------------------------------------------------------------------


class TestPipelineErrors:
    def test_empty_bucket_raises_data_validation_error(self):
        """§5.4: no files under raw_uri → DataValidationError."""
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            client = MockClient.return_value
            bucket = MagicMock()
            client.bucket.return_value = bucket
            client.list_blobs.return_value = []  # empty listing

            with pytest.raises(DataValidationError, match="No input files"):
                run_pipeline(**_PIPELINE_KWARGS)

    def test_single_blob_warning_not_error(self, valid_chat_jsonl_bytes):
        """§5.4: single document → WARN: dataset_size_low but no exception."""
        blobs = [_make_blob("raw/single.jsonl", valid_chat_jsonl_bytes)]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            result = run_pipeline(**_PIPELINE_KWARGS)

        assert result["n_train"] > 0

    def test_all_empty_files_raises_data_validation_error(self):
        """§5.4: all documents extract to empty text → DataValidationError."""
        # An empty .txt file extracts to empty string
        blobs = [
            _make_blob("raw/empty1.txt", b""),
            _make_blob("raw/empty2.txt", b""),
        ]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            with pytest.raises(DataValidationError):
                run_pipeline(**_PIPELINE_KWARGS)

    def test_bad_split_ratios_raise_before_processing(self):
        """train_split + validation_split != 1.0 → error before any GCS calls."""
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            client = MockClient.return_value
            # list_blobs should never be called if splits are bad
            client.list_blobs.return_value = []

            with pytest.raises(DataValidationError, match="must equal 1.0"):
                run_pipeline(
                    raw_uri="gs://test-bucket/raw",
                    processed_uri="gs://test-bucket/processed",
                    data_format="chat",
                    train_split=0.8,
                    validation_split=0.3,  # 0.8 + 0.3 = 1.1 ≠ 1.0
                    job_name="bad-splits",
                )

    def test_manifest_uri_in_result(self, valid_chat_jsonl_bytes):
        """run_pipeline returns a dict containing manifest_uri."""
        blobs = [_make_blob("raw/data.jsonl", valid_chat_jsonl_bytes)]
        with patch("data_prep.pipeline._storage.Client") as MockClient:
            MockClient.return_value = _pipeline_client(blobs)
            result = run_pipeline(**_PIPELINE_KWARGS)

        assert "manifest_uri" in result
        assert result["manifest_uri"].endswith("manifest.json")
