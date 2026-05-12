"""Merge a LoRA adapter into base model weights — standalone training-container CLI.

Usable directly from the training container without the full serving stack.
This is a thin wrapper around Unsloth's FastLanguageModel.save_pretrained_merged()
that adds GCS I/O and a --quantize flag.

For producing serving-ready merged weights (with optional HF Hub push),
see serving/prepare_model.py which runs as a Cloud Run Job and includes the
push-to-hub capability.

Usage:
    python merge_adapters.py \\
        --base-model    unsloth/Meta-Llama-3.1-8B-Instruct \\
        --adapter-path  gs://bucket/adapter/               \\
        --output-path   gs://bucket/merged/                \\
        [--quantize     none|4bit|8bit]                    \\
        [--max-seq-length 4096]

Quantize modes:
  none  — Full 16-bit merged weights (default; compatible with any backend)
  4bit  — 4-bit merged weights via Unsloth (smaller; vLLM can load directly)
  8bit  — 8-bit via bitsandbytes load before merge; saved in 16-bit layout
          (NOTE: 8bit is intended for memory-constrained dev environments;
           for production, prefer 'none' or 'none' + GGUF quantisation.)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile

logger = logging.getLogger(__name__)

_SAVE_METHODS = {
    "none": "merged_16bit",
    "4bit": "merged_4bit",
    # 8bit: load in 8bit then merge at full precision — Unsloth doesn't
    # have a native merged_8bit export, so we load with load_in_8bit=True
    # and save with merged_16bit (the in-memory weights are 8bit but the
    # merged output is written as 16bit safetensors).
    "8bit": "merged_16bit",
}


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    uri = uri.removeprefix("gs://")
    bucket, _, prefix = uri.partition("/")
    return bucket, prefix.rstrip("/")


def _gcs_download_dir(gcs_uri: str, local_dir: str) -> None:
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


def _gcs_upload_dir(local_dir: str, gcs_uri: str) -> None:
    from google.cloud import storage  # noqa: PLC0415

    client = storage.Client()
    bucket_name, prefix = _parse_gcs_uri(gcs_uri)
    prefix = prefix.rstrip("/") + "/"
    bucket = client.bucket(bucket_name)
    for root, _dirs, files in os.walk(local_dir):
        for fname in sorted(files):
            local_path = os.path.join(root, fname)
            relative = os.path.relpath(local_path, local_dir).replace(os.sep, "/")
            blob_name = prefix + relative
            bucket.blob(blob_name).upload_from_filename(local_path)
            logger.info("  ↑ %s", blob_name)


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
        help="HF model ID (e.g. unsloth/Meta-Llama-3.1-8B-Instruct)",
    )
    p.add_argument(
        "--adapter-path", required=True,
        help="LoRA adapter directory — local path or gs:// URI",
    )
    p.add_argument(
        "--output-path", required=True,
        help="Merged model destination — local path or gs:// URI",
    )
    p.add_argument(
        "--quantize", default="none", choices=list(_SAVE_METHODS),
        help=(
            "Quantization mode for the merged output: "
            "'none' = 16-bit (default), '4bit' = 4-bit via Unsloth, "
            "'8bit' = bitsandbytes 8-bit load with 16-bit output"
        ),
    )
    p.add_argument(
        "--max-seq-length", type=int, default=4096,
        help="max_seq_length passed to FastLanguageModel (default: 4096)",
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
    save_method = _SAVE_METHODS[args.quantize]
    tmp_dirs: list[str] = []

    if args.quantize == "8bit":
        logger.warning(
            "--quantize 8bit: loading base model in 8-bit via bitsandbytes. "
            "The merged output will be saved as 16-bit safetensors — the "
            "in-memory representation is 8-bit but the saved weights are "
            "dequantised to fp16. For production use, prefer --quantize none."
        )

    try:
        # ------------------------------------------------------------------
        # Step 1 — resolve adapter: download from GCS if needed
        # ------------------------------------------------------------------
        adapter_local = args.adapter_path
        if args.adapter_path.startswith("gs://"):
            adapter_local = tempfile.mkdtemp(prefix="adapter_")
            tmp_dirs.append(adapter_local)
            logger.info("Downloading adapter from %s", args.adapter_path)
            _gcs_download_dir(args.adapter_path, adapter_local)
            logger.info("Adapter ready at %s", adapter_local)

        # ------------------------------------------------------------------
        # Step 2 — load base model via Unsloth
        # ------------------------------------------------------------------
        logger.info(
            "Loading base model: %s  (quantize=%s, save_method=%s)",
            args.base_model, args.quantize, save_method,
        )
        from unsloth import FastLanguageModel  # noqa: PLC0415

        load_kwargs: dict = {
            "model_name": args.base_model,
            "max_seq_length": args.max_seq_length,
            "load_in_4bit": args.quantize == "4bit",
            "load_in_8bit": args.quantize == "8bit",
        }
        model, tokenizer = FastLanguageModel.from_pretrained(**load_kwargs)

        # ------------------------------------------------------------------
        # Step 3 — attach LoRA adapter
        # ------------------------------------------------------------------
        logger.info("Attaching LoRA adapter from %s", adapter_local)
        from peft import PeftModel  # noqa: PLC0415

        model = PeftModel.from_pretrained(model, adapter_local)

        # ------------------------------------------------------------------
        # Step 4 — merge and save locally
        # ------------------------------------------------------------------
        merged_local = tempfile.mkdtemp(prefix="merged_")
        tmp_dirs.append(merged_local)
        logger.info("Merging into %s  (save_method=%s)…", merged_local, save_method)

        FastLanguageModel.save_pretrained_merged(
            model,
            tokenizer,
            merged_local,
            save_method=save_method,
        )
        logger.info("Merge complete.")

        # ------------------------------------------------------------------
        # Step 5 — upload / copy to output-path
        # ------------------------------------------------------------------
        if args.output_path.startswith("gs://"):
            logger.info("Uploading merged model to %s", args.output_path)
            _gcs_upload_dir(merged_local, args.output_path)
            logger.info("Upload complete.")
        else:
            os.makedirs(args.output_path, exist_ok=True)
            for item in sorted(os.listdir(merged_local)):
                src = os.path.join(merged_local, item)
                dst = os.path.join(args.output_path, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            logger.info("Merged model saved to %s", args.output_path)

        logger.info("Done.  Merged %s → %s", args.base_model, args.output_path)

    finally:
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
