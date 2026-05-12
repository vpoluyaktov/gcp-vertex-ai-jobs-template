#!/usr/bin/env bash
# download_model.sh — download final model artifacts from GCS to a local directory
#
# Downloads the fine-tuned LoRA adapter (and optionally the merged weights)
# from the GCS final-models bucket to a local directory.
#
# Usage:
#   ./scripts/download_model.sh --job-name <name> --output-dir <dir> [--env stage|prod]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -f "${ROOT_DIR}/.env" ]] && source "${ROOT_DIR}/.env"

# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") --job-name <name> --output-dir <dir> [OPTIONS]

Options:
  --job-name    <name>     Vertex AI / workflow job name used as GCS prefix   (required)
  --output-dir  <dir>      Local destination directory                         (required)
  --env         stage|prod Target environment                                  (default: stage)
  --bucket      <name>     Override GCS final-models bucket                   (default: GCS_FINAL_MODELS_BUCKET)
  --adapter-only           Download only the adapter/ sub-directory (skip merged/)
  --merged-only            Download only the merged/ sub-directory (skip adapter/)
  --dry-run                Print what would be downloaded without transferring
  -h, --help               Print this message

Environment variables read from .env:
  GCS_FINAL_MODELS_BUCKET
  GCP_PROJECT_ID_STAGE / GCP_PROJECT_ID_PROD

Examples:
  ./scripts/download_model.sh --job-name invoices-llama3-8b-lora-v1 --output-dir ./models/invoices
  ./scripts/download_model.sh --job-name foo --output-dir /tmp/foo --adapter-only --env prod
EOF
}

# ---------------------------------------------------------------------------
JOB_NAME=""
OUTPUT_DIR=""
ENV="${ENV:-stage}"
BUCKET_OVERRIDE=""
ADAPTER_ONLY=false
MERGED_ONLY=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --job-name)      JOB_NAME="$2";      shift 2 ;;
    --output-dir)    OUTPUT_DIR="$2";    shift 2 ;;
    --env)           ENV="$2";           shift 2 ;;
    --bucket)        BUCKET_OVERRIDE="$2"; shift 2 ;;
    --adapter-only)  ADAPTER_ONLY=true;  shift ;;
    --merged-only)   MERGED_ONLY=true;   shift ;;
    --dry-run)       DRY_RUN=true;       shift ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -z "${JOB_NAME}" ]]   && { echo "ERROR: --job-name is required" >&2;  usage; exit 1; }
[[ -z "${OUTPUT_DIR}" ]] && { echo "ERROR: --output-dir is required" >&2; usage; exit 1; }
[[ "${ENV}" != "stage" && "${ENV}" != "prod" ]] && {
  echo "ERROR: --env must be 'stage' or 'prod'" >&2; exit 1
}
[[ "${ADAPTER_ONLY}" == "true" && "${MERGED_ONLY}" == "true" ]] && {
  echo "ERROR: --adapter-only and --merged-only are mutually exclusive" >&2; exit 1
}

# Resolve bucket
if [[ -n "${BUCKET_OVERRIDE}" ]]; then
  BUCKET="${BUCKET_OVERRIDE}"
else
  BUCKET="${GCS_FINAL_MODELS_BUCKET:-}"
  if [[ -z "${BUCKET}" ]]; then
    if [[ "${ENV}" == "stage" ]]; then
      PROJECT="${GCP_PROJECT_ID_STAGE:-dfh-stage-id}"
    else
      PROJECT="${GCP_PROJECT_ID_PROD:-dfh-prod-id}"
    fi
    BUCKET="${PROJECT}-final-models"
    echo "WARN: GCS_FINAL_MODELS_BUCKET not set — defaulting to ${BUCKET}"
  fi
fi

GCS_PREFIX="gs://${BUCKET}/${JOB_NAME}"

# Build the list of sub-paths to download
declare -a SOURCES
if [[ "${ADAPTER_ONLY}" == "true" ]]; then
  SOURCES=("${GCS_PREFIX}/adapter/")
elif [[ "${MERGED_ONLY}" == "true" ]]; then
  SOURCES=("${GCS_PREFIX}/merged/")
else
  SOURCES=("${GCS_PREFIX}/")
fi

mkdir -p "${OUTPUT_DIR}"

echo "==> Download: ${GCS_PREFIX}  →  ${OUTPUT_DIR}"
[[ "${ADAPTER_ONLY}" == "true" ]] && echo "    Scope: adapter only"
[[ "${MERGED_ONLY}" == "true" ]]  && echo "    Scope: merged only"

for SRC in "${SOURCES[@]}"; do
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "    [dry-run] Would run: gcloud storage cp -r '${SRC}' '${OUTPUT_DIR}/'"
  else
    echo ""
    echo "    Downloading: ${SRC}"
    gcloud storage cp -r "${SRC}" "${OUTPUT_DIR}/"
  fi
done

if [[ "${DRY_RUN}" != "true" ]]; then
  echo ""
  echo "==> Download complete → ${OUTPUT_DIR}"
  find "${OUTPUT_DIR}" -type f | sort | while read -r f; do
    SIZE=$(du -sh "${f}" 2>/dev/null | cut -f1)
    echo "    ${SIZE}  ${f}"
  done
fi
