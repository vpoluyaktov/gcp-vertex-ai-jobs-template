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
# Cloud SQL — Postgres backing the Temporal server. Depends on networking via
# psa_dependency so the PSA peering exists before the private-IP instance is
# created.
# -----------------------------------------------------------------------------

module "cloud_sql" {
  source = "./modules/cloud_sql"

  project_id          = var.project_id
  region              = var.region
  app_name            = var.app_name
  environment         = var.environment
  tier                = var.cloud_sql_tier
  disk_gb             = var.cloud_sql_disk_gb
  network_id          = module.networking.network_id
  psa_dependency      = module.networking.private_service_connection
  deletion_protection = var.environment == "prod"
  labels              = local.common_labels

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# Temporal server — Cloud Run Service running auto-setup. Depends on cloud_sql
# (private IP + password secret) and on iam (the temporal-server SA).
# -----------------------------------------------------------------------------

module "temporal_server" {
  source = "./modules/temporal_server"

  project_id            = var.project_id
  region                = var.region
  app_name              = var.app_name
  environment           = var.environment
  image                 = var.temporal_server_image
  temporal_namespace    = var.temporal_namespace
  service_account_email = module.iam.temporal_server_sa_email

  vpc_connector_id  = module.networking.connector_self_link
  network_self_link = module.networking.network_self_link

  cloud_sql_private_ip               = module.cloud_sql.private_ip_address
  cloud_sql_instance_connection_name = module.cloud_sql.instance_connection_name
  db_user                            = module.cloud_sql.database_user
  db_name                            = module.cloud_sql.temporal_database_name
  db_visibility_name                 = module.cloud_sql.temporal_visibility_database_name
  db_password_secret_id              = module.cloud_sql.password_secret_id
  db_password_secret_name            = module.cloud_sql.password_secret_name
  db_password_secret_version         = module.cloud_sql.password_secret_version

  labels = local.common_labels

  depends_on = [module.cloud_sql]
}

# -----------------------------------------------------------------------------
# Secret Manager — §8 inventory of optional secrets. Payloads are populated
# out-of-band via gcloud / scripts/bootstrap_secrets.sh — Terraform creates
# empty placeholders only. temporal-postgres-password is owned by cloud_sql.
# -----------------------------------------------------------------------------

module "secret_manager" {
  source = "./modules/secret_manager"

  project_id  = var.project_id
  app_name    = var.app_name
  environment = var.environment
  labels      = local.common_labels

  # Most secrets are off by default; flip per-env via the root variables when
  # the corresponding workflow option is in use.
  enable_hf_token            = false
  enable_w_and_b             = var.enable_w_and_b
  enable_slack_webhook       = false
  enable_sendgrid_api_key    = false
  enable_github_pat_readonly = false

  training_sa_email   = module.iam.training_sa_email
  worker_sa_email     = module.iam.worker_sa_email
  cloudbuild_sa_email = module.iam.cloudbuild_sa_email

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# Vertex AI — TensorBoard instance (per §15.0 single source of truth for
# training metrics) + optional ML Metadata store backing Experiments/lineage.
# Model Registry itself has no static resource — models are uploaded at
# training time by the SDK.
# -----------------------------------------------------------------------------

module "vertex_ai" {
  source = "./modules/vertex_ai"

  project_id  = var.project_id
  region      = var.region
  app_name    = var.app_name
  environment = var.environment
  labels      = local.common_labels

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# TODO (next tasks): remaining modules per ARCHITECTURE.md §12.1:
#   cloud_run_worker → cloud_build → scheduler → monitoring
