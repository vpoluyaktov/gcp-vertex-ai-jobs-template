#!/usr/bin/env bash
# upload_dataset.sh — upload raw documents or a processed dataset to GCS
#
# Copies a local directory of documents (PDFs, DOCX, HTML, JSONL) to the
# GCS raw-documents bucket.  An optional --prefix lets you organise uploads
# into sub-paths (e.g. invoices/, contracts/).
#
# Usage:
#   ./scripts/upload_dataset.sh --local-dir <dir> [--prefix <gcs-prefix>] [--env stage|prod]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -f "${ROOT_DIR}/.env" ]] && source "${ROOT_DIR}/.env"

# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") --local-dir <dir> [--prefix <gcs-prefix>] [--env stage|prod]

Options:
  --local-dir  Local directory containing documents to upload  (required)
  --prefix     Sub-path inside the raw-documents bucket        (default: basename of local-dir)
  --env        stage | prod                                     (default: stage)
  --bucket     Override GCS bucket name                        (default: GCS_RAW_DOCUMENTS_BUCKET from .env)
  --dry-run    Print what would be uploaded without transferring files
  -h, --help   Print this message

Environment variables (from .env):
  GCS_RAW_DOCUMENTS_BUCKET  Target bucket name
  GCP_PROJECT_ID_STAGE / GCP_PROJECT_ID_PROD

Examples:
  ./scripts/upload_dataset.sh --local-dir ./data/invoices --prefix invoices/
  ./scripts/upload_dataset.sh --local-dir ./data/contracts --env prod
EOF
}

# ---------------------------------------------------------------------------
LOCAL_DIR=""
PREFIX=""
ENV="${ENV:-stage}"
BUCKET_OVERRIDE=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local-dir)    LOCAL_DIR="$2";      shift 2 ;;
    --prefix)       PREFIX="$2";         shift 2 ;;
    --env)          ENV="$2";            shift 2 ;;
    --bucket)       BUCKET_OVERRIDE="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=true;        shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -z "${LOCAL_DIR}" ]] && { echo "ERROR: --local-dir is required" >&2; usage; exit 1; }
[[ ! -d "${LOCAL_DIR}" ]] && { echo "ERROR: '${LOCAL_DIR}' is not a directory" >&2; exit 1; }
[[ "${ENV}" != "stage" && "${ENV}" != "prod" ]] && {
  echo "ERROR: --env must be 'stage' or 'prod'" >&2; exit 1
}

# Resolve bucket
if [[ -n "${BUCKET_OVERRIDE}" ]]; then
  BUCKET="${BUCKET_OVERRIDE}"
else
  BUCKET="${GCS_RAW_DOCUMENTS_BUCKET:-}"
  if [[ -z "${BUCKET}" ]]; then
    if [[ "${ENV}" == "stage" ]]; then
      PROJECT="${GCP_PROJECT_ID_STAGE:-dfh-stage-id}"
    else
      PROJECT="${GCP_PROJECT_ID_PROD:-dfh-prod-id}"
    fi
    BUCKET="${PROJECT}-raw-documents"
    echo "WARN: GCS_RAW_DOCUMENTS_BUCKET not set in .env — defaulting to ${BUCKET}"
  fi
fi

# Resolve prefix
if [[ -z "${PREFIX}" ]]; then
  PREFIX="$(basename "${LOCAL_DIR%/}")/"
fi
# Normalise: ensure exactly one trailing slash
PREFIX="${PREFIX%/}/"

GCS_DEST="gs://${BUCKET}/${PREFIX}"

# Count files
FILE_COUNT=$(find "${LOCAL_DIR}" -type f | wc -l | tr -d ' ')
echo "==> Upload: ${LOCAL_DIR}  →  ${GCS_DEST}"
echo "    Files: ${FILE_COUNT}"

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "    [dry-run] Would run: gcloud storage cp -r '${LOCAL_DIR}/' '${GCS_DEST}'"
  exit 0
fi

# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
echo ""
gcloud storage cp -r "${LOCAL_DIR}/" "${GCS_DEST}"

echo ""
echo "==> Upload complete: ${FILE_COUNT} file(s) → ${GCS_DEST}"
echo ""
echo "    Next step — run the data prep pipeline:"
echo "      Set data.raw_uri: ${GCS_DEST} in your job YAML, then:"
echo "      ./scripts/trigger_training.sh --config <job-yaml> --env ${ENV}"
