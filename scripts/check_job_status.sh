#!/usr/bin/env bash
# check_job_status.sh — query Vertex AI custom job state and Temporal workflow status
#
# Polls both the Vertex AI CustomJob (by display name or job ID) and the
# Temporal workflow (by workflow ID) and prints a combined status summary.
#
# Usage:
#   ./scripts/check_job_status.sh [--job-name <name>] [--workflow-id <id>] [--env stage|prod]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -f "${ROOT_DIR}/.env" ]] && source "${ROOT_DIR}/.env"

# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

At least one of --job-name or --workflow-id is required.

Options:
  --job-name    <name>   Vertex AI CustomJob display name (e.g. invoices-llama3-8b-lora-v1)
  --workflow-id <id>     Temporal workflow ID
  --env         stage|prod  Target environment                  (default: stage)
  --region      <region>    Vertex AI region                   (default: VERTEX_AI_REGION or us-central1)
  --watch                   Poll every 30s until the job reaches a terminal state
  -h, --help                Print this message

Environment variables read from .env:
  GCP_PROJECT_ID_STAGE / GCP_PROJECT_ID_PROD
  VERTEX_AI_REGION
  TEMPORAL_ADDRESS
  TEMPORAL_NAMESPACE

Examples:
  ./scripts/check_job_status.sh --job-name invoices-llama3-8b-lora-v1 --env stage
  ./scripts/check_job_status.sh --workflow-id finetune-invoices-llama3-8b-a1b2c3d4
  ./scripts/check_job_status.sh --job-name foo --workflow-id finetune-foo-xxx --watch
EOF
}

# ---------------------------------------------------------------------------
JOB_NAME=""
WORKFLOW_ID=""
ENV="${ENV:-stage}"
REGION="${VERTEX_AI_REGION:-${GCP_REGION:-us-central1}}"
WATCH=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --job-name)    JOB_NAME="$2";    shift 2 ;;
    --workflow-id) WORKFLOW_ID="$2"; shift 2 ;;
    --env)         ENV="$2";         shift 2 ;;
    --region)      REGION="$2";      shift 2 ;;
    --watch)       WATCH=true;       shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -z "${JOB_NAME}" && -z "${WORKFLOW_ID}" ]] && {
  echo "ERROR: at least one of --job-name or --workflow-id is required" >&2
  usage; exit 1
}
[[ "${ENV}" != "stage" && "${ENV}" != "prod" ]] && {
  echo "ERROR: --env must be 'stage' or 'prod'" >&2; exit 1
}

if [[ "${ENV}" == "stage" ]]; then
  PROJECT="${GCP_PROJECT_ID_STAGE:-dfh-stage-id}"
else
  PROJECT="${GCP_PROJECT_ID_PROD:-dfh-prod-id}"
fi

# ---------------------------------------------------------------------------
_print_vertex_status() {
  if [[ -z "${JOB_NAME}" ]]; then return; fi

  echo "── Vertex AI CustomJob ──────────────────────────────"
  # List jobs whose displayName starts with the provided name (newest first).
  gcloud ai custom-jobs list \
    --project="${PROJECT}" \
    --region="${REGION}" \
    --filter="displayName:${JOB_NAME}" \
    --sort-by="~createTime" \
    --limit=5 \
    --format="table(name.basename():label=JOB_ID, displayName:label=DISPLAY_NAME, state, createTime.date('%Y-%m-%d %H:%M'):label=CREATED)"
}

_print_temporal_status() {
  if [[ -z "${WORKFLOW_ID}" ]]; then return; fi

  TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-temporal-server.${ENV}.internal:443}"
  TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}"

  echo ""
  echo "── Temporal Workflow ────────────────────────────────"
  echo "  Workflow ID : ${WORKFLOW_ID}"
  echo "  Server      : ${TEMPORAL_ADDRESS}"
  echo "  Namespace   : ${TEMPORAL_NAMESPACE}"

  # Use temporal CLI if available; otherwise print instructions.
  if command -v temporal >/dev/null 2>&1; then
    temporal workflow describe \
      --workflow-id "${WORKFLOW_ID}" \
      --address "${TEMPORAL_ADDRESS}" \
      --namespace "${TEMPORAL_NAMESPACE}" \
      --tls 2>/dev/null || echo "  (Could not connect — is the VPC connector active?)"
  else
    echo "  (temporal CLI not installed — install from https://docs.temporal.io/cli)"
    echo "  Web UI: http://temporal-server.${ENV}.internal:8233/namespaces/${TEMPORAL_NAMESPACE}/workflows/${WORKFLOW_ID}"
  fi
}

_check_once() {
  echo "==> Status check at $(date '+%Y-%m-%d %H:%M:%S')  [env=${ENV}  project=${PROJECT}]"
  echo ""
  _print_vertex_status
  _print_temporal_status
  echo ""
}

# ---------------------------------------------------------------------------
if [[ "${WATCH}" == "true" ]]; then
  echo "==> Watching every 30s — press Ctrl+C to stop"
  while true; do
    _check_once
    sleep 30
  done
else
  _check_once
fi
