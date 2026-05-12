"""Post-training evaluation — perplexity, ROUGE-L, BLEU, and exact-match.

Writes two artefacts to --output-uri (GCS or local):
  eval_results.json  — full metrics breakdown
  eval_score.json    — {"eval_score": <float|null>}  (workflow gating key)

The primary eval_score is selected automatically:
  - JSON output mode  → exact_match  (detected when ≥50% of expected outputs
                                       parse as valid JSON)
  - Free-text mode    → rouge_l

Called by the Temporal workflow's eval-score gating step after Step 3
(monitor_training_job) and before Step 5 (save_artifacts).

Usage:
    python evaluate.py \\
        --base-model    unsloth/Meta-Llama-3.1-8B-Instruct \\
        --adapter-path  gs://bucket/adapter/   \\  # omit for merged model
        --val-data      gs://bucket/val.jsonl  \\
        --output-uri    gs://bucket/results/   \\
        [--format       chat|instruct]         \\
        [--max-samples  100]                   \\
        [--max-new-tokens 256]                 \\
        [--max-seq-length 2048]
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import os
import sys
import tempfile
from typing import Any, Optional

import torch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_SAMPLES = 100
_DEFAULT_MAX_NEW_TOKENS = 256
_DEFAULT_MAX_SEQ_LENGTH = 2048
_JSON_DETECT_PROBE = 10   # number of samples to examine for JSON detection
_JSON_DETECT_THRESHOLD = 0.5  # fraction that must parse as JSON
_GEN_BATCH_SIZE = 1  # generation batch size (conservative for GPU memory)


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


def _gcs_download_file(gcs_uri: str, local_path: str) -> None:
    from google.cloud import storage  # noqa: PLC0415

    bucket_name, blob_name = _parse_gcs_uri(gcs_uri)
    storage.Client().bucket(bucket_name).blob(blob_name).download_to_filename(local_path)
    logger.info("Downloaded %s → %s", gcs_uri, local_path)


def _gcs_download_dir(gcs_uri: str, local_dir: str) -> None:
    """Download all blobs under a GCS prefix into local_dir."""
    from google.cloud import storage  # noqa: PLC0415

    client = storage.Client()
    bucket_name, prefix = _parse_gcs_uri(gcs_uri)
    prefix = prefix.rstrip("/") + "/"
    blobs = list(client.list_blobs(bucket_name, prefix=prefix))
    if not blobs:
        raise FileNotFoundError(f"No objects found at {gcs_uri}")
    os.makedirs(local_dir, exist_ok=True)
    for blob in blobs:
        relative = blob.name[len(prefix):]
        if not relative:
            continue
        dest = os.path.join(local_dir, relative)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        blob.download_to_filename(dest)
        logger.info("  ↓ %s", blob.name)


def _gcs_upload_text(content: str, gcs_uri: str) -> None:
    from google.cloud import storage  # noqa: PLC0415

    bucket_name, blob_name = _parse_gcs_uri(gcs_uri)
    storage.Client().bucket(bucket_name).blob(blob_name).upload_from_string(
        content.encode("utf-8"), content_type="application/json"
    )
    logger.info("Uploaded → %s", gcs_uri)


def _write_output(content: str, uri: str) -> None:
    """Write text to a GCS URI or local path."""
    if uri.startswith("gs://"):
        _gcs_upload_text(content, uri)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(uri)), exist_ok=True)
        with open(uri, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Wrote %s", uri)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_val_data(gcs_uri: str, max_samples: int) -> list[dict]:
    """Download and parse a JSONL file from GCS (or local path)."""
    if gcs_uri.startswith("gs://"):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
            local_path = tmp.name
        _gcs_download_file(gcs_uri, local_path)
    else:
        local_path = gcs_uri

    samples: list[dict] = []
    with open(local_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSONL line")
            if len(samples) >= max_samples:
                break

    if gcs_uri.startswith("gs://"):
        os.unlink(local_path)

    logger.info("Loaded %d validation samples from %s", len(samples), gcs_uri)
    return samples


# ---------------------------------------------------------------------------
# Sample formatting
# ---------------------------------------------------------------------------

def _get_expected_output(sample: dict, fmt: str) -> str:
    """Extract the expected (ground-truth) output string from a sample."""
    if fmt == "chat":
        messages = sample.get("messages", [])
        for m in reversed(messages):
            if m.get("role") == "assistant":
                return m.get("content", "")
        return ""
    return sample.get("output", "")


def _format_full_text(sample: dict, fmt: str, tokenizer: Any) -> Optional[str]:
    """Render a sample as a single string for perplexity computation."""
    if fmt == "chat":
        messages = sample.get("messages", [])
        if not messages:
            return None
        if hasattr(tokenizer, "apply_chat_template"):
            try:
                return tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=False
                )
            except Exception:
                pass
        # Fallback: concatenate turns
        parts = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            parts.append(f"<|{role}|>\n{content}")
        return "\n".join(parts)
    else:  # instruct
        instruction = sample.get("instruction", "")
        inp = sample.get("input", "")
        output = sample.get("output", "")
        if inp:
            return (
                f"### Instruction:\n{instruction}\n\n"
                f"### Input:\n{inp}\n\n"
                f"### Response:\n{output}"
            )
        return f"### Instruction:\n{instruction}\n\n### Response:\n{output}"


def _format_input_prompt(sample: dict, fmt: str, tokenizer: Any) -> Optional[str]:
    """Render the input-only portion of a sample (no expected output)."""
    if fmt == "chat":
        messages = sample.get("messages", [])
        # Drop trailing assistant turns — they are what we want to generate
        input_messages = [m for m in messages if m.get("role") != "assistant"]
        if not input_messages:
            return None
        if hasattr(tokenizer, "apply_chat_template"):
            try:
                return tokenizer.apply_chat_template(
                    input_messages, tokenize=False, add_generation_prompt=True
                )
            except Exception:
                pass
        parts = [f"<|{m['role']}|>\n{m['content']}" for m in input_messages]
        return "\n".join(parts) + "\n<|assistant|>\n"
    else:  # instruct
        instruction = sample.get("instruction", "")
        inp = sample.get("input", "")
        if inp:
            return (
                f"### Instruction:\n{instruction}\n\n"
                f"### Input:\n{inp}\n\n"
                f"### Response:\n"
            )
        return f"### Instruction:\n{instruction}\n\n### Response:\n"


# ---------------------------------------------------------------------------
# JSON output detection
# ---------------------------------------------------------------------------

def _detect_json_mode(samples: list[dict], fmt: str) -> bool:
    """Return True if ≥50% of the first _JSON_DETECT_PROBE expected outputs parse as JSON."""
    probe = samples[: _JSON_DETECT_PROBE]
    json_count = 0
    for s in probe:
        expected = _get_expected_output(s, fmt).strip()
        try:
            parsed = json.loads(expected)
            if isinstance(parsed, (dict, list)):
                json_count += 1
        except (json.JSONDecodeError, ValueError):
            pass
    return len(probe) > 0 and (json_count / len(probe)) >= _JSON_DETECT_THRESHOLD


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _load_model_and_tokenizer(
    base_model: str,
    adapter_path: Optional[str],
    max_seq_length: int,
) -> tuple[Any, Any]:
    """Load base model (optionally with LoRA adapter) via Unsloth."""
    from unsloth import FastLanguageModel  # noqa: PLC0415

    logger.info("Loading base model: %s", base_model)
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=base_model,
        max_seq_length=max_seq_length,
        load_in_4bit=False,
    )

    if adapter_path:
        logger.info("Loading LoRA adapter from %s", adapter_path)
        from peft import PeftModel  # noqa: PLC0415

        model = PeftModel.from_pretrained(model, adapter_path)

    FastLanguageModel.for_inference(model)
    model.eval()
    return model, tokenizer


# ---------------------------------------------------------------------------
# Perplexity
# ---------------------------------------------------------------------------

def _compute_perplexity(
    model: Any,
    tokenizer: Any,
    samples: list[dict],
    fmt: str,
    max_seq_length: int,
) -> float:
    """Compute average token-level perplexity over validation samples."""
    total_nll = 0.0
    total_tokens = 0
    device = next(model.parameters()).device

    with torch.no_grad():
        for sample in samples:
            text = _format_full_text(sample, fmt, tokenizer)
            if text is None:
                continue

            inputs = tokenizer(
                text,
                return_tensors="pt",
                truncation=True,
                max_length=max_seq_length,
            )
            input_ids = inputs["input_ids"].to(device)

            outputs = model(input_ids=input_ids, labels=input_ids)
            loss = outputs.loss.item()
            n_tokens = input_ids.shape[-1]
            total_nll += loss * n_tokens
            total_tokens += n_tokens

    if total_tokens == 0:
        return float("inf")
    return math.exp(total_nll / total_tokens)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _generate_outputs(
    model: Any,
    tokenizer: Any,
    samples: list[dict],
    fmt: str,
    max_new_tokens: int,
) -> list[str]:
    """Generate model outputs for each sample. Returns empty string on failure."""
    device = next(model.parameters()).device
    generated: list[str] = []

    with torch.no_grad():
        for sample in samples:
            prompt = _format_input_prompt(sample, fmt, tokenizer)
            if prompt is None:
                generated.append("")
                continue
            try:
                inputs = tokenizer(
                    prompt, return_tensors="pt", truncation=True, max_length=1024
                )
                input_ids = inputs["input_ids"].to(device)
                n_input = input_ids.shape[-1]

                out_ids = model.generate(
                    input_ids,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,          # greedy for determinism
                    pad_token_id=tokenizer.eos_token_id,
                    eos_token_id=tokenizer.eos_token_id,
                )
                new_ids = out_ids[0, n_input:]
                text = tokenizer.decode(new_ids, skip_special_tokens=True).strip()
                generated.append(text)
            except Exception as exc:
                logger.warning("Generation failed for sample: %s", exc)
                generated.append("")

    return generated


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def _compute_rouge_l(predictions: list[str], references: list[str]) -> float:
    """Return mean ROUGE-L F1 across all samples."""
    from rouge_score import rouge_scorer  # noqa: PLC0415

    scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)
    scores = []
    for pred, ref in zip(predictions, references):
        if not ref.strip():
            continue
        result = scorer.score(ref, pred)
        scores.append(result["rougeL"].fmeasure)
    return sum(scores) / len(scores) if scores else 0.0


def _compute_bleu(predictions: list[str], references: list[str]) -> float:
    """Return corpus BLEU score (0–1) using NLTK with smoothing."""
    import nltk  # noqa: PLC0415
    from nltk.translate.bleu_score import SmoothingFunction, corpus_bleu  # noqa: PLC0415

    nltk.download("punkt_tab", quiet=True)
    smoothie = SmoothingFunction().method4
    refs_tok = [[ref.split()] for ref in references if ref.strip()]
    hyps_tok = [pred.split() for pred, ref in zip(predictions, references) if ref.strip()]
    if not refs_tok:
        return 0.0
    return corpus_bleu(refs_tok, hyps_tok, smoothing_function=smoothie)


def _compute_exact_match(predictions: list[str], references: list[str]) -> float:
    """
    JSON-aware exact match.  Both sides are parsed as JSON; if either fails
    to parse, falls back to normalised string equality.
    Returns fraction of matching samples (0–1).
    """
    matches = 0
    total = 0
    for pred, ref in zip(predictions, references):
        if not ref.strip():
            continue
        total += 1
        try:
            parsed_pred = json.loads(pred.strip())
            parsed_ref = json.loads(ref.strip())
            if parsed_pred == parsed_ref:
                matches += 1
        except (json.JSONDecodeError, ValueError):
            if pred.strip() == ref.strip():
                matches += 1
    return matches / total if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--base-model", required=True,
        help="HF model ID or local/GCS path to merged model weights",
    )
    p.add_argument(
        "--adapter-path", default=None,
        help="LoRA adapter directory (local or gs://). Omit for merged-weight model.",
    )
    p.add_argument(
        "--val-data", required=True,
        help="Validation JSONL file (local path or gs:// URI)",
    )
    p.add_argument(
        "--output-uri", required=True,
        help="GCS prefix (or local dir) where eval_results.json + eval_score.json are written",
    )
    p.add_argument(
        "--format", dest="fmt", default="chat", choices=["chat", "instruct"],
        help="JSONL format — 'chat' (messages[]) or 'instruct' (instruction/input/output)",
    )
    p.add_argument(
        "--max-samples", type=int, default=_DEFAULT_MAX_SAMPLES,
        help=f"Max validation samples to evaluate (default: {_DEFAULT_MAX_SAMPLES})",
    )
    p.add_argument(
        "--max-new-tokens", type=int, default=_DEFAULT_MAX_NEW_TOKENS,
        help=f"Max tokens to generate per sample (default: {_DEFAULT_MAX_NEW_TOKENS})",
    )
    p.add_argument(
        "--max-seq-length", type=int, default=_DEFAULT_MAX_SEQ_LENGTH,
        help=f"Max sequence length for model loading (default: {_DEFAULT_MAX_SEQ_LENGTH})",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )

    args = _parse_args()
    tmp_dirs: list[str] = []

    try:
        # ------------------------------------------------------------------
        # Resolve adapter path — download from GCS if needed
        # ------------------------------------------------------------------
        adapter_local: Optional[str] = None
        if args.adapter_path:
            if args.adapter_path.startswith("gs://"):
                adapter_local = tempfile.mkdtemp(prefix="adapter_")
                tmp_dirs.append(adapter_local)
                logger.info("Downloading adapter from %s", args.adapter_path)
                _gcs_download_dir(args.adapter_path, adapter_local)
            else:
                adapter_local = args.adapter_path

        # ------------------------------------------------------------------
        # Load validation data
        # ------------------------------------------------------------------
        samples = _load_val_data(args.val_data, args.max_samples)
        if not samples:
            logger.warning("No validation samples found — writing null eval_score.")
            _write_output(
                json.dumps({"eval_score": None}),
                _output_uri(args.output_uri, "eval_score.json"),
            )
            _write_output(
                json.dumps({
                    "eval_score": None,
                    "reason": "empty_validation_set",
                    "n_samples": 0,
                }),
                _output_uri(args.output_uri, "eval_results.json"),
            )
            return

        # ------------------------------------------------------------------
        # Detect scoring mode before loading the model
        # ------------------------------------------------------------------
        json_mode = _detect_json_mode(samples, args.fmt)
        score_type = "exact_match" if json_mode else "rouge_l"
        logger.info("Scoring mode: %s", score_type)

        # ------------------------------------------------------------------
        # Load model
        # ------------------------------------------------------------------
        model, tokenizer = _load_model_and_tokenizer(
            args.base_model, adapter_local, args.max_seq_length
        )

        # ------------------------------------------------------------------
        # Perplexity (forward pass only — fast)
        # ------------------------------------------------------------------
        logger.info("Computing perplexity on %d samples…", len(samples))
        perplexity = _compute_perplexity(model, tokenizer, samples, args.fmt, args.max_seq_length)
        logger.info("Perplexity: %.4f", perplexity)

        # ------------------------------------------------------------------
        # Generation-based metrics
        # ------------------------------------------------------------------
        logger.info("Generating outputs for %d samples (max_new_tokens=%d)…",
                    len(samples), args.max_new_tokens)
        predictions = _generate_outputs(model, tokenizer, samples, args.fmt, args.max_new_tokens)
        references = [_get_expected_output(s, args.fmt) for s in samples]

        rouge_l = _compute_rouge_l(predictions, references)
        bleu = _compute_bleu(predictions, references)
        exact_match = _compute_exact_match(predictions, references)
        logger.info("ROUGE-L: %.4f  BLEU: %.4f  exact_match: %.4f", rouge_l, bleu, exact_match)

        # ------------------------------------------------------------------
        # Primary eval_score
        # ------------------------------------------------------------------
        eval_score: Optional[float]
        if score_type == "exact_match":
            eval_score = exact_match
        else:
            eval_score = rouge_l

        logger.info("eval_score (%s): %.4f", score_type, eval_score if eval_score is not None else -1)

        # ------------------------------------------------------------------
        # Write outputs
        # ------------------------------------------------------------------
        eval_results = {
            "eval_score": eval_score,
            "score_type": score_type,
            "perplexity": perplexity if math.isfinite(perplexity) else None,
            "rouge_l": rouge_l,
            "bleu": bleu,
            "exact_match": exact_match,
            "n_samples": len(samples),
            "format": args.fmt,
            "json_mode_detected": json_mode,
            "base_model": args.base_model,
            "adapter_path": args.adapter_path,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        }

        results_uri = _output_uri(args.output_uri, "eval_results.json")
        score_uri = _output_uri(args.output_uri, "eval_score.json")

        _write_output(json.dumps(eval_results, indent=2), results_uri)
        _write_output(json.dumps({"eval_score": eval_score}), score_uri)

        logger.info("Evaluation complete.  eval_score=%.4f  results → %s",
                    eval_score if eval_score is not None else -1.0, results_uri)

    finally:
        import shutil
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)


def _output_uri(base_uri: str, filename: str) -> str:
    """Join a GCS prefix or local dir with a filename."""
    base = base_uri.rstrip("/")
    return f"{base}/{filename}"


if __name__ == "__main__":
    main()
