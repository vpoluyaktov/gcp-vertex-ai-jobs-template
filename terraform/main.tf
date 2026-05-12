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
# are defined. Mandatory apply order (enforced by depends_on per §12.1):
#   networking → cloud_sql → temporal_server → cloud_run_worker
# Full sequence:
#   networking → gcs → iam → artifact_registry → secret_manager
#   → cloud_sql → temporal_server → vertex_ai → cloud_run_worker
#   → cloud_build → scheduler → monitoring
#
# module "networking" {
#   source             = "./modules/networking"
#   project_id         = var.project_id
#   app_name           = var.app_name
#   environment        = var.environment
#   region             = var.region
#   vpc_cidr           = var.vpc_cidr
#   vpc_connector_cidr = var.vpc_connector_cidr
# }
#
# module "cloud_sql" {
#   source            = "./modules/cloud_sql"
#   project_id        = var.project_id
#   app_name          = var.app_name
#   environment       = var.environment
#   region            = var.region
#   tier              = var.cloud_sql_tier
#   disk_gb           = var.cloud_sql_disk_gb
#   private_network   = module.networking.vpc_self_link
#   depends_on        = [module.networking]
# }
#
# module "temporal_server" {
#   source             = "./modules/temporal_server"
#   project_id         = var.project_id
#   app_name           = var.app_name
#   environment        = var.environment
#   region             = var.region
#   image              = var.temporal_server_image
#   temporal_namespace = var.temporal_namespace
#   cloud_sql_instance = module.cloud_sql.instance_connection_name
#   db_password_secret = module.secret_manager.temporal_postgres_password_secret_id
#   vpc_connector      = module.networking.vpc_connector_self_link
#   depends_on         = [module.cloud_sql]
# }
#
# ...etc.
