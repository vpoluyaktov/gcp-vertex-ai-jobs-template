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
# Modules currently wired: apis, networking, gcs, iam, artifact_registry.
# Still TODO (per ARCHITECTURE.md §12.1): secret_manager, cloud_sql,
# temporal_server, vertex_ai, cloud_run_worker, cloud_build, scheduler,
# monitoring.
###############################################################################

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# Common labels merged onto every labelable resource that supports them.
locals {
  common_labels = {
    app = var.app_name
    env = var.environment
  }
}

# -----------------------------------------------------------------------------
# Networking — no upstream deps. Other modules (cloud_sql, temporal_server,
# cloud_run_worker) depend on its outputs.
# -----------------------------------------------------------------------------

module "networking" {
  source = "./modules/networking"

  project_id         = var.project_id
  region             = var.region
  app_name           = var.app_name
  environment        = var.environment
  vpc_cidr           = var.vpc_cidr
  vpc_connector_cidr = var.vpc_connector_cidr
  labels             = local.common_labels

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# GCS — 7 buckets per ARCHITECTURE.md §6.
# -----------------------------------------------------------------------------

module "gcs" {
  source = "./modules/gcs"

  project_id  = var.project_id
  app_name    = var.app_name
  environment = var.environment
  labels      = local.common_labels

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# IAM — 7 SAs per §7, scoped bucket bindings per §7.2, optional WIF per §7.1.
# Depends on gcs because storage IAM is bucket-scoped (§7.2).
# -----------------------------------------------------------------------------

module "iam" {
  source = "./modules/iam"

  project_id   = var.project_id
  app_name     = var.app_name
  environment  = var.environment
  bucket_names = module.gcs.bucket_names
  github_repo  = "vpoluyaktov/${var.app_name}"

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# Artifact Registry — 2 Docker repos with scoped per-repo IAM.
# -----------------------------------------------------------------------------

module "artifact_registry" {
  source = "./modules/artifact_registry"

  project_id          = var.project_id
  region              = var.region
  app_name            = var.app_name
  environment         = var.environment
  labels              = local.common_labels
  training_sa_email   = module.iam.training_sa_email
  serving_sa_email    = module.iam.serving_sa_email
  cloudbuild_sa_email = module.iam.cloudbuild_sa_email
  worker_sa_email     = module.iam.worker_sa_email

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# TODO (next tasks): wire the remaining modules.
# Mandatory apply order (enforced by depends_on per §12.1):
#   networking → cloud_sql → temporal_server → cloud_run_worker
# Full sequence:
#   networking → gcs → iam → artifact_registry → secret_manager
#   → cloud_sql → temporal_server → vertex_ai → cloud_run_worker
#   → cloud_build → scheduler → monitoring
#
# module "secret_manager" {
#   source                  = "./modules/secret_manager"
#   project_id              = var.project_id
#   app_name                = var.app_name
#   environment             = var.environment
#   enable_w_and_b          = var.enable_w_and_b
#   temporal_server_sa_email = module.iam.service_account_emails["worker"] # placeholder
# }
#
# module "cloud_sql" {
#   source             = "./modules/cloud_sql"
#   project_id         = var.project_id
#   region             = var.region
#   app_name           = var.app_name
#   environment        = var.environment
#   tier               = var.cloud_sql_tier
#   disk_gb            = var.cloud_sql_disk_gb
#   network_self_link  = module.networking.network_self_link
#   psa_dependency     = module.networking.private_service_connection
#   db_password_secret = module.secret_manager.temporal_postgres_password_secret_id
# }
#
# module "temporal_server" {
#   source              = "./modules/temporal_server"
#   project_id          = var.project_id
#   region              = var.region
#   app_name            = var.app_name
#   environment         = var.environment
#   image               = var.temporal_server_image
#   temporal_namespace  = var.temporal_namespace
#   cloud_sql_instance  = module.cloud_sql.instance_connection_name
#   db_private_ip       = module.cloud_sql.private_ip_address
#   db_password_secret  = module.secret_manager.temporal_postgres_password_secret_id
#   vpc_connector       = module.networking.connector_self_link
#   service_account     = module.iam.service_account_emails["worker"] # placeholder until temporal-server SA exists
# }
