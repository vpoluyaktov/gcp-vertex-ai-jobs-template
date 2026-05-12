"""vLLM OpenAI-compatible serving entrypoint.

Reads configuration from environment variables and exec()s vLLM's built-in
OpenAI-compatible API server.  Supports two serving modes:

  Adapter-only  — base model loaded from HF Hub + LoRA adapter mounted at
                  ADAPTER_PATH (vLLM --enable-lora --lora-modules)
  Merged-weight — full merged model loaded directly from MODEL_PATH

Mode selection (§11.1 of ARCHITECTURE.md):
  ADAPTER_PATH=none               → merged mode; MODEL_PATH (default /models/merged)
  ADAPTER_PATH=<local-path>       → adapter mode; BASE_MODEL_ID loaded from HF Hub
  ADAPTER_PATH=gs://bucket/path/  → adapter downloaded from GCS at startup, then
                                    adapter mode as above

Environment variables
---------------------
BASE_MODEL_ID          HF model ID used in adapter mode
                       (default: unsloth/Meta-Llama-3.1-8B-Instruct)
ADAPTER_PATH           Local adapter dir, gs:// URI, or "none" for merged mode
                       (default: /models/adapter)
MODEL_PATH             Local path for merged-weight mode
                       (default: /models/merged)
MAX_MODEL_LEN          vLLM --max-model-len          (default: 4096)
GPU_MEMORY_UTILIZATION vLLM --gpu-memory-utilization (default: 0.90)
TENSOR_PARALLEL_SIZE   vLLM --tensor-parallel-size   (default: 1)
PORT                   HTTP port                      (default: 8000)
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GCS helper
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
        raise ValueError(f"No objects found at {gcs_uri}")

    os.makedirs(local_dir, exist_ok=True)
    for blob in blobs:
        relative = blob.name[len(prefix):]
        if not relative:
            continue  # skip the directory placeholder blob
        dest = os.path.join(local_dir, relative)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        blob.download_to_filename(dest)
        logger.info("  ↓ %s  →  %s", blob.name, dest)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )

    # -----------------------------------------------------------------------
    # Read configuration from environment
    # -----------------------------------------------------------------------
    base_model_id = os.environ.get(
        "BASE_MODEL_ID", "unsloth/Meta-Llama-3.1-8B-Instruct"
    )
    adapter_path = os.environ.get("ADAPTER_PATH", "/models/adapter")
    model_path = os.environ.get("MODEL_PATH", "/models/merged")
    max_model_len = os.environ.get("MAX_MODEL_LEN", "4096")
    gpu_memory_utilization = os.environ.get("GPU_MEMORY_UTILIZATION", "0.90")
    tensor_parallel_size = os.environ.get("TENSOR_PARALLEL_SIZE", "1")
    port = os.environ.get("PORT", "8000")

    # -----------------------------------------------------------------------
    # Determine serving mode
    # -----------------------------------------------------------------------
    merged_mode: bool = adapter_path.strip().lower() == "none"
    _tmp_adapter_dir: str | None = None

    if not merged_mode and adapter_path.startswith("gs://"):
        # Download adapter from GCS into a temporary directory
        _tmp_adapter_dir = tempfile.mkdtemp(prefix="adapter_")
        logger.info(
            "Downloading LoRA adapter from %s → %s", adapter_path, _tmp_adapter_dir
        )
        _gcs_download_dir(adapter_path, _tmp_adapter_dir)
        adapter_path = _tmp_adapter_dir
        logger.info("Adapter ready at %s", adapter_path)

    # -----------------------------------------------------------------------
    # Build vLLM command
    # -----------------------------------------------------------------------
    if merged_mode:
        logger.info("Serving mode: merged-weight  (model=%s)", model_path)
        cmd = [
            sys.executable, "-m", "vllm.entrypoints.openai.api_server",
            "--model", model_path,
            "--max-model-len", max_model_len,
            "--gpu-memory-utilization", gpu_memory_utilization,
            "--tensor-parallel-size", tensor_parallel_size,
            "--port", port,
            "--host", "0.0.0.0",
            "--trust-remote-code",
            "--disable-log-requests",
        ]
    else:
        logger.info(
            "Serving mode: adapter-only  (base=%s  adapter=%s)",
            base_model_id,
            adapter_path,
        )
        cmd = [
            sys.executable, "-m", "vllm.entrypoints.openai.api_server",
            "--model", base_model_id,
            "--enable-lora",
            "--lora-modules", f"adapter={adapter_path}",
            "--max-lora-rank", "64",
            "--max-model-len", max_model_len,
            "--gpu-memory-utilization", gpu_memory_utilization,
            "--tensor-parallel-size", tensor_parallel_size,
            "--port", port,
            "--host", "0.0.0.0",
            "--trust-remote-code",
            "--disable-log-requests",
        ]

    logger.info("exec: %s", " ".join(cmd))
    # Replace the current process with the vLLM server — no wrapper overhead.
    os.execvp(cmd[0], cmd)
    # Unreachable — execvp never returns on success.


if __name__ == "__main__":
    main()
