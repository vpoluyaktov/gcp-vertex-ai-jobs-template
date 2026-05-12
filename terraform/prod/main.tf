###############################################################################
# Production root module.
#
# Backend, provider, and variable conventions mirror the reference templates
# (gcp-cloudrun-template, gcp-clouddeploy-gke-template):
#   - Per-environment state bucket (backend.tf in this directory).
#   - Project ID and all env-specific values come from terraform.tfvars.
#   - CI authenticates with GCP_PROD_SA_KEY (see .github/workflows/deploy-prod.yml).
#
# Module wiring follows ARCHITECTURE.md §12.1. Module bodies are intentionally
# TODO placeholders today — see terraform/modules/*/main.tf. As each module
# lands, replace the corresponding stub block below with the real invocation.
###############################################################################

# Built-in sentinel — keeps `terraform plan` producing a non-empty graph until
# the real modules land. Uses terraform_data so no extra provider is required.
# Safe to remove once any real module is wired in.
resource "terraform_data" "scaffold_sentinel" {
  input = {
    environment = var.environment
    app_name    = var.app_name
    project_id  = var.project_id
  }
}

# TODO(devops): wire modules per ARCHITECTURE.md §12.1 once module variables
# are defined. Suggested order: gcs → iam → artifact_registry → secret_manager
# → vertex_ai → cloud_run_worker → cloud_build → scheduler → networking
# → monitoring.
#
# module "gcs_buckets" {
#   source      = "../modules/gcs"
#   project_id  = var.project_id
#   app_name    = var.app_name
#   environment = var.environment
# }
#
# module "iam" {
#   source      = "../modules/iam"
#   project_id  = var.project_id
#   app_name    = var.app_name
#   environment = var.environment
# }
#
# ...etc.
