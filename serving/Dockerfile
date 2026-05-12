##############################################################################
# Serving container — vLLM OpenAI-compatible inference image
# Base: vllm/vllm-openai:v0.6.3 (CUDA 12.4 + PyTorch — no separate CUDA base needed)
# See §11.1 of ARCHITECTURE.md for design decisions.
#
# Build ARGs
# ----------
# BASE_MODEL_ID   — HF model ID baked into the image label (default: Llama 3.1 8B)
# ADAPTER_URI     — gs:// URI of a LoRA adapter to bake into /models/adapter at
#                   build time.  Leave empty to download at container start instead.
# MERGE_ADAPTER   — "true" if the adapter was already merged; causes server.py to
#                   use /models/merged instead of /models/adapter.
#
# Two serving modes (selected at runtime by env vars in server.py):
#   1. Adapter-only  — vLLM loads BASE_MODEL_ID + LoRA adapter dynamically
#   2. Merged-weight — vLLM loads the pre-merged model from MODEL_PATH directly
##############################################################################

# ---------------------------------------------------------------------------
# Stage 1 — model prep: optional build-time adapter download from GCS
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS model-prep

ARG ADAPTER_URI=""

WORKDIR /prep

RUN pip install --no-cache-dir "google-cloud-storage>=2.16,<3"

# Download adapter from GCS if ADAPTER_URI was supplied at build time.
# Application Default Credentials (ADC) must be configured in the Cloud Build
# service account — no key file is needed.
COPY _gcs_download.py /prep/
RUN if [ -n "${ADAPTER_URI}" ]; then \
      python /prep/_gcs_download.py "${ADAPTER_URI}" /models/adapter; \
    else \
      mkdir -p /models/adapter; \
    fi

# ---------------------------------------------------------------------------
# Stage 2 — runtime image
# ---------------------------------------------------------------------------
FROM vllm/vllm-openai:v0.6.3

ARG BASE_MODEL_ID="unsloth/Meta-Llama-3.1-8B-Instruct"
ARG ADAPTER_URI=""

# Non-root user (uid 1000) — §10.1 convention applied to serving container
RUN useradd -m -u 1000 -s /bin/bash serving \
    && mkdir -p /app /models/adapter /models/merged \
    && chown -R serving:serving /app /models

WORKDIR /app

# Copy serving code and helpers
COPY --chown=serving:serving server.py requirements.txt _gcs_download.py ./

# Install extra Python deps not bundled in the vllm-openai image
RUN pip install --no-cache-dir -r requirements.txt

# Copy pre-downloaded model artifacts from stage 1
COPY --from=model-prep --chown=serving:serving /models/ /models/

USER serving

# ---------------------------------------------------------------------------
# Default env vars (all overridable at container start)
# ---------------------------------------------------------------------------
ENV PORT=8000
ENV BASE_MODEL_ID="${BASE_MODEL_ID}"
ENV ADAPTER_PATH="/models/adapter"
ENV MAX_MODEL_LEN=4096
ENV GPU_MEMORY_UTILIZATION=0.90
ENV TENSOR_PARALLEL_SIZE=1

EXPOSE ${PORT}

LABEL org.opencontainers.image.title="vLLM serving — gcp-vertex-ai-jobs-template"
LABEL org.opencontainers.image.base.name="vllm/vllm-openai:v0.6.3"

ENTRYPOINT ["python", "/app/server.py"]
