# Data Preparation Guide

This guide walks you through preparing financial and investment documents for
fine-tuning a language model using `gcp-vertex-ai-jobs-template`.

The data preparation pipeline (`data_prep/`) converts raw documents into
instruction-tuning JSONL, validates the output, deduplicates rows, and splits
into train/val sets that are consumed directly by `training/train.py`.

---

## 1. Document Format Requirements

The pipeline accepts four input formats:

| Format | Extensions | How it's handled |
|---|---|---|
| **PDF** | `.pdf` | Text extracted with `pdfplumber` (preferred) or `pypdf` fallback |
| **Word** | `.docx` | Text extracted with `python-docx` |
| **HTML / XML** | `.html`, `.htm`, `.xml` | Parsed with `BeautifulSoup` + `lxml` |
| **Pre-labelled JSONL** | `.jsonl` | Validated against chat or instruct schema; passed through without re-extraction |

**Pre-labelled JSONL** is the highest-quality input.  If you already have
`(instruction, expected_output)` pairs — for example, human-labelled invoice
extractions — put them in JSONL format and the pipeline skips text extraction
entirely and goes straight to validation and splitting.

### Document quality tips

- **Remove scanned PDFs** (images only, no embedded text).  The pipeline cannot
  extract text from image-only PDFs; these produce empty rows and are rejected.
- **One document = one entity.**  An invoice PDF with 3 pages for one invoice
  is fine.  A 200-page batch dump of mixed invoices will be chunked, but the domain
  classifier will still label each chunk correctly.
- **Use consistent file naming.**  The domain classifier reads filenames to
  distinguish invoices (`invoice_`, `inv_`) from contracts (`contract_`, `agreement_`)
  and financial statements (`balance_sheet_`, `income_stmt_`).  Consistent naming
  improves automatic domain assignment.

---

## 2. Upload Raw Documents to GCS

```bash
# Upload a local directory of invoices to the raw-documents bucket
./scripts/upload_dataset.sh \
  --local-dir ./data/invoices \
  --prefix invoices/ \
  --env stage

# Verify the upload
gcloud storage ls gs://<project>-raw-documents/invoices/ --recursive | head -20
```

The `--prefix` sub-path is used as the `raw_uri` value in your job YAML.  Use
a descriptive prefix that matches the document domain (e.g. `invoices/`,
`contracts/q1-2026/`, `financial-statements/`).

**GCS URI pattern for job config:**
```
data:
  raw_uri: gs://<project>-raw-documents/invoices/
```

---

## 3. Running the Data Preparation Pipeline

The pipeline is invoked by the Temporal workflow automatically (Step 1:
`validate_and_preprocess`), but you can also run it standalone for testing:

```bash
# Install dependencies
pip install -r data_prep/requirements.txt

# Run the pipeline locally (reads from GCS, writes to GCS)
export GCS_RAW_DOCUMENTS_BUCKET=<project>-raw-documents
export GCS_PROCESSED_DATASETS_BUCKET=<project>-processed-datasets
export GCP_PROJECT_ID=<project>

python -m data_prep.pipeline \
  --raw-prefix invoices/ \
  --output-prefix invoices-llama3-8b-lora-v1/ \
  --format chat \
  --train-split 0.95 \
  --validation-split 0.05
```

### What the pipeline does

1. **List** all objects under `raw_uri`; compute a SHA-256 of the listing for idempotency.
2. **Extract** text from each document (PDF → pdfplumber, DOCX → python-docx, HTML → BS4).
3. **Classify** document domain from filename and content patterns:
   - `invoice` → extract vendor, total, line items, due date
   - `contract` → extract parties, effective date, key obligations, termination clauses
   - `financial_statement` → extract revenue, expenses, net income, balance sheet items
   - `generic` → general document extraction
4. **Render** each chunk as a chat or instruct JSONL row using the Jinja2 template
   in `data_prep/templates/instruction_template.jinja2`.
5. **Deduplicate** rows by SHA-256 of the (instruction, input) pair.
6. **Split** into `train.jsonl` and `val.jsonl` using a reproducible seed derived
   from the job name.
7. **Write** a `manifest.json` with `n_train`, `n_val`, `n_rejected`, `format`,
   `input_listing_sha256`, and `pipeline_version`.

### Idempotency

The pipeline tracks processed files in `.processed_files.json` under the output
prefix.  Re-running the pipeline with the same input and output prefix is a no-op
for files that haven't changed (content SHA-256 match).  Only new or changed files
are re-processed.

---

## 4. Output Format

### Chat format (recommended for instruction-following models)

```jsonl
{"messages":[
  {"role":"system","content":"You are a specialist in financial and investment document analysis..."},
  {"role":"user","content":"Extract vendor, total, and due date from this invoice:\n\nACME Corp\nInvoice #12345\nDue: 2026-06-01\nTotal: $1,250.00"},
  {"role":"assistant","content":"{\"vendor\":\"ACME Corp\",\"total\":1250.00,\"due_date\":\"2026-06-01\"}"}
]}
```

### Instruct format (for models fine-tuned with Alpaca-style prompts)

```jsonl
{"instruction":"Extract invoice fields as JSON.","input":"ACME Corp\nInvoice #12345\nDue: 2026-06-01\nTotal: $1,250.00","output":"{\"vendor\":\"ACME Corp\",\"total\":1250.00,\"due_date\":\"2026-06-01\"}"}
```

**Choose `format: chat`** for Llama 3.1, Mistral Instruct, and Qwen Instruct models
— they are trained with chat templates and perform best with multi-turn chat format.
Use `format: instruct` only for base (non-instruct) model variants.

---

## 5. Validating Output

The `validate_dataset.py` script (also called inside the pipeline) checks:

- **Schema:** each row conforms to the chat or instruct schema
- **UTF-8 encoding:** no binary garbage in text fields
- **Minimum lengths:** instruction and output are non-empty
- **Split sizes:** training split is non-empty after deduplication
- **Deduplication:** exact duplicate rows are removed

```bash
# Validate a processed dataset directly
python -m data_prep.validate_dataset \
  --input gs://<project>-processed-datasets/invoices-llama3-8b-lora-v1/train.jsonl \
  --format chat

# Check the manifest for pipeline statistics
gsutil cat gs://<project>-processed-datasets/invoices-llama3-8b-lora-v1/manifest.json | python -m json.tool
```

**Healthy manifest example:**
```json
{
  "n_train": 1140,
  "n_val": 60,
  "n_rejected": 12,
  "format": "chat",
  "input_listing_sha256": "a3f7...",
  "tokenizer_validation_pass": true,
  "pipeline_version": "1.0.0"
}
```

---

## 6. Quality Checklist

Before submitting a training job, verify:

- [ ] **`n_train` ≥ 100** — fewer rows rarely produce meaningful fine-tuning gains.
      For financial extraction, 500–2000 labelled examples is a practical target.
- [ ] **`n_rejected` < 10% of total** — high rejection rates indicate document
      quality issues (scanned PDFs, encoding problems, very short pages).
- [ ] **`n_val` ≥ 20** — the eval step needs enough rows to produce stable ROUGE-L
      and exact-match scores.  With `validation_split: 0.05`, you need at least
      400 total rows to get 20 val rows.
- [ ] **Check a sample of chat rows** to confirm the assistant turn contains
      valid JSON (for extraction tasks):
      ```bash
      gsutil cat gs://.../train.jsonl | head -5 | python -c "
      import sys, json
      for line in sys.stdin:
          row = json.loads(line)
          asst = next(m['content'] for m in row['messages'] if m['role']=='assistant')
          print(json.loads(asst))  # will raise if not valid JSON
      "
      ```
- [ ] **Token length check** — verify p95 token length fits within `max_seq_length`:
      ```bash
      # Quick estimate: average ~4 chars/token for English text
      gsutil cat gs://.../train.jsonl | python -c "
      import sys, json, statistics
      lengths = []
      for line in sys.stdin:
          row = json.loads(line)
          text = ' '.join(m['content'] for m in row.get('messages', []))
          lengths.append(len(text) // 4)
      print(f'p50={statistics.median(lengths):.0f}  p95={sorted(lengths)[int(len(lengths)*0.95)]}')"
      ```
- [ ] **No PII leakage** — if documents contain real customer data, confirm that
      your extraction prompts produce structured fields (not raw text that would
      preserve PII verbatim in training data).
- [ ] **`manifest.json` present** — the Temporal workflow's `save_artifacts`
      step reads this file; missing manifest causes a non-retryable error.

---

## 7. Common Data Prep Issues

| Issue | Symptom | Fix |
|---|---|---|
| Empty train split | `DataValidationError("empty training split")` | Dataset has too few rows for the configured `train_split`. Add more documents or reduce `validation_split` (minimum 0.01). |
| PDF produces no text | `n_rejected` very high | Document is scanned (image PDF). Use OCR pre-processing (e.g. `pytesseract`) before upload, or manually label the data. |
| Encoding errors | `UnicodeDecodeError` in pipeline | Re-encode source files to UTF-8: `iconv -f <src_enc> -t utf-8 file.html > file_utf8.html` |
| Wrong domain classification | Extraction prompt targets wrong fields | Rename files to include the domain keyword (`invoice_`, `contract_`, `financial_stmt_`) or pre-label using JSONL format. |
| Duplicate rows after dedup | `n_train` much lower than expected | Source documents contain repeated boilerplate. This is expected and correct — dedup prevents the model from memorising template text. |
| `manifest.json` missing | Step 5 (save_artifacts) fails | Re-run the data prep pipeline to regenerate the manifest, or manually create it from the pipeline logs. |
