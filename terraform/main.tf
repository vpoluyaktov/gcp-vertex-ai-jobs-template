###############################################################################
# Single shared root module — mirrors gcp-cloudrun-template /
# gcp-clouddeploy-gke-template.
#
# This directory is THE root module for both stage and prod. The per-env
# subdirectories (stage/, prod/) hold only `backend.tf` and `<env>.tfvars`.
# CI copies the right backend.tf in before `terraform init`:
#
#   cd terraform
#   cp stage/backend.tf backend.tf      # or prod/backend.tf
#   terraform init
#   terraform plan -var-file=stage/stage.tfvars -out=tfplan
#   terraform apply tfplan
#
# Auth uses GCP_STAGE_SA_KEY for the stage branch, GCP_PROD_SA_KEY for main.
# Module wiring follows ARCHITECTURE.md §12.1; module bodies are TODO today.
###############################################################################

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# Built-in sentinel — keeps `terraform plan` producing a non-empty graph until
# the real modules land. Uses terraform_data so no extra provider is required.
# Safe to remove once any real module is wired in below.
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
#   source      = "./modules/gcs"
#   project_id  = var.project_id
#   app_name    = var.app_name
#   environment = var.environment
# }
#
# module "iam" {
#   source      = "./modules/iam"
#   project_id  = var.project_id
#   app_name    = var.app_name
#   environment = var.environment
# }
#
# ...etc.
