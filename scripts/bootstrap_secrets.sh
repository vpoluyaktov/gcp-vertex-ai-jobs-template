#!/usr/bin/env bash
# bootstrap_secrets.sh — populate Secret Manager secrets created by Terraform
#
# Terraform creates secret *placeholders* without payloads.  This script
# prompts for each secret value and uploads it via `gcloud secrets versions add`.
# Secret payloads are read from stdin (never echo'd to the terminal or shell
# history) using the `--data-file=-` pattern recommended in ARCHITECTURE.md §8.
#
# Usage:
#   ./scripts/bootstrap_secrets.sh --env stage|prod [--project <id>]
#
# Secrets populated:
#   temporal-postgres-password  (required — Temporal server DB password)
#   hf-token                    (optional — only if upload_hf_hub=true)
#   wandb-api-key               (optional — only if enable_w_and_b=true)
#   slack-webhook               (optional — Slack notifications)
#   github-pat-readonly         (optional — private GitHub submodules)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -f "${ROOT_DIR}/.env" ]] && source "${ROOT_DIR}/.env"

# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") --env stage|prod [--project <id>] [--skip-optional]

Options:
  --env             stage | prod                       (required)
  --project         GCP project ID                     (default: from .env)
  --skip-optional   Only set the required secrets, skip optional ones
  -h, --help        Print this message

Secrets set:
  REQUIRED:
    temporal-postgres-password  Temporal PostgreSQL password
  OPTIONAL (prompted unless --skip-optional):
    hf-token                    Hugging Face Hub token (for upload_hf_hub=true)
    wandb-api-key               W&B API key (for W&B integration)
    slack-webhook               Slack incoming webhook URL (for notifications)
    github-pat-readonly         GitHub PAT for private repo access in Cloud Build

Secrets are entered interactively (input is hidden).
Values are never written to disk, shell history, or logs.

Examples:
  ./scripts/bootstrap_secrets.sh --env stage
  ./scripts/bootstrap_secrets.sh --env prod --skip-optional
EOF
}

# ---------------------------------------------------------------------------
ENV=""
PROJECT_OVERRIDE=""
SKIP_OPTIONAL=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)            ENV="$2";              shift 2 ;;
    --project)        PROJECT_OVERRIDE="$2"; shift 2 ;;
    --skip-optional)  SKIP_OPTIONAL=true;    shift ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -z "${ENV}" ]] && { echo "ERROR: --env is required" >&2; usage; exit 1; }
[[ "${ENV}" != "stage" && "${ENV}" != "prod" ]] && {
  echo "ERROR: --env must be 'stage' or 'prod'" >&2; exit 1
}

if [[ -n "${PROJECT_OVERRIDE}" ]]; then
  PROJECT="${PROJECT_OVERRIDE}"
elif [[ "${ENV}" == "stage" ]]; then
  PROJECT="${GCP_PROJECT_ID_STAGE:-dfh-stage-id}"
else
  PROJECT="${GCP_PROJECT_ID_PROD:-dfh-prod-id}"
fi

echo "==> Bootstrapping secrets for env=${ENV}  project=${PROJECT}"
echo "    (Terraform must have run 'terraform apply' first to create the secret resources.)"
echo ""

# ---------------------------------------------------------------------------
# Helper: prompt and upload one secret
# ---------------------------------------------------------------------------
_set_secret() {
  local SECRET_NAME="$1"
  local DESCRIPTION="$2"
  local REQUIRED="${3:-false}"
  local CURRENT_VERSIONS

  # Check the secret resource exists (Terraform must have created it).
  if ! gcloud secrets describe "${SECRET_NAME}" --project="${PROJECT}" \
      >/dev/null 2>&1; then
    if [[ "${REQUIRED}" == "true" ]]; then
      echo "ERROR: Secret '${SECRET_NAME}' does not exist in project ${PROJECT}." >&2
      echo "       Run 'terraform apply' first to create secret resources." >&2
      exit 1
    else
      echo "    SKIP ${SECRET_NAME} — secret resource not found (Terraform may have skipped it)"
      return
    fi
  fi

  echo "  ── ${SECRET_NAME}"
  echo "     ${DESCRIPTION}"

  # Show how many versions already exist.
  CURRENT_VERSIONS=$(gcloud secrets versions list "${SECRET_NAME}" \
    --project="${PROJECT}" --format="value(name)" 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${CURRENT_VERSIONS}" -gt 0 ]]; then
    echo "     (${CURRENT_VERSIONS} version(s) already exist — a new version will be added)"
  fi

  # Read the value with no echo.
  local VALUE
  read -r -s -p "     Enter value (leave empty to skip): " VALUE
  echo ""  # newline after hidden input

  if [[ -z "${VALUE}" ]]; then
    echo "     Skipped."
    echo ""
    return
  fi

  # Upload via --data-file=- (value never touches the filesystem).
  printf '%s' "${VALUE}" | \
    gcloud secrets versions add "${SECRET_NAME}" \
      --project="${PROJECT}" \
      --data-file=-
  echo "     ✓ Version added."
  echo ""
}

# ---------------------------------------------------------------------------
# Required secrets
# ---------------------------------------------------------------------------
echo "── Required secrets ──────────────────────────────────"
echo ""
_set_secret "temporal-postgres-password" \
  "Password for the Temporal PostgreSQL database (Cloud SQL)." \
  "true"

# ---------------------------------------------------------------------------
# Optional secrets
# ---------------------------------------------------------------------------
if [[ "${SKIP_OPTIONAL}" != "true" ]]; then
  echo "── Optional secrets ──────────────────────────────────"
  echo "   (Press Enter to skip any you don't need yet.)"
  echo ""

  _set_secret "hf-token" \
    "Hugging Face Hub token — required only when artifacts.upload_hf_hub=true." \
    "false"

  _set_secret "wandb-api-key" \
    "Weights & Biases API key — required only when enable_w_and_b=true in Terraform." \
    "false"

  _set_secret "slack-webhook" \
    "Slack incoming webhook URL for training-complete / failure notifications." \
    "false"

  _set_secret "github-pat-readonly" \
    "GitHub PAT (read-only) for Cloud Build access to private GitHub repos." \
    "false"
fi

# ---------------------------------------------------------------------------
echo "==> Done.  Secrets bootstrapped for ${ENV} (${PROJECT})."
echo ""
echo "    To verify:"
echo "      gcloud secrets list --project=${PROJECT}"
echo "      gcloud secrets versions list <secret-name> --project=${PROJECT}"
