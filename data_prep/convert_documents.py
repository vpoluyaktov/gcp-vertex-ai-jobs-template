"""Document conversion: raw financial/investment documents → training JSONL rows.

Supports PDF, DOCX, HTML, plain-text, and JSON source formats.
Produces rows in either:
  - chat format:     {"messages": [{"role": "system",...}, {"role": "user",...}, {"role": "assistant",...}]}
  - instruct format: {"instruction":..., "input":..., "output":...}

Row construction delegates to ``data_prep/templates/instruction_template.jinja2``
so the prompt shape can be changed without touching Python code.

GCS integration
---------------
When run as a CLI the script reads environment variables:
  GCS_RAW_DOCUMENTS_BUCKET      — source bucket (required)
  GCS_PROCESSED_DATASETS_BUCKET — output bucket (required)
  GCP_PROJECT_ID                — optional GCP project

Idempotency
-----------
A ``.processed_files.json`` manifest is maintained in the output prefix.
Each entry records the SHA-256 of the source file's content.  On re-runs,
files whose SHA-256 is already present are skipped, so only new or changed
documents are converted.  This mirrors the listing-level idempotency in
``temporal/activities/data_prep_activity.py``.

Usage (library)::

    converter = DocumentConverter(data_format="chat")
    rows = list(converter.convert_file("invoice.pdf"))

Usage (CLI)::

    python -m data_prep.convert_documents \\
        --input  gs://bucket/raw-documents/job1/ \\
        --output gs://bucket/processed-datasets/job1/raw.jsonl \\
        --format chat \\
        --job-name job1
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
from typing import Callable, Iterator, Optional

import jinja2

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_SYSTEM_PROMPT_FILE = _TEMPLATES_DIR / "system_prompt.txt"
_INSTRUCTION_TEMPLATE = "instruction_template.jinja2"

# ---------------------------------------------------------------------------
# Extension sets
# ---------------------------------------------------------------------------

_SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc",
    ".html", ".htm",
    ".json", ".jsonl",
    ".txt", ".md",
}

# ---------------------------------------------------------------------------
# Per-document-type extraction tasks (user-turn instructions)
# ---------------------------------------------------------------------------

DOMAIN_TASKS: dict[str, str] = {
    "invoice": (
        "Extract all key fields from the following invoice and return them as a "
        "JSON object with fields: vendor, invoice_number, invoice_date, due_date, "
        "line_items (list of {description, quantity, unit_price, total}), "
        "subtotal, tax, total, currency, payment_terms. "
        "Set missing fields to null."
    ),
    "contract": (
        "Analyse the following contract section and extract: parties (list), "
        "effective_date, expiry_date, key_obligations (list), termination_conditions, "
        "governing_law, jurisdiction. Return as JSON."
    ),
    "financial_statement": (
        "Extract the financial figures from the following statement section. "
        "Return JSON with: period, revenue, cost_of_revenue, gross_profit, "
        "operating_expenses, net_income, total_assets, total_liabilities, equity, "
        "cash_and_equivalents. Use null for absent fields."
    ),
    "generic": (
        "Extract all named entities, dates, monetary amounts, and key facts from "
        "the following document section. Return as a structured JSON object."
    ),
}

# Chunk size defaults
_DEFAULT_CHUNK_MAX_CHARS = 4096   # ≈ 1 024 tokens at 4 chars/token
_DEFAULT_CHUNK_OVERLAP = 512


# ---------------------------------------------------------------------------
# Jinja2 environment
# ---------------------------------------------------------------------------


def _build_jinja_env() -> jinja2.Environment:
    loader = jinja2.FileSystemLoader(str(_TEMPLATES_DIR))
    env = jinja2.Environment(
        loader=loader,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    return env


_JINJA_ENV = _build_jinja_env()


def _render_row(
    data_format: str,
    system_prompt: str,
    extraction_task: str,
    document_text: str,
    source_name: str,
    expected_output: str = "",
) -> dict:
    """Render one training row via the Jinja2 template."""
    tmpl = _JINJA_ENV.get_template(_INSTRUCTION_TEMPLATE)
    rendered = tmpl.render(
        format=data_format,
        system_prompt=system_prompt,
        extraction_task=extraction_task,
        document_text=document_text,
        source_name=source_name,
        expected_output=expected_output,
    ).strip()
    return json.loads(rendered)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ExtractedDocument:
    source_name: str
    source_uri: str
    extension: str
    text: str
    sha256: str
    doc_type: str = "generic"


@dataclass
class Chunk:
    text: str
    source_name: str
    chunk_index: int
    doc_type: str = "generic"
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(
            self.text.encode("utf-8", errors="replace")
        ).hexdigest()


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


def _clean_text(text: str) -> str:
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber  # type: ignore[import]
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n\n".join(p.extract_text() or "" for p in pdf.pages).strip()
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("pdfplumber failed: %s — trying pypdf", exc)
    try:
        import pypdf  # type: ignore[import]
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        logger.warning("Neither pdfplumber nor pypdf is installed; PDF text will be empty")
    except Exception as exc:
        logger.warning("pypdf fallback failed: %s", exc)
    return ""


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document  # type: ignore[import]
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    except ImportError:
        logger.warning("python-docx not installed; DOCX text will be empty")
    except Exception as exc:
        logger.warning("python-docx failed: %s", exc)
    return ""


def _extract_html(data: bytes) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore[import]
        soup = BeautifulSoup(data, "html.parser")
        for tag in soup(["script", "style", "head", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator="\n")
    except ImportError:
        logger.warning("beautifulsoup4 not installed; falling back to raw decode")
    except Exception as exc:
        logger.warning("HTML extraction failed: %s", exc)
    return data.decode("utf-8", errors="replace")


def _extract_json(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        return text


def _extract_text(data: bytes, extension: str) -> str:
    ext = extension.lower()
    if ext == ".pdf":
        return _extract_pdf(data)
    if ext in (".docx", ".doc"):
        return _extract_docx(data)
    if ext in (".html", ".htm"):
        return _extract_html(data)
    if ext in (".json", ".jsonl"):
        return _extract_json(data)
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Document-type classifier
# ---------------------------------------------------------------------------

_INVOICE_RE = re.compile(
    r"\b(invoice|bill\s+to|amount\s+due|subtotal|payment\s+terms|due\s+date)\b",
    re.IGNORECASE,
)
_CONTRACT_RE = re.compile(
    r"\b(agreement|contract|terms\s+and\s+conditions|indemnif|governing\s+law|"
    r"hereinafter|whereas|party|parties)\b",
    re.IGNORECASE,
)
_FINANCIAL_RE = re.compile(
    r"\b(balance\s+sheet|income\s+statement|cash\s+flow|revenue|net\s+income|"
    r"total\s+assets|earnings|fiscal\s+year|quarterly)\b",
    re.IGNORECASE,
)


def _classify_doc_type(text: str) -> str:
    scores = {
        "invoice": len(_INVOICE_RE.findall(text)),
        "contract": len(_CONTRACT_RE.findall(text)),
        "financial_statement": len(_FINANCIAL_RE.findall(text)),
    }
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
    """Split text into overlapping chunks (paragraph → sentence → hard-split)."""
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current, current_len
        val = "\n\n".join(current).strip()
        if val:
            chunks.append(val)
        tail = chunks[-1][-overlap:] if chunks and len(chunks[-1]) > overlap else (chunks[-1] if chunks else "")
        current = [tail] if tail else []
        current_len = len(tail)

    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            for sent in re.split(r"(?<=[.!?])\s+", para):
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
        val = "\n\n".join(current).strip()
        if val:
            chunks.append(val)

    return chunks or [text[:max_chars]]


# ---------------------------------------------------------------------------
# Processed-file idempotency cache (per output prefix)
# ---------------------------------------------------------------------------


def _load_processed_cache(
    storage_client, bucket_name: str, prefix: str
) -> dict[str, str]:
    """Return {blob_name: sha256} for already-processed files."""
    cache_blob = storage_client.bucket(bucket_name).blob(
        f"{prefix}/.processed_files.json"
    )
    if cache_blob.exists():
        try:
            return json.loads(cache_blob.download_as_text())
        except Exception:
            pass
    return {}


def _save_processed_cache(
    storage_client, bucket_name: str, prefix: str, cache: dict[str, str]
) -> None:
    storage_client.bucket(bucket_name).blob(
        f"{prefix}/.processed_files.json"
    ).upload_from_string(
        json.dumps(cache, indent=2).encode("utf-8"),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# DocumentConverter
# ---------------------------------------------------------------------------


class DocumentConverter:
    """Convert raw documents to training JSONL rows.

    Parameters
    ----------
    data_format : "chat" | "instruct"
    max_chunk_chars : int
    chunk_overlap : int
    system_prompt : optional override; default loaded from templates/system_prompt.txt
    strict : if True, raise on unsupported extensions instead of skipping
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
        self.strict = strict
        self.system_prompt = system_prompt or self._load_system_prompt()

    @staticmethod
    def _load_system_prompt() -> str:
        try:
            return _SYSTEM_PROMPT_FILE.read_text().strip()
        except Exception:
            return (
                "You are a specialist in financial document analysis. "
                "Extract structured information accurately and return it as JSON."
            )

    # ------------------------------------------------------------------

    def extract_document(
        self, data: bytes, filename: str, source_uri: str = ""
    ) -> ExtractedDocument:
        ext = Path(filename).suffix.lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            msg = f"Unsupported extension {ext!r} for file {filename!r}"
            if self.strict:
                raise ValueError(msg)
            logger.warning("Skipping: %s", msg)
            return ExtractedDocument(
                source_name=filename, source_uri=source_uri,
                extension=ext, text="", sha256=_sha256(b""), doc_type="generic",
            )
        raw = _extract_text(data, ext)
        text = _clean_text(raw)
        return ExtractedDocument(
            source_name=filename, source_uri=source_uri,
            extension=ext, text=text, sha256=_sha256(data), doc_type=_classify_doc_type(text),
        )

    def make_chunks(self, doc: ExtractedDocument) -> list[Chunk]:
        return [
            Chunk(text=t, source_name=doc.source_name, chunk_index=i, doc_type=doc.doc_type)
            for i, t in enumerate(chunk_text(doc.text, self.max_chunk_chars, self.chunk_overlap))
        ]

    def format_chunk(
        self,
        chunk: Chunk,
        expected_output: str = "",
    ) -> dict:
        """Format a chunk into a JSONL row using the Jinja2 template."""
        return _render_row(
            data_format=self.data_format,
            system_prompt=self.system_prompt,
            extraction_task=DOMAIN_TASKS.get(chunk.doc_type, DOMAIN_TASKS["generic"]),
            document_text=chunk.text,
            source_name=chunk.source_name,
            expected_output=expected_output,
        )

    def convert_bytes(
        self, data: bytes, filename: str, source_uri: str = ""
    ) -> Iterator[dict]:
        """bytes → JSONL row dicts.  Yields one row per chunk."""
        doc = self.extract_document(data, filename, source_uri)
        if not doc.text:
            logger.warning("Empty extraction from '%s' — skipping", filename)
            return
        for chunk in self.make_chunks(doc):
            try:
                yield self.format_chunk(chunk)
            except Exception as exc:
                logger.warning("Row render failed for '%s' chunk %d: %s", filename, chunk.chunk_index, exc)

    def convert_file(self, path: str) -> Iterator[dict]:
        data = Path(path).read_bytes()
        yield from self.convert_bytes(data, Path(path).name, source_uri=path)

    def convert_gcs_blob(self, blob) -> Iterator[dict]:
        data = blob.download_as_bytes()
        yield from self.convert_bytes(
            data, Path(blob.name).name,
            source_uri=f"gs://{blob.bucket.name}/{blob.name}",
        )


# ---------------------------------------------------------------------------
# Pre-labelled JSON/JSONL passthrough
# ---------------------------------------------------------------------------


def load_prelabelled_jsonl(data: bytes, data_format: str) -> tuple[list[dict], list[dict]]:
    """Parse a pre-labelled JSONL file. Returns (valid_rows, rejected_rows)."""
    valid: list[dict] = []
    rejected: list[dict] = []
    for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            rejected.append({"_line": lineno, "_reason": f"json_decode_error: {exc}", "_raw": line[:200]})
            continue
        if data_format == "chat" and not isinstance(row.get("messages"), list):
            rejected.append({**row, "_reason": "missing_messages_field"})
        elif data_format == "instruct" and not all(k in row for k in ("instruction", "output")):
            rejected.append({**row, "_reason": "missing_instruction_or_output"})
        else:
            valid.append(row)
    return valid, rejected


# ---------------------------------------------------------------------------
# GCS batch conversion (used by pipeline.py and CLI)
# ---------------------------------------------------------------------------


def convert_gcs_prefix(
    raw_uri: str,
    output_jsonl_uri: str,
    data_format: str = "chat",
    project: Optional[str] = None,
    strict: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[list[dict], int]:
    """Convert all documents under `raw_uri` and write a JSONL to `output_jsonl_uri`.

    Returns (rows, n_skipped_cached).
    Skips files whose SHA-256 is already in the idempotency cache.
    Writes the cache back to GCS on completion.
    """
    from google.cloud import storage  # deferred import

    def _parse(uri: str) -> tuple[str, str]:
        u = uri.removeprefix("gs://")
        b, _, p = u.partition("/")
        return b, p.rstrip("/")

    storage_client = storage.Client(project=project)

    raw_bucket, raw_prefix = _parse(raw_uri)
    out_bucket, out_prefix = _parse(output_jsonl_uri)
    # strip filename from out_prefix for the cache location
    cache_prefix = "/".join(out_prefix.split("/")[:-1]) if "/" in out_prefix else out_prefix

    processed_cache = _load_processed_cache(storage_client, out_bucket, cache_prefix)

    raw_blobs = [
        b for b in storage_client.list_blobs(raw_bucket, prefix=raw_prefix + "/")
        if not b.name.endswith("/")
    ]

    converter = DocumentConverter(data_format=data_format, strict=strict)
    rows: list[dict] = []
    n_skipped = 0

    for idx, blob in enumerate(raw_blobs, 1):
        ext = Path(blob.name).suffix.lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            if strict:
                raise ValueError(f"Unsupported extension {ext!r}: {blob.name}")
            logger.warning("Skipping unsupported extension %r (%s)", ext, blob.name)
            continue

        # Per-file idempotency: fetch blob bytes, compute sha256
        data = blob.download_as_bytes()
        file_sha = _sha256(data)

        if processed_cache.get(blob.name) == file_sha:
            logger.debug("Skipping already-processed file: %s", blob.name)
            n_skipped += 1
            if progress_callback:
                progress_callback(idx, len(raw_blobs))
            continue

        if ext in (".json", ".jsonl"):
            valid, rejected = load_prelabelled_jsonl(data, data_format)
            rows.extend(valid)
            if rejected:
                logger.warning("%d pre-labelled rows rejected in '%s'", len(rejected), blob.name)
        else:
            blob_rows = list(converter.convert_bytes(data, Path(blob.name).name,
                                                      source_uri=f"gs://{raw_bucket}/{blob.name}"))
            rows.extend(blob_rows)

        processed_cache[blob.name] = file_sha
        if progress_callback:
            progress_callback(idx, len(raw_blobs))

    # Write output JSONL
    if rows:
        jsonl = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        storage_client.bucket(out_bucket).blob(out_prefix).upload_from_string(
            jsonl.encode("utf-8"), content_type="application/jsonl"
        )
        logger.info("Wrote %d rows to gs://%s/%s", len(rows), out_bucket, out_prefix)

    # Persist idempotency cache
    _save_processed_cache(storage_client, out_bucket, cache_prefix, processed_cache)

    if n_skipped:
        logger.info("Skipped %d already-processed files (idempotency cache hit)", n_skipped)

    return rows, n_skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="Convert raw documents to training JSONL")
    parser.add_argument(
        "--input", default=os.environ.get("GCS_RAW_DOCUMENTS_BUCKET"),
        help="Input directory or gs:// URI (env: GCS_RAW_DOCUMENTS_BUCKET)",
    )
    parser.add_argument(
        "--output", default=os.environ.get("GCS_PROCESSED_DATASETS_BUCKET"),
        help="Output JSONL path or gs:// URI (env: GCS_PROCESSED_DATASETS_BUCKET)",
    )
    parser.add_argument("--format", choices=["chat", "instruct"], default="chat")
    parser.add_argument("--job-name", default="job", help="Job name (for output prefix)")
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"))
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def main() -> None:
    import sys

    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        level=logging.INFO, stream=sys.stdout,
    )
    args = _parse_args()

    if not args.input:
        logger.error("--input or GCS_RAW_DOCUMENTS_BUCKET is required")
        sys.exit(1)
    if not args.output:
        logger.error("--output or GCS_PROCESSED_DATASETS_BUCKET is required")
        sys.exit(1)

    try:
        from tqdm import tqdm  # type: ignore[import]
        pbar = tqdm(total=None, unit="file", desc="Converting")
        def _progress(done: int, total: int) -> None:
            pbar.total = total
            pbar.update(1)
    except ImportError:
        def _progress(done: int, total: int) -> None:
            logger.info("Processed %d/%d files", done, total)

    input_uri = args.input
    output_uri = args.output

    # Auto-append job-name to output if output is a bucket root
    if not output_uri.endswith(".jsonl"):
        output_uri = output_uri.rstrip("/") + f"/{args.job_name}/raw.jsonl"

    if input_uri.startswith("gs://"):
        rows, n_skipped = convert_gcs_prefix(
            raw_uri=input_uri,
            output_jsonl_uri=output_uri,
            data_format=args.format,
            project=args.project,
            strict=args.strict,
            progress_callback=_progress,
        )
        logger.info("Done: %d rows written, %d files skipped (cached)", len(rows), n_skipped)
    else:
        # Local path fallback
        converter = DocumentConverter(data_format=args.format, strict=args.strict)
        rows: list[dict] = []
        for fp in sorted(Path(input_uri).rglob("*")):
            if fp.is_file() and fp.suffix.lower() in _SUPPORTED_EXTENSIONS:
                logger.info("Processing %s", fp)
                rows.extend(converter.convert_file(str(fp)))
                _progress(len(rows), -1)

        out_path = Path(output_uri)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
            encoding="utf-8",
        )
        logger.info("Wrote %d rows to %s", len(rows), output_uri)


if __name__ == "__main__":
    main()
