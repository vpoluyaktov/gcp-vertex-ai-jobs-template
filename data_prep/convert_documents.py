"""Document conversion: raw financial/investment documents → training JSONL rows.

Supports PDF, DOCX, HTML, and JSON source formats.  Produces rows in either:
  - chat format:    {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}
  - instruct format: {"instruction": ..., "input": ..., "output": ...}

Designed for financial domain documents: invoices, contracts, financial
statements, and investment reports.  The system prompt and extraction prompts
are kept generic enough to work across sub-domains; override via the
`system_prompt` parameter or by placing a custom `templates/system_prompt.txt`
alongside this file.

Usage (library)::

    converter = DocumentConverter(data_format="chat")
    rows = list(converter.convert_file("invoice.pdf"))

Usage (CLI)::

    python -m data_prep.convert_documents \\
        --input  gs://bucket/raw-documents/job1/ \\
        --output gs://bucket/processed-datasets/job1/ \\
        --format chat
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".html", ".htm", ".json", ".jsonl", ".txt", ".md"}
_DEFAULT_CHUNK_MAX_CHARS = 4096   # ≈ 1 024 tokens at ~4 chars/token
_DEFAULT_CHUNK_OVERLAP = 512      # ≈ 128 tokens overlap
_SAMPLE_SIZE = 5                  # rows written to sample.jsonl

_DEFAULT_SYSTEM_PROMPT = (
    Path(__file__).parent / "templates" / "system_prompt.txt"
)

# Financial-document extraction prompts keyed by detected document type
_DOMAIN_PROMPTS: dict[str, dict[str, str]] = {
    "invoice": {
        "user": (
            "Extract all key fields from the following invoice document and "
            "return them as a structured JSON object with fields: vendor, "
            "invoice_number, invoice_date, due_date, line_items (list), "
            "subtotal, tax, total, currency, payment_terms."
        ),
    },
    "contract": {
        "user": (
            "Analyse the following contract section and extract: parties "
            "involved, effective_date, key_obligations (list), termination "
            "conditions, governing_law.  Return as JSON."
        ),
    },
    "financial_statement": {
        "user": (
            "Extract the financial figures from the following statement section. "
            "Return a JSON with: period, revenue, net_income, total_assets, "
            "total_liabilities, equity.  Use null for missing fields."
        ),
    },
    "generic": {
        "user": (
            "Summarise the key information from the following document section "
            "and extract any structured data (dates, amounts, parties, "
            "identifiers) as a JSON object."
        ),
    },
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ExtractedDocument:
    """Raw text extracted from a single source file."""

    source_name: str        # original file name (no path)
    source_uri: str         # original GCS / local URI
    extension: str          # lowercased, e.g. ".pdf"
    text: str               # full extracted text (may be empty)
    sha256: str             # sha256 of `text`
    doc_type: str = "generic"  # heuristic classification


@dataclass
class Chunk:
    """A text fragment ready to become one training row."""

    text: str
    source_name: str
    chunk_index: int
    doc_type: str = "generic"
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(self.text.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _clean_text(text: str) -> str:
    """Normalise whitespace and remove control characters (keep newlines)."""
    # Replace null bytes and surrogates
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    # Normalise Unicode to NFC
    text = unicodedata.normalize("NFC", text)
    # Collapse excessive blank lines (>2 consecutive)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace per line
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()


def _extract_pdf(data: bytes) -> str:
    """Extract text from a PDF using pdfplumber; fall back to pypdf."""
    try:
        import pdfplumber  # type: ignore[import]

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n\n".join(p for p in pages if p.strip())
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("pdfplumber failed: %s — trying pypdf fallback", exc)

    try:
        import pypdf  # type: ignore[import]

        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n\n".join(
            page.extract_text() or "" for page in reader.pages
        )
    except ImportError:
        logger.warning("Neither pdfplumber nor pypdf is installed; PDF text will be empty")
    except Exception as exc:
        logger.warning("pypdf fallback also failed: %s", exc)

    return ""


def _extract_docx(data: bytes) -> str:
    """Extract text from a DOCX file using python-docx."""
    try:
        from docx import Document  # type: ignore[import]

        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    except ImportError:
        logger.warning("python-docx not installed; DOCX text will be empty")
    except Exception as exc:
        logger.warning("python-docx extraction failed: %s", exc)
    return ""


def _extract_html(data: bytes) -> str:
    """Extract readable text from HTML using BeautifulSoup."""
    try:
        from bs4 import BeautifulSoup  # type: ignore[import]

        soup = BeautifulSoup(data, "html.parser")
        # Remove script/style tags
        for tag in soup(["script", "style", "head", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator="\n")
    except ImportError:
        logger.warning("beautifulsoup4 not installed; falling back to raw decode")
    except Exception as exc:
        logger.warning("HTML extraction failed: %s", exc)
    return data.decode("utf-8", errors="replace")


def _extract_json(data: bytes) -> str:
    """Return a pretty-printed JSON string (or raw text if not valid JSON)."""
    text = data.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        return text


def _extract_text(data: bytes, extension: str) -> str:
    """Dispatch to the correct extractor based on file extension."""
    ext = extension.lower()
    if ext == ".pdf":
        return _extract_pdf(data)
    if ext in (".docx", ".doc"):
        return _extract_docx(data)
    if ext in (".html", ".htm"):
        return _extract_html(data)
    if ext in (".json", ".jsonl"):
        return _extract_json(data)
    # Plain text / Markdown — just decode
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Document type classifier
# ---------------------------------------------------------------------------


_INVOICE_RE = re.compile(
    r"\b(invoice|bill\s+to|amount\s+due|subtotal|payment\s+terms|due\s+date)\b",
    re.IGNORECASE,
)
_CONTRACT_RE = re.compile(
    r"\b(agreement|contract|terms\s+and\s+conditions|indemnif|governing\s+law|"
    r"party|parties|hereinafter|whereas)\b",
    re.IGNORECASE,
)
_FINANCIAL_RE = re.compile(
    r"\b(balance\s+sheet|income\s+statement|cash\s+flow|revenue|net\s+income|"
    r"total\s+assets|earnings|fiscal\s+year|quarterly)\b",
    re.IGNORECASE,
)


def _classify_doc_type(text: str) -> str:
    invoice_hits = len(_INVOICE_RE.findall(text))
    contract_hits = len(_CONTRACT_RE.findall(text))
    financial_hits = len(_FINANCIAL_RE.findall(text))
    scores = {"invoice": invoice_hits, "contract": contract_hits, "financial_statement": financial_hits}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else "generic"


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    max_chars: int = _DEFAULT_CHUNK_MAX_CHARS,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into overlapping chunks of at most `max_chars` characters.

    Strategy:
      1. Split on paragraph boundaries first.
      2. If a paragraph is longer than max_chars, split on sentence boundaries.
      3. If a sentence is still too long, hard-split on character count.

    Overlap is implemented by prepending the tail of the previous chunk.
    """
    if not text.strip():
        return []

    if len(text) <= max_chars:
        return [text]

    # Paragraph split
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush(carry_overlap: bool = True) -> None:
        nonlocal current, current_len
        chunk_text_val = "\n\n".join(current).strip()
        if chunk_text_val:
            chunks.append(chunk_text_val)
        # Carry overlap: keep last ~overlap chars of the current chunk
        if carry_overlap and chunks:
            tail = chunks[-1][-overlap:] if len(chunks[-1]) > overlap else chunks[-1]
            current = [tail]
            current_len = len(tail)
        else:
            current = []
            current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            # Sentence-level split for oversized paragraphs
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sent in sentences:
                if current_len + len(sent) + 2 > max_chars:
                    _flush()
                current.append(sent)
                current_len += len(sent) + 2
        else:
            if current_len + len(para) + 2 > max_chars:
                _flush()
            current.append(para)
            current_len += len(para) + 2

    if current:
        _flush(carry_overlap=False)

    return chunks or [text[:max_chars]]


# ---------------------------------------------------------------------------
# Row formatter
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    try:
        return _DEFAULT_SYSTEM_PROMPT.read_text().strip()
    except Exception:
        return "You are a helpful assistant specialising in financial document analysis."


def format_as_chat(
    chunk: Chunk,
    system_prompt: str,
    user_prompt_override: Optional[str] = None,
    assistant_response_override: Optional[str] = None,
) -> dict:
    """Produce a chat-format JSONL row from a text chunk.

    The `user` message contains the domain-specific extraction prompt followed
    by the raw chunk text.  The `assistant` message is either a provided
    override (for labelled data) or a placeholder extraction task description.
    """
    domain = _DOMAIN_PROMPTS.get(chunk.doc_type, _DOMAIN_PROMPTS["generic"])
    user_content = user_prompt_override or (
        f"{domain['user']}\n\n---\n\n{chunk.text}"
    )
    assistant_content = assistant_response_override or (
        f"[Extracted from {chunk.source_name}, chunk {chunk.chunk_index}]"
    )
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ]
    }


def format_as_instruct(
    chunk: Chunk,
    instruction_override: Optional[str] = None,
    output_override: Optional[str] = None,
) -> dict:
    """Produce an instruct-format JSONL row from a text chunk."""
    domain = _DOMAIN_PROMPTS.get(chunk.doc_type, _DOMAIN_PROMPTS["generic"])
    return {
        "instruction": instruction_override or domain["user"],
        "input": chunk.text,
        "output": output_override or "",
    }


# ---------------------------------------------------------------------------
# DocumentConverter — main public class
# ---------------------------------------------------------------------------


class DocumentConverter:
    """Convert raw documents to training JSONL rows.

    Parameters
    ----------
    data_format:
        ``"chat"`` or ``"instruct"``
    max_chunk_chars:
        Maximum characters per chunk (§5.4: chunks created when doc > max_seq_length × 4 chars)
    chunk_overlap:
        Character overlap between consecutive chunks
    system_prompt:
        Override system prompt for chat rows (default: from templates/system_prompt.txt)
    strict:
        If True, raise on unsupported file extensions instead of skipping
    """

    def __init__(
        self,
        data_format: str = "chat",
        max_chunk_chars: int = _DEFAULT_CHUNK_MAX_CHARS,
        chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP,
        system_prompt: Optional[str] = None,
        strict: bool = False,
    ) -> None:
        if data_format not in ("chat", "instruct"):
            raise ValueError(f"data_format must be 'chat' or 'instruct', got {data_format!r}")
        self.data_format = data_format
        self.max_chunk_chars = max_chunk_chars
        self.chunk_overlap = chunk_overlap
        self.system_prompt = system_prompt or _load_system_prompt()
        self.strict = strict

    # -------------------------------------------------------------------------
    # Core extraction + formatting pipeline
    # -------------------------------------------------------------------------

    def extract_document(self, data: bytes, filename: str, source_uri: str = "") -> ExtractedDocument:
        """Extract text from raw bytes, returning an ExtractedDocument."""
        ext = Path(filename).suffix.lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            if self.strict:
                raise ValueError(f"Unsupported extension {ext!r} for file {filename!r}")
            logger.warning("Skipping unsupported extension %r (%s)", ext, filename)
            return ExtractedDocument(
                source_name=filename,
                source_uri=source_uri,
                extension=ext,
                text="",
                sha256=_sha256(""),
                doc_type="generic",
            )

        raw_text = _extract_text(data, ext)
        text = _clean_text(raw_text)
        doc_type = _classify_doc_type(text)
        return ExtractedDocument(
            source_name=filename,
            source_uri=source_uri,
            extension=ext,
            text=text,
            sha256=_sha256(text),
            doc_type=doc_type,
        )

    def make_chunks(self, doc: ExtractedDocument) -> list[Chunk]:
        """Split an extracted document into Chunk objects."""
        texts = chunk_text(doc.text, self.max_chunk_chars, self.chunk_overlap)
        return [
            Chunk(
                text=t,
                source_name=doc.source_name,
                chunk_index=i,
                doc_type=doc.doc_type,
            )
            for i, t in enumerate(texts)
        ]

    def format_chunk(
        self,
        chunk: Chunk,
        user_prompt: Optional[str] = None,
        assistant_response: Optional[str] = None,
    ) -> dict:
        """Format a single Chunk into a JSONL row."""
        if self.data_format == "chat":
            return format_as_chat(
                chunk,
                system_prompt=self.system_prompt,
                user_prompt_override=user_prompt,
                assistant_response_override=assistant_response,
            )
        return format_as_instruct(
            chunk,
            instruction_override=user_prompt,
            output_override=assistant_response,
        )

    def convert_bytes(
        self,
        data: bytes,
        filename: str,
        source_uri: str = "",
    ) -> Iterator[dict]:
        """Full pipeline: bytes → JSONL row dicts.

        Yields one row per chunk.  Skips documents that extract to empty text.
        """
        doc = self.extract_document(data, filename, source_uri)
        if not doc.text:
            logger.debug("Empty extraction from '%s' — skipping", filename)
            return

        chunks = self.make_chunks(doc)
        if not chunks:
            return

        for chunk in chunks:
            yield self.format_chunk(chunk)

    def convert_file(self, path: str) -> Iterator[dict]:
        """Read a local file and yield JSONL rows."""
        data = Path(path).read_bytes()
        yield from self.convert_bytes(data, Path(path).name, source_uri=path)

    def convert_gcs_blob(
        self,
        blob,  # google.cloud.storage.Blob
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Iterator[dict]:
        """Download a GCS Blob and yield JSONL rows."""
        data = blob.download_as_bytes()
        yield from self.convert_bytes(data, Path(blob.name).name, source_uri=f"gs://{blob.bucket.name}/{blob.name}")


# ---------------------------------------------------------------------------
# Pre-labelled JSON/JSONL passthrough
# ---------------------------------------------------------------------------


def load_prelabelled_jsonl(data: bytes, data_format: str) -> tuple[list[dict], list[dict]]:
    """Parse a JSONL file that already contains labelled rows.

    Returns (valid_rows, rejected_rows_with_reason).
    Valid rows pass a loose format check (presence of expected keys).
    """
    valid: list[dict] = []
    rejected: list[dict] = []
    for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            rejected.append({"_line": lineno, "_reason": f"json_decode_error: {exc}", "_raw": line})
            continue

        if data_format == "chat":
            if "messages" not in row or not isinstance(row["messages"], list):
                rejected.append({**row, "_reason": "missing_messages_field"})
                continue
        elif data_format == "instruct":
            if not all(k in row for k in ("instruction", "output")):
                rejected.append({**row, "_reason": "missing_instruction_or_output"})
                continue
        valid.append(row)

    return valid, rejected


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert raw documents to training JSONL rows"
    )
    parser.add_argument("--input", required=True, help="Input directory or GCS URI (gs://...)")
    parser.add_argument("--output", required=True, help="Output file path or GCS URI")
    parser.add_argument(
        "--format",
        choices=["chat", "instruct"],
        default="chat",
        help="Output JSONL format (default: chat)",
    )
    parser.add_argument(
        "--max-chunk-chars",
        type=int,
        default=_DEFAULT_CHUNK_MAX_CHARS,
        help="Max characters per chunk",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="Override system prompt for chat format",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on unsupported file extensions instead of skipping",
    )
    return parser.parse_args()


def main() -> None:
    import sys

    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )
    args = _parse_args()
    converter = DocumentConverter(
        data_format=args.format,
        max_chunk_chars=args.max_chunk_chars,
        system_prompt=args.system_prompt,
        strict=args.strict,
    )

    rows: list[dict] = []
    input_path = args.input

    if input_path.startswith("gs://"):
        from google.cloud import storage

        uri = input_path.removeprefix("gs://")
        bucket_name, _, prefix = uri.partition("/")
        client = storage.Client()
        blobs = list(client.list_blobs(bucket_name, prefix=prefix.rstrip("/") + "/"))
        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            logger.info("Processing %s", blob.name)
            rows.extend(converter.convert_gcs_blob(blob))
    else:
        for fp in sorted(Path(input_path).rglob("*")):
            if fp.is_file() and fp.suffix.lower() in _SUPPORTED_EXTENSIONS:
                logger.info("Processing %s", fp)
                rows.extend(converter.convert_file(str(fp)))

    output = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)

    if args.output.startswith("gs://"):
        from google.cloud import storage

        uri = args.output.removeprefix("gs://")
        bucket_name, _, blob_name = uri.partition("/")
        storage.Client().bucket(bucket_name).blob(blob_name).upload_from_string(
            output.encode("utf-8"), content_type="application/jsonl"
        )
    else:
        Path(args.output).write_text(output, encoding="utf-8")

    logger.info("Written %d rows to %s", len(rows), args.output)


if __name__ == "__main__":
    main()
