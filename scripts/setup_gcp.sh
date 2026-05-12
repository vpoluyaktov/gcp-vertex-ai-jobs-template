#!/usr/bin/env bash
# setup_gcp.sh — first-time GCP project bootstrap
#
# Enables all required APIs, creates the Terraform remote-state bucket,
# and verifies ADC is configured for local development.
# Must be run once per project before `terraform init`.
#
# Usage:
#   ./scripts/setup_gcp.sh --env stage|prod [--project <project-id>] [--region <region>]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
[[ -f "${ROOT_DIR}/.env" ]] && source "${ROOT_DIR}/.env"

# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") --env stage|prod [--project <id>] [--region <region>]

Options:
  --env      stage | prod                                       (required)
  --project  GCP project ID                                     (default: GCP_PROJECT_ID_STAGE / GCP_PROJECT_ID_PROD)
  --region   GCP region                                         (default: GCP_REGION or us-central1)
  -h, --help Print this message

What this script does:
  1. Verifies Application Default Credentials (ADC)
  2. Enables all required GCP APIs
  3. Creates the Terraform remote-state GCS bucket (with versioning)
  4. Prints next steps

Examples:
  ./scripts/setup_gcp.sh --env stage
  ./scripts/setup_gcp.sh --env prod --project dfh-prod-id
EOF
}

# ---------------------------------------------------------------------------
ENV=""
PROJECT_OVERRIDE=""
REGION="${GCP_REGION:-us-central1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)       ENV="$2";              shift 2 ;;
    --project)   PROJECT_OVERRIDE="$2"; shift 2 ;;
    --region)    REGION="$2";           shift 2 ;;
    -h|--help)   usage; exit 0 ;;
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

STATE_BUCKET="${PROJECT}-tfstate"

echo "==> Bootstrap: env=${ENV}  project=${PROJECT}  region=${REGION}"

# ---------------------------------------------------------------------------
# 1. Verify ADC
# ---------------------------------------------------------------------------
echo ""
echo "==> Verifying Application Default Credentials..."
if ! gcloud auth application-default print-access-token --quiet >/dev/null 2>&1; then
  echo "    ADC not configured — running gcloud auth application-default login..."
  gcloud auth application-default login
fi
echo "    ADC OK."

# ---------------------------------------------------------------------------
# 2. Set active project
# ---------------------------------------------------------------------------
gcloud config set project "${PROJECT}" --quiet

# ---------------------------------------------------------------------------
# 3. Enable required APIs (matches terraform/apis.tf)
# ---------------------------------------------------------------------------
APIS=(
  cloudresourcemanager.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  serviceusage.googleapis.com
  storage.googleapis.com
  artifactregistry.googleapis.com
  compute.googleapis.com
  vpcaccess.googleapis.com
  servicenetworking.googleapis.com
  dns.googleapis.com
  run.googleapis.com
  aiplatform.googleapis.com
  cloudbuild.googleapis.com
  sqladmin.googleapis.com
  secretmanager.googleapis.com
  cloudscheduler.googleapis.com
  logging.googleapis.com
  monitoring.googleapis.com
  firestore.googleapis.com
)

echo ""
echo "==> Enabling ${#APIS[@]} APIs (this may take a few minutes on first run)..."
gcloud services enable "${APIS[@]}" --project="${PROJECT}"
echo "    All APIs enabled."

# ---------------------------------------------------------------------------
# 4. Create Terraform state bucket
# ---------------------------------------------------------------------------
echo ""
echo "==> Ensuring Terraform state bucket: gs://${STATE_BUCKET}"
if gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="${PROJECT}" \
    >/dev/null 2>&1; then
  echo "    Bucket already exists."
else
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="${PROJECT}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
  echo "    Bucket created."
fi

# Enable versioning so accidental `terraform destroy` state is recoverable.
gcloud storage buckets update "gs://${STATE_BUCKET}" \
  --versioning \
  --project="${PROJECT}" >/dev/null
echo "    Versioning enabled on gs://${STATE_BUCKET}."

# ---------------------------------------------------------------------------
echo ""
echo "==> Bootstrap complete for ${ENV} (${PROJECT})."
echo ""
echo "    Next steps:"
echo "      1. terraform -chdir=terraform/${ENV} init"
echo "      2. terraform -chdir=terraform/${ENV} plan"
echo "      3. terraform -chdir=terraform/${ENV} apply"
echo "      4. ./scripts/bootstrap_secrets.sh --env ${ENV}"
