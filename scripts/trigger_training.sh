#!/usr/bin/env bash
# trigger_training.sh — start a FineTuneWorkflow via the Temporal client CLI
#
# Thin wrapper around `python -m temporal.client.trigger_workflow` that
# sources .env for connection parameters and activates the Python virtualenv
# if one is present at .venv/.
#
# Usage:
#   ./scripts/trigger_training.sh --config <job-yaml> [--env stage|prod] [--wait] [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -f "${ROOT_DIR}/.env" ]] && source "${ROOT_DIR}/.env"

# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") --config <job-yaml> [OPTIONS]

Options:
  --config <path|gs://>   Path to job YAML (local or GCS URI)   (required)
  --env    stage|prod      Target environment                     (default: stage)
  --workflow-id <id>       Workflow ID override                   (default: auto-generated)
  --wait                   Block until the workflow completes
  --wait-seconds <n>       Timeout for --wait in seconds         (default: 300)
  --dry-run                Validate config and print resolved params without starting
  --verbose                Enable DEBUG logging in the Python client
  -h, --help               Print this message

Environment variables read from .env:
  TEMPORAL_ADDRESS     gRPC address of Temporal server
  TEMPORAL_NAMESPACE   Temporal namespace (default: default)
  TEMPORAL_TASK_QUEUE  Task queue override

Examples:
  ./scripts/trigger_training.sh --config configs/training-job-llama3-8b.yaml --env stage
  ./scripts/trigger_training.sh --config gs://dfh-stage-id-configs/jobs/foo.yaml --env stage --wait
  ./scripts/trigger_training.sh --config configs/training-job-llama3-8b.yaml --dry-run
EOF
}

# ---------------------------------------------------------------------------
CONFIG=""
ENV="${ENV:-stage}"
WORKFLOW_ID=""
WAIT=false
WAIT_SECONDS=300
DRY_RUN=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)        CONFIG="$2";        shift 2 ;;
    --env)           ENV="$2";           shift 2 ;;
    --workflow-id)   WORKFLOW_ID="$2";   shift 2 ;;
    --wait)          WAIT=true;          shift ;;
    --wait-seconds)  WAIT_SECONDS="$2";  shift 2 ;;
    --dry-run)       DRY_RUN=true;       shift ;;
    --verbose|-v)    VERBOSE=true;       shift ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -z "${CONFIG}" ]] && { echo "ERROR: --config is required" >&2; usage; exit 1; }
[[ "${ENV}" != "stage" && "${ENV}" != "prod" ]] && {
  echo "ERROR: --env must be 'stage' or 'prod'" >&2; exit 1
}

# ---------------------------------------------------------------------------
# Activate virtualenv if present
# ---------------------------------------------------------------------------
if [[ -f "${ROOT_DIR}/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "${ROOT_DIR}/.venv/bin/activate"
fi

# Confirm the trigger client is importable
if ! python -c "import temporal.client.trigger_workflow" 2>/dev/null; then
  echo "ERROR: temporal.client.trigger_workflow not importable." >&2
  echo "       Run: pip install -r temporal/client/requirements.txt" >&2
  echo "       And ensure PYTHONPATH includes the repo root." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Build argument list
# ---------------------------------------------------------------------------
ARGS=(
  --config "${CONFIG}"
  --env    "${ENV}"
  --wait-seconds "${WAIT_SECONDS}"
)
[[ -n "${WORKFLOW_ID}" ]]  && ARGS+=(--workflow-id "${WORKFLOW_ID}")
[[ "${WAIT}" == "true" ]]  && ARGS+=(--wait)
[[ "${DRY_RUN}" == "true" ]] && ARGS+=(--dry-run)
[[ "${VERBOSE}" == "true" ]] && ARGS+=(--verbose)

# Pass env vars through; the Python client also reads TEMPORAL_ADDRESS etc.
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-}"
export TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}"
export TEMPORAL_TASK_QUEUE="${TEMPORAL_TASK_QUEUE:-}"

echo "==> Triggering FineTuneWorkflow"
echo "    config : ${CONFIG}"
echo "    env    : ${ENV}"
echo "    address: ${TEMPORAL_ADDRESS:-temporal-server.${ENV}.internal:443 (default)}"
[[ "${DRY_RUN}" == "true" ]] && echo "    [dry-run]"
echo ""

cd "${ROOT_DIR}"
python -m temporal.client.trigger_workflow "${ARGS[@]}"
