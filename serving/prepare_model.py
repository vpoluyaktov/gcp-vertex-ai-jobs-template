#!/usr/bin/env python3
"""Merge a LoRA adapter into the base model weights and upload to GCS.

Usage:
    python prepare_model.py \\
        --base-model unsloth/Meta-Llama-3.1-8B-Instruct \\
        --adapter-path gs://bucket/adapter/ \\
        --output-path  gs://bucket/merged/ \\
        [--push-to-hub  user/repo-name] \\
        [--hf-token     <token>] \\
        [--max-seq-length 4096]

Steps
-----
1. Download adapter from GCS (if --adapter-path starts with gs://).
2. Load base model + adapter via Unsloth FastLanguageModel.
3. Merge LoRA weights into base model (16-bit).
4. Save merged model to a local temp directory.
5. Upload merged model to GCS (if --output-path starts with gs://).
6. Optionally push merged model to HF Hub (--push-to-hub).

This script runs inside the **training container** (which has Unsloth/PEFT
installed), not inside the vLLM serving container.  It is invoked by the
`prepare_serving_artifacts` Temporal activity as a Cloud Run Job.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _gcs_download_dir(gcs_uri: str, local_dir: str) -> None:
    """Download all blobs under a GCS prefix into local_dir."""
    from google.cloud import storage  # noqa: PLC0415

    client = storage.Client()
    without_scheme = gcs_uri[len("gs://"):]
    bucket_name, _, prefix = without_scheme.partition("/")
    prefix = prefix.rstrip("/") + "/"

    blobs = list(client.list_blobs(bucket_name, prefix=prefix))
    if not blobs:
        raise FileNotFoundError(f"No objects found at {gcs_uri}")

    os.makedirs(local_dir, exist_ok=True)
    for blob in blobs:
        relative = blob.name[len(prefix):]
        if not relative:
            continue  # skip directory placeholder blobs
        dest = os.path.join(local_dir, relative)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        blob.download_to_filename(dest)
        logger.info("  ↓ %s", blob.name)


def _gcs_upload_dir(local_dir: str, gcs_uri: str) -> None:
    """Upload all files under local_dir to a GCS prefix."""
    from google.cloud import storage  # noqa: PLC0415

    client = storage.Client()
    without_scheme = gcs_uri[len("gs://"):]
    bucket_name, _, prefix = without_scheme.partition("/")
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
        "--base-model",
        required=True,
        help="HF model ID, e.g. unsloth/Meta-Llama-3.1-8B-Instruct",
    )
    p.add_argument(
        "--adapter-path",
        required=True,
        help="LoRA adapter directory — local path or gs:// URI",
    )
    p.add_argument(
        "--output-path",
        required=True,
        help="Merged model destination — local path or gs:// URI",
    )
    p.add_argument(
        "--push-to-hub",
        default=None,
        help="HF Hub repo ID to push the merged model (optional)",
    )
    p.add_argument(
        "--hf-token",
        default=os.environ.get("HF_TOKEN"),
        help="HF Hub token; falls back to HF_TOKEN env var",
    )
    p.add_argument(
        "--max-seq-length",
        type=int,
        default=4096,
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
    tmp_dirs: list[str] = []

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
        # Step 2 — load base model + adapter via Unsloth
        # ------------------------------------------------------------------
        logger.info("Loading base model: %s", args.base_model)
        from unsloth import FastLanguageModel  # noqa: PLC0415

        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=args.base_model,
            max_seq_length=args.max_seq_length,
            # Load in full precision so the merge is lossless.
            load_in_4bit=False,
            load_in_8bit=False,
        )

        logger.info("Attaching LoRA adapter from %s", adapter_local)
        from peft import PeftModel  # noqa: PLC0415

        model = PeftModel.from_pretrained(model, adapter_local)

        # ------------------------------------------------------------------
        # Step 3 & 4 — merge and save locally (16-bit safetensors)
        # ------------------------------------------------------------------
        merged_local = tempfile.mkdtemp(prefix="merged_")
        tmp_dirs.append(merged_local)
        logger.info("Merging LoRA weights → %s (merged_16bit)", merged_local)

        FastLanguageModel.save_pretrained_merged(
            model,
            tokenizer,
            merged_local,
            save_method="merged_16bit",
        )
        logger.info("Merge complete.")

        # ------------------------------------------------------------------
        # Step 5 — upload / copy merged model to output-path
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

        # ------------------------------------------------------------------
        # Step 6 (optional) — push to HF Hub
        # ------------------------------------------------------------------
        if args.push_to_hub:
            logger.info("Pushing merged model to HF Hub: %s", args.push_to_hub)
            model.push_to_hub(
                args.push_to_hub, token=args.hf_token, private=True
            )
            tokenizer.push_to_hub(
                args.push_to_hub, token=args.hf_token, private=True
            )
            logger.info("HF Hub push complete.")

    finally:
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
