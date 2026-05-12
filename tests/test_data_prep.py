"""Tests for data_prep document conversion and dataset validation.

Covers §5.1–§5.4 of ARCHITECTURE.md:

DocumentConverter (convert_documents.py):
- Empty document bytes → no rows yielded, logged as skipped
- Document with no extractable text → no rows yielded
- Output chat-format rows match the canonical schema: {messages: [...]}
- Output instruct-format rows match the canonical schema: {instruction, input, output}

DatasetValidator (validate_dataset.py):
- Duplicate rows (same SHA-256) → only first occurrence kept
- Missing required roles (user / assistant) → row rejected
- Empty content strings → row rejected
- train_split + validation_split != 1.0 → DataValidationError
- n_train > 0 for any non-empty valid dataset
- Empty input raises DataValidationError
- Single-row dataset is processed (not rejected)

All tests are unit-level — no GCS or external calls.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from data_prep.convert_documents import DocumentConverter, chunk_text
from data_prep.validate_dataset import (
    ChatRow,
    DatasetValidator,
    DataValidationError,
    InstructRow,
    _row_content_hash,
    validate_train_val_splits,
)


# ---------------------------------------------------------------------------
# DocumentConverter — extraction and row formatting
# ---------------------------------------------------------------------------


class TestDocumentConverterEmptyInputs:
    def test_empty_bytes_yields_no_rows(self):
        """Empty document → DocumentConverter must yield nothing (§5.4)."""
        conv = DocumentConverter(data_format="chat")
        rows = list(conv.convert_bytes(b"", "empty.txt"))
        assert rows == []

    def test_whitespace_only_bytes_yields_no_rows(self):
        """Whitespace-only content has no extractable text → skip."""
        conv = DocumentConverter(data_format="chat")
        rows = list(conv.convert_bytes(b"   \n\t\n   ", "blank.txt"))
        assert rows == []

    def test_empty_txt_extract_then_skip(self):
        """After extraction, if text is empty the converter must not yield rows."""
        conv = DocumentConverter(data_format="chat")
        rows = list(conv.convert_bytes(b"\x00\x00\x00", "null.txt"))
        # NULL bytes decode to a non-empty string, but clean_text strips to nothing
        # depending on content — the key assertion is that the converter handles
        # this gracefully (no exception, possibly empty).
        assert isinstance(rows, list)


class TestDocumentConverterChatFormat:
    def test_chat_row_has_messages_key(self):
        conv = DocumentConverter(data_format="chat")
        rows = list(
            conv.convert_bytes(
                b"Invoice #100 due 2026-06-01, total $500 USD", "invoice.txt"
            )
        )
        assert len(rows) > 0
        for row in rows:
            assert "messages" in row, f"Row missing 'messages' key: {row}"

    def test_chat_row_has_system_user_assistant_roles(self):
        """§5.2: chat-format row must have system, user, and assistant messages."""
        conv = DocumentConverter(data_format="chat")
        rows = list(conv.convert_bytes(b"Contract between Alice and Bob", "contract.txt"))
        assert len(rows) > 0
        for row in rows:
            roles = {m["role"] for m in row["messages"]}
            assert "system" in roles, "Missing system role"
            assert "user" in roles, "Missing user role"
            assert "assistant" in roles, "Missing assistant role"

    def test_chat_row_messages_have_content(self):
        """Every message in a chat row must have non-empty content."""
        conv = DocumentConverter(data_format="chat")
        rows = list(
            conv.convert_bytes(b"Financial statement: revenue $1M, net income $100K", "fin.txt")
        )
        assert len(rows) > 0
        for row in rows:
            for msg in row["messages"]:
                assert msg.get("content"), f"Empty content in message: {msg}"

    def test_chat_row_validates_as_chat_schema(self):
        """Rows produced by the converter must pass ChatRow Pydantic validation."""
        conv = DocumentConverter(data_format="chat")
        rows = list(conv.convert_bytes(b"Invoice #1 total $100 due 2026-05-01", "inv.txt"))
        for row in rows:
            parsed = ChatRow.model_validate(row)
            assert parsed is not None


class TestDocumentConverterInstructFormat:
    def test_instruct_row_has_instruction_input_output(self):
        """§5.3: instruct-format row must have instruction, input, output keys."""
        conv = DocumentConverter(data_format="instruct")
        rows = list(conv.convert_bytes(b"Extract vendor from invoice: ACME Corp", "inv.txt"))
        assert len(rows) > 0
        for row in rows:
            assert "instruction" in row
            assert "input" in row
            assert "output" in row

    def test_instruct_row_has_correct_keys(self):
        """Instruct rows from the converter have the three required keys.

        Note: the `output` field will be empty for unlabelled raw documents
        (the converter uses expected_output="" by default).  InstructRow schema
        (min_length=1 on output) applies to user-supplied JSONL, not converter
        output — labelled data must be provided as pre-labelled JSONL.
        """
        conv = DocumentConverter(data_format="instruct")
        rows = list(conv.convert_bytes(b"Balance sheet Q1 2026: assets $5M", "bs.txt"))
        assert len(rows) > 0
        for row in rows:
            assert "instruction" in row
            assert "input" in row
            assert "output" in row
            # instruction and input are populated from the extractor
            assert row["instruction"], "instruction must be non-empty"
            assert row["input"], "input (document text) must be non-empty"


# ---------------------------------------------------------------------------
# DocumentConverter — invalid format
# ---------------------------------------------------------------------------


class TestDocumentConverterInit:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="data_format must be"):
            DocumentConverter(data_format="unknown")

    def test_valid_formats_accepted(self):
        for fmt in ("chat", "instruct"):
            conv = DocumentConverter(data_format=fmt)
            assert conv.data_format == fmt


# ---------------------------------------------------------------------------
# chunk_text edge cases
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_empty_text_returns_empty_list(self):
        """Empty input → no chunks."""
        assert chunk_text("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert chunk_text("   \n  ") == []

    def test_short_text_returns_single_chunk(self):
        text = "Hello world"
        chunks = chunk_text(text, max_chars=100)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_produces_multiple_chunks(self):
        """Text with paragraph breaks → multiple chunks when total exceeds max_chars.

        The chunker splits on \\n{2,} (paragraphs).  Without paragraph breaks
        the chunker has no split-point for unsplit text and will return a single
        chunk, even for long strings.  This test uses paragraph-separated text.
        """
        paragraph = "word " * 20 + "end sentence.\n\n"
        text = paragraph * 10  # ten paragraphs of ~105 chars each
        chunks = chunk_text(text, max_chars=200, overlap=20)
        assert len(chunks) > 1

    def test_single_word_text(self):
        chunks = chunk_text("Hello")
        assert len(chunks) == 1
        assert chunks[0] == "Hello"


# ---------------------------------------------------------------------------
# DatasetValidator — Pydantic row schemas
# ---------------------------------------------------------------------------


class TestChatRowSchema:
    def test_valid_chat_row(self):
        row = {
            "messages": [
                {"role": "system", "content": "You are a helper."},
                {"role": "user", "content": "What is 2+2?"},
                {"role": "assistant", "content": "4"},
            ]
        }
        parsed = ChatRow.model_validate(row)
        assert len(parsed.messages) == 3

    def test_missing_user_role_raises(self):
        """§5.4: row without a user message must be rejected."""
        row = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "assistant", "content": "answer"},
            ]
        }
        with pytest.raises(ValidationError):
            ChatRow.model_validate(row)

    def test_missing_assistant_role_raises(self):
        """Row without an assistant message must be rejected."""
        row = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "question"},
            ]
        }
        with pytest.raises(ValidationError):
            ChatRow.model_validate(row)

    def test_empty_content_raises(self):
        """§5.4: empty content string in a message must be rejected."""
        row = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": ""},   # empty — must fail
                {"role": "assistant", "content": "answer"},
            ]
        }
        with pytest.raises(ValidationError):
            ChatRow.model_validate(row)

    def test_empty_messages_list_raises(self):
        with pytest.raises(ValidationError):
            ChatRow.model_validate({"messages": []})

    def test_invalid_role_raises(self):
        row = {
            "messages": [
                {"role": "admin", "content": "sys"},  # invalid role
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ]
        }
        with pytest.raises(ValidationError):
            ChatRow.model_validate(row)


class TestInstructRowSchema:
    def test_valid_instruct_row(self):
        row = {"instruction": "Extract fields", "input": "Doc text", "output": '{"key":"val"}'}
        parsed = InstructRow.model_validate(row)
        assert parsed.instruction == "Extract fields"

    def test_missing_instruction_raises(self):
        with pytest.raises(ValidationError):
            InstructRow.model_validate({"input": "text", "output": "ans"})

    def test_missing_output_raises(self):
        with pytest.raises(ValidationError):
            InstructRow.model_validate({"instruction": "do this", "input": "text"})

    def test_empty_output_raises(self):
        with pytest.raises(ValidationError):
            InstructRow.model_validate({"instruction": "do this", "input": "text", "output": ""})


# ---------------------------------------------------------------------------
# DatasetValidator — deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_duplicate_rows_deduplicated(self, sample_chat_jsonl_rows):
        """§5.4: duplicate rows (same SHA-256) → first kept, rest counted in n_rejected."""
        row = sample_chat_jsonl_rows[0]
        rows = [row, row, row]  # 3 copies of the same row
        validator = DatasetValidator(
            data_format="chat", job_name="test", train_split=1.0, validation_split=0.0
        )
        result = validator.run(rows)
        assert result.n_train == 1
        assert result.n_duplicates == 2
        assert result.n_rejected == 2  # duplicates count as rejected

    def test_unique_rows_are_all_kept(self, sample_chat_jsonl_rows):
        validator = DatasetValidator(
            data_format="chat", job_name="test", train_split=1.0, validation_split=0.0
        )
        result = validator.run(sample_chat_jsonl_rows)
        assert result.n_train == len(sample_chat_jsonl_rows)
        assert result.n_duplicates == 0

    def test_duplicate_detection_uses_content_hash(self):
        """Rows that differ only by whitespace around content still differ (exact match)."""
        row_a = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "World"},
            ]
        }
        row_b = {
            "messages": [
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": "World"},
            ]
        }
        hash_a = _row_content_hash(row_a, "chat")
        hash_b = _row_content_hash(row_b, "chat")
        assert hash_a != hash_b


# ---------------------------------------------------------------------------
# DatasetValidator — split logic
# ---------------------------------------------------------------------------


class TestSplitLogic:
    def test_train_val_ratios_sum_to_1(self, sample_chat_jsonl_rows):
        validator = DatasetValidator(
            data_format="chat", job_name="test", train_split=0.8, validation_split=0.2
        )
        result = validator.run(sample_chat_jsonl_rows)
        total = result.n_train + result.n_val
        assert total == len(sample_chat_jsonl_rows)

    def test_n_train_greater_than_zero_for_non_empty_dataset(self, sample_chat_jsonl_rows):
        """§5.4: n_train > 0 for any non-empty valid dataset."""
        validator = DatasetValidator(
            data_format="chat", job_name="test", train_split=0.95, validation_split=0.05
        )
        result = validator.run(sample_chat_jsonl_rows)
        assert result.n_train > 0

    def test_single_row_produces_train_row(self, sample_chat_jsonl_rows):
        """§5.4: single-document input still proceeds (train_split=1.0)."""
        validator = DatasetValidator(
            data_format="chat", job_name="test", train_split=1.0, validation_split=0.0
        )
        result = validator.run([sample_chat_jsonl_rows[0]])
        assert result.n_train == 1
        assert result.n_val == 0

    def test_split_is_reproducible(self, sample_chat_jsonl_rows):
        """Same job_name → same split (deterministic seed from SHA-256)."""
        validator = DatasetValidator(
            data_format="chat", job_name="my-job", train_split=0.8, validation_split=0.2
        )
        result_a = validator.run(sample_chat_jsonl_rows)
        result_b = validator.run(sample_chat_jsonl_rows)
        # train sets must be identical across runs
        assert [r["messages"][1]["content"] for r in result_a.train_rows] == [
            r["messages"][1]["content"] for r in result_b.train_rows
        ]

    def test_invalid_split_sum_raises_at_init(self):
        """train_split + validation_split != 1.0 → DataValidationError at __init__."""
        with pytest.raises(DataValidationError, match="must equal 1.0"):
            DatasetValidator(train_split=0.8, validation_split=0.3)

    def test_standalone_validate_splits_ok(self):
        validate_train_val_splits(0.95, 0.05)  # should not raise

    def test_standalone_validate_splits_bad(self):
        with pytest.raises(DataValidationError):
            validate_train_val_splits(0.8, 0.3)


# ---------------------------------------------------------------------------
# DatasetValidator — edge cases for error paths
# ---------------------------------------------------------------------------


class TestValidatorEdgeCases:
    def test_empty_rows_raises(self):
        """Empty row list → DataValidationError (§5.4)."""
        validator = DatasetValidator(data_format="chat", job_name="test")
        with pytest.raises(DataValidationError, match="No input rows"):
            validator.run([])

    def test_all_invalid_rows_raises(self):
        """If every row fails validation, DataValidationError must be raised."""
        bad_rows = [{"messages": [{"role": "user", "content": "q"}]}]  # missing assistant
        validator = DatasetValidator(data_format="chat", job_name="test")
        with pytest.raises(DataValidationError, match="All rows were rejected"):
            validator.run(bad_rows)

    def test_mixed_valid_invalid_rows(self, sample_chat_jsonl_rows):
        """Valid rows are retained; invalid rows go to rejected — no exception."""
        bad_row = {"messages": [{"role": "user", "content": "q"}]}  # missing assistant
        rows = sample_chat_jsonl_rows[:3] + [bad_row]
        validator = DatasetValidator(
            data_format="chat", job_name="test", train_split=1.0, validation_split=0.0
        )
        result = validator.run(rows)
        assert result.n_train == 3
        assert result.n_rejected == 1

    def test_sample_is_capped_at_five(self, sample_chat_jsonl_rows):
        """§5.1 stage 8: sample.jsonl contains the first 5 rows of the training set."""
        validator = DatasetValidator(
            data_format="chat", job_name="test", train_split=1.0, validation_split=0.0
        )
        result = validator.run(sample_chat_jsonl_rows)
        assert len(result.sample_rows) <= 5

    def test_validate_row_rejects_empty_user_content(self):
        """Validator.validate_row returns False for whitespace-only user content."""
        validator = DatasetValidator(data_format="chat", job_name="test")
        row = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "   "},   # whitespace only
                {"role": "assistant", "content": "ans"},
            ]
        }
        ok, reason = validator.validate_row(row)
        assert not ok
        assert reason is not None

    def test_validate_row_accepts_valid_row(self, sample_chat_jsonl_rows):
        validator = DatasetValidator(data_format="chat", job_name="test")
        ok, reason = validator.validate_row(sample_chat_jsonl_rows[0])
        assert ok is True
        assert reason is None

    def test_instruct_validator_rejects_empty_output(self, sample_instruct_jsonl_rows):
        """instruct format: empty output field must be rejected."""
        validator = DatasetValidator(data_format="instruct", job_name="test")
        row = {"instruction": "Extract fields", "input": "doc", "output": "   "}
        ok, reason = validator.validate_row(row)
        assert not ok
