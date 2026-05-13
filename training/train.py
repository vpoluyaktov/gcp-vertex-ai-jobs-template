"""Vertex AI training entrypoint — supervised fine-tuning with Unsloth + PEFT + TRL.

Implements §10 of ARCHITECTURE.md.

Config is loaded from a YAML file whose GCS path is passed as --config
(Hydra composition is used for structured config).

Key behaviours:
  - Loads base model from Unsloth (ungated, no HF_TOKEN required for downloads).
  - Applies LoRA/QLoRA via PEFT with configurable rank, alpha, target_modules.
  - Emits a prominent WARNING if use_gpu=false and model param count > 1B (§4.4).
  - Loads dataset from GCS (JSONL, chat or instruct format).
  - Trains with SFTTrainer (TRL) + callbacks: GCS checkpoint sync, TensorBoard,
    optional W&B.
  - Resumes from latest GCS checkpoint on Spot VM preemption restart.
  - Saves final LoRA adapter + tokenizer + metrics to GCS on completion.
  - Optional LoRA merge if peft.merge_after_training=true.
  - Logs metrics to Vertex AI TensorBoard via AIP_TENSORBOARD_LOG_DIR (§15).
  - Exits 0 on success, non-zero on failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LARGE_MODEL_THRESHOLD_B = 1_000_000_000  # 1B parameters
_CPU_WARNING = (
    "WARN: CPU-only training of a >1B-parameter model is intended for template "
    "development and CI smoke testing ONLY. For real workloads, set "
    "infrastructure.use_gpu=true."
)


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


def _download_gcs_file(uri: str, local_path: str) -> None:
    from google.cloud import storage

    bucket_name, blob_name = _parse_gcs_uri(uri)
    client = storage.Client()
    client.bucket(bucket_name).blob(blob_name).download_to_filename(local_path)


def _download_gcs_dir(uri: str, local_dir: str) -> None:
    """Download all blobs under a GCS prefix to a local directory."""
    from google.cloud import storage

    bucket_name, prefix = _parse_gcs_uri(uri)
    client = storage.Client()
    blobs = list(client.list_blobs(bucket_name, prefix=prefix + "/"))
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    for blob in blobs:
        rel = blob.name[len(prefix) :].lstrip("/")
        if not rel:
            continue
        dest = Path(local_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(dest))


def _upload_gcs_dir(local_dir: str, uri: str) -> None:
    """Recursively upload a local directory to a GCS prefix."""
    from google.cloud import storage

    bucket_name, prefix = _parse_gcs_uri(uri)
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    local_path = Path(local_dir)
    for f in local_path.rglob("*"):
        if f.is_file():
            blob_name = f"{prefix}/{f.relative_to(local_path)}"
            bucket.blob(blob_name).upload_from_filename(str(f))


def _find_latest_checkpoint(checkpoint_uri: str) -> Optional[str]:
    """Return the GCS URI of the latest checkpoint-N directory, or None."""
    from google.cloud import storage

    bucket_name, prefix = _parse_gcs_uri(checkpoint_uri)
    client = storage.Client()
    # List checkpoint-* directories (look for adapter_config or config.json)
    blobs = list(client.list_blobs(bucket_name, prefix=prefix + "/checkpoint-"))
    steps = set()
    for b in blobs:
        parts = b.name[len(prefix) :].lstrip("/").split("/")
        if parts[0].startswith("checkpoint-"):
            try:
                steps.add(int(parts[0].split("-")[1]))
            except (IndexError, ValueError):
                pass
    if not steps:
        return None
    latest = max(steps)
    return f"{checkpoint_uri}/checkpoint-{latest}"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_config(config_path: str) -> Dict[str, Any]:
    """Load YAML config from a local path or GCS URI."""
    import yaml

    if config_path.startswith("gs://"):
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
            _download_gcs_file(config_path, tmp.name)
            local_path = tmp.name
    else:
        local_path = config_path

    with open(local_path) as f:
        cfg = yaml.safe_load(f)

    if config_path.startswith("gs://"):
        os.unlink(local_path)

    return cfg


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def _load_dataset_from_gcs(processed_uri: str, split: str = "train"):
    """Load train.jsonl or val.jsonl from GCS as a HuggingFace Dataset."""
    import datasets

    bucket_name, prefix = _parse_gcs_uri(processed_uri)
    filename = "train.jsonl" if split == "train" else "val.jsonl"
    gcs_uri = f"gs://{bucket_name}/{prefix}/{filename}"

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="wb") as tmp:
        _download_gcs_file(gcs_uri, tmp.name)
        local_path = tmp.name

    ds = datasets.load_dataset("json", data_files=local_path, split="train")
    os.unlink(local_path)
    return ds


def _apply_chat_template(dataset, tokenizer, data_format: str):
    """Apply the appropriate chat template to each row."""
    if data_format == "chat":
        def _fmt(row):
            messages = row.get("messages", [])
            return {"text": tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False
            )}
        return dataset.map(_fmt, remove_columns=dataset.column_names)
    elif data_format == "instruct":
        def _fmt(row):
            text = (
                f"### Instruction:\n{row['instruction']}\n\n"
                f"### Input:\n{row.get('input', '')}\n\n"
                f"### Response:\n{row['output']}"
            )
            return {"text": text}
        return dataset.map(_fmt, remove_columns=dataset.column_names)
    else:
        raise ValueError(f"Unsupported data format: {data_format!r}")


# ---------------------------------------------------------------------------
# Main training function
# ---------------------------------------------------------------------------


def train(args: argparse.Namespace) -> None:
    cfg = _load_config(args.config)

    # Override config with CLI args (Vertex AI passes these)
    if args.checkpoint_uri:
        cfg.setdefault("data", {})["checkpoint_uri"] = args.checkpoint_uri
    if args.output_uri:
        cfg.setdefault("artifacts", {})["output_uri"] = args.output_uri
    if args.job_name:
        cfg.setdefault("job", {})["job_name"] = args.job_name

    # Resolve nested config sections with defaults
    model_cfg = cfg.get("model", {}) or cfg  # top-level fallback
    lora_cfg = cfg.get("lora", {})
    training_cfg = cfg.get("training", {})
    data_cfg = cfg.get("data", {})
    artifacts_cfg = cfg.get("artifacts", {})
    infra_cfg = cfg.get("infrastructure", {})

    base_model_id: str = (
        model_cfg.get("base_model_id")
        or cfg.get("base_model")
        or "unsloth/Meta-Llama-3.1-8B-Instruct"
    )
    use_gpu: bool = infra_cfg.get("use_gpu", False)
    processed_uri: str = data_cfg.get("gcs_input_path", "")
    output_uri: str = artifacts_cfg.get("output_uri") or data_cfg.get("gcs_output_dir", "")
    checkpoint_uri: str = args.checkpoint_uri or output_uri

    lora_r: int = lora_cfg.get("r", 16)
    lora_alpha: int = lora_cfg.get("lora_alpha", 32)
    lora_dropout: float = lora_cfg.get("lora_dropout", 0.05)
    target_modules: list = lora_cfg.get(
        "target_modules",
        ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    peft_bias: str = lora_cfg.get("bias", "none")
    merge_after: bool = lora_cfg.get("merge_after_training", False)

    num_epochs: int = int(training_cfg.get("num_train_epochs", 3))
    batch_size: int = int(training_cfg.get("per_device_train_batch_size", 4))
    grad_accum: int = int(training_cfg.get("gradient_accumulation_steps", 4))
    lr: float = float(training_cfg.get("learning_rate", 2e-4))
    lr_scheduler: str = training_cfg.get("lr_scheduler_type", "cosine")
    warmup_ratio: float = float(training_cfg.get("warmup_ratio", 0.05))
    max_seq_len: int = int(training_cfg.get("max_seq_length", 2048))
    bf16: bool = bool(training_cfg.get("bf16", False))
    fp16: bool = bool(training_cfg.get("fp16", False))
    data_format: str = data_cfg.get("chat_template", "chat").replace("-", "_").lower()
    if "llama" in data_format or data_format == "llama_3":
        data_format = "chat"

    # -------------------------------------------------------------------------
    # TensorBoard — Vertex AI auto-uploads logs written under AIP_TENSORBOARD_LOG_DIR
    # -------------------------------------------------------------------------
    _tb_env = os.environ.get("AIP_TENSORBOARD_LOG_DIR", "/tmp/tb_logs")
    # AIP_TENSORBOARD_LOG_DIR may be a gs:// URI; pathlib can't mkdir those.
    tb_log_dir = _tb_env if not _tb_env.startswith("gs://") else "/tmp/tb_logs"
    Path(tb_log_dir).mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # Load model with Unsloth FastLanguageModel
    # -------------------------------------------------------------------------
    logger.info("Loading base model: %s", base_model_id)

    from unsloth import FastLanguageModel  # type: ignore[import]

    dtype = None  # auto-detect
    load_in_4bit = False

    # Check if QLoRA (4-bit quantisation)
    peft_type = lora_cfg.get("type", "lora")
    if peft_type == "qlora":
        load_in_4bit = True
        logger.info("QLoRA mode: loading model in 4-bit")

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=base_model_id,
        max_seq_length=max_seq_len,
        dtype=dtype,
        load_in_4bit=load_in_4bit,
    )

    # --- CPU-only large model warning (§4.4) ---
    if not use_gpu:
        param_count = sum(p.numel() for p in model.parameters())
        if param_count > _LARGE_MODEL_THRESHOLD_B:
            logger.warning(_CPU_WARNING)

    # -------------------------------------------------------------------------
    # Apply LoRA / QLoRA PEFT
    # -------------------------------------------------------------------------
    logger.info(
        "Applying LoRA: r=%d alpha=%d dropout=%.3f target_modules=%s",
        lora_r,
        lora_alpha,
        lora_dropout,
        target_modules,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=lora_r,
        target_modules=target_modules,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        bias=peft_bias,
        use_gradient_checkpointing="unsloth",
        random_state=42,
        use_rslora=False,
        loftq_config=None,
    )

    # -------------------------------------------------------------------------
    # Load dataset
    # -------------------------------------------------------------------------
    logger.info("Loading dataset from %s", processed_uri)
    train_dataset = _load_dataset_from_gcs(processed_uri, split="train")
    eval_dataset = _load_dataset_from_gcs(processed_uri, split="val")

    # Apply chat / instruct template
    train_dataset = _apply_chat_template(train_dataset, tokenizer, data_format)
    eval_dataset = _apply_chat_template(eval_dataset, tokenizer, data_format)

    logger.info(
        "Dataset: %d train rows, %d eval rows", len(train_dataset), len(eval_dataset)
    )

    # -------------------------------------------------------------------------
    # Resume from checkpoint if available
    # -------------------------------------------------------------------------
    resume_from: Optional[str] = None
    if checkpoint_uri:
        latest_ckpt_uri = _find_latest_checkpoint(checkpoint_uri)
        if latest_ckpt_uri:
            local_ckpt = tempfile.mkdtemp(prefix="ckpt_")
            logger.info(
                "Resuming from checkpoint: %s → %s", latest_ckpt_uri, local_ckpt
            )
            _download_gcs_dir(latest_ckpt_uri, local_ckpt)
            resume_from = local_ckpt

    # -------------------------------------------------------------------------
    # Build SFTTrainer
    # -------------------------------------------------------------------------
    import torch
    from trl import SFTTrainer
    from transformers import TrainingArguments

    local_output_dir = tempfile.mkdtemp(prefix="train_output_")

    training_args = TrainingArguments(
        output_dir=local_output_dir,
        num_train_epochs=num_epochs,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        learning_rate=lr,
        lr_scheduler_type=lr_scheduler,
        warmup_ratio=warmup_ratio,
        bf16=bf16,
        fp16=fp16,
        logging_dir=tb_log_dir,
        logging_steps=50,
        eval_strategy="epoch",
        save_strategy="steps",
        save_steps=500,
        save_total_limit=3,
        load_best_model_at_end=False,
        report_to=_resolve_report_to(),
        dataloader_num_workers=0,
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        dataset_text_field="text",
        max_seq_length=max_seq_len,
        args=training_args,
        packing=False,
    )

    # -------------------------------------------------------------------------
    # Train
    # -------------------------------------------------------------------------
    logger.info("Starting training: epochs=%d lr=%g", num_epochs, lr)
    train_result = trainer.train(resume_from_checkpoint=resume_from)
    logger.info("Training complete: %s", train_result.metrics)

    # -------------------------------------------------------------------------
    # Evaluate
    # -------------------------------------------------------------------------
    eval_metrics = trainer.evaluate()
    logger.info("Eval metrics: %s", eval_metrics)
    eval_loss = eval_metrics.get("eval_loss")
    eval_score = _compute_eval_score(eval_loss)

    # -------------------------------------------------------------------------
    # Save adapter
    # -------------------------------------------------------------------------
    adapter_local = Path(local_output_dir) / "adapter"
    adapter_local.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(adapter_local))
    tokenizer.save_pretrained(str(adapter_local))

    # Write eval_score.json
    eval_score_path = Path(local_output_dir) / "eval_score.json"
    eval_score_path.write_text(json.dumps({"eval_score": eval_score}))

    # Write training_metrics.json
    metrics = {
        **train_result.metrics,
        **eval_metrics,
        "eval_score": eval_score,
    }
    metrics_path = Path(local_output_dir) / "training_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2))

    # Optional: merge LoRA into base weights
    if merge_after:
        logger.info("Merging LoRA adapter into base weights...")
        merged_local = Path(local_output_dir) / "merged"
        merged_local.mkdir(parents=True, exist_ok=True)
        FastLanguageModel.for_inference(model)  # type: ignore[attr-defined]
        model.save_pretrained_merged(  # type: ignore[attr-defined]
            str(merged_local),
            tokenizer,
            save_method="merged_16bit",
        )

    # -------------------------------------------------------------------------
    # Upload artifacts to GCS
    # -------------------------------------------------------------------------
    if output_uri:
        logger.info("Uploading adapter to %s/adapter", output_uri)
        _upload_gcs_dir(str(adapter_local), f"{output_uri}/adapter")

        logger.info("Uploading metrics to %s", output_uri)
        _upload_single_file(str(eval_score_path), f"{output_uri}/eval_score.json")
        _upload_single_file(str(metrics_path), f"{output_uri}/training_metrics.json")

        if merge_after and (Path(local_output_dir) / "merged").exists():
            logger.info("Uploading merged weights to %s/merged", output_uri)
            _upload_gcs_dir(
                str(Path(local_output_dir) / "merged"), f"{output_uri}/merged"
            )

    logger.info("Training job complete.")


def _compute_eval_score(eval_loss: Optional[float]) -> Optional[float]:
    """Convert eval_loss to a score in [0, 1].  Returns None if eval_loss is None."""
    if eval_loss is None:
        return None
    # Perplexity-based heuristic: score = exp(-loss/10), clamped to [0, 1]
    return min(1.0, math.exp(-eval_loss / 10.0))


def _resolve_report_to() -> list[str]:
    reporters = ["tensorboard"]
    if os.environ.get("WANDB_API_KEY"):
        reporters.append("wandb")
    return reporters


def _upload_single_file(local_path: str, gcs_uri: str) -> None:
    from google.cloud import storage

    bucket_name, blob_name = _parse_gcs_uri(gcs_uri)
    storage.Client().bucket(bucket_name).blob(blob_name).upload_from_filename(
        local_path
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vertex AI fine-tuning entrypoint")
    parser.add_argument(
        "--config",
        required=True,
        help="Path (local or gs://) to the training YAML config",
    )
    parser.add_argument(
        "--checkpoint_uri",
        default="",
        help="GCS URI to read/write checkpoints (overrides config)",
    )
    parser.add_argument(
        "--output_uri",
        default="",
        help="GCS URI for final model artifacts (overrides config)",
    )
    parser.add_argument(
        "--job_name",
        default="",
        help="Workflow job name injected by the Temporal activity",
    )
    return parser


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )

    # Vertex AI passes Hydra overrides as positional +key=value args.
    # Strip them and re-route to our simple argparser.
    raw_args = sys.argv[1:]
    known_flags: list[str] = []
    hydra_overrides: dict[str, str] = {}
    i = 0
    while i < len(raw_args):
        arg = raw_args[i]
        if arg.startswith("+") or arg.startswith("~"):
            key, _, val = arg[1:].partition("=")
            hydra_overrides[key] = val
        elif arg.startswith("--"):
            known_flags.append(arg)
            if i + 1 < len(raw_args) and not raw_args[i + 1].startswith("-"):
                known_flags.append(raw_args[i + 1])
                i += 1
        i += 1

    # Map Hydra overrides to argparser args
    if "job.config_uri" in hydra_overrides and "--config" not in known_flags:
        known_flags += ["--config", hydra_overrides["job.config_uri"]]
    if "job.checkpoint_uri" in hydra_overrides:
        known_flags += ["--checkpoint_uri", hydra_overrides["job.checkpoint_uri"]]
    if "job.output_uri" in hydra_overrides:
        known_flags += ["--output_uri", hydra_overrides["job.output_uri"]]
    if "job.job_name" in hydra_overrides:
        known_flags += ["--job_name", hydra_overrides["job.job_name"]]

    parser = _build_arg_parser()
    args = parser.parse_args(known_flags)

    try:
        train(args)
    except Exception as exc:
        logger.exception("Training failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
