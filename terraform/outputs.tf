output "project_id" {
  description = "GCP project ID for this environment"
  value       = var.project_id
}

output "environment" {
  description = "Environment name"
  value       = var.environment
}

output "region" {
  description = "Default GCP region"
  value       = var.region
}

output "app_name" {
  description = "Application name used to derive all resource names"
  value       = var.app_name
}

output "custom_domain" {
  description = "Hostname reserved for this environment"
  value       = var.custom_domain
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------

output "network_self_link" {
  description = "VPC self_link"
  value       = module.networking.network_self_link
}

output "subnet_self_link" {
  description = "Subnet self_link"
  value       = module.networking.subnet_self_link
}

output "connector_self_link" {
  description = "Serverless VPC Access Connector self_link"
  value       = module.networking.connector_self_link
}

# -----------------------------------------------------------------------------
# Storage — all 7 bucket names from §6
# -----------------------------------------------------------------------------

output "bucket_names" {
  description = "Map of bucket purpose → full bucket name"
  value       = module.gcs.bucket_names
}

# -----------------------------------------------------------------------------
# IAM — SA emails
# -----------------------------------------------------------------------------

output "service_account_emails" {
  description = "Map of SA logical key → email"
  value       = module.iam.service_account_emails
}

output "workload_identity_provider" {
  description = "WIF provider resource name (empty if WIF disabled)"
  value       = module.iam.workload_identity_provider_name
}

# -----------------------------------------------------------------------------
# Artifact Registry
# -----------------------------------------------------------------------------

output "training_images_repo_url" {
  description = "Push/pull URL for training images"
  value       = module.artifact_registry.training_images_repo_url
}

output "serving_images_repo_url" {
  description = "Push/pull URL for serving images"
  value       = module.artifact_registry.serving_images_repo_url
}

# -----------------------------------------------------------------------------
# Cloud SQL (Temporal persistence)
# -----------------------------------------------------------------------------

output "temporal_db_instance_name" {
  description = "Cloud SQL instance hosting the Temporal databases"
  value       = module.cloud_sql.instance_name
}

output "temporal_db_private_ip" {
  description = "Private IP of the Cloud SQL instance (sensitive — only reachable from inside the VPC)"
  value       = module.cloud_sql.private_ip_address
  sensitive   = true
}

output "temporal_db_password_secret" {
  description = "Secret Manager resource id for the Postgres password"
  value       = module.cloud_sql.password_secret_id
}

# -----------------------------------------------------------------------------
# Temporal server (Cloud Run)
# -----------------------------------------------------------------------------

output "temporal_server_uri" {
  description = "Cloud Run-issued URI for the Temporal server (INTERNAL ingress)"
  value       = module.temporal_server.service_uri
}

output "temporal_server_internal_hostname" {
  description = "Stable VPC-internal hostname (temporal-server.<env>.internal)"
  value       = module.temporal_server.internal_hostname
}

# -----------------------------------------------------------------------------
# Secret Manager
# -----------------------------------------------------------------------------

output "secret_ids" {
  description = "Map of bare secret name → full Secret Manager resource id (only enabled secrets appear)"
  value       = module.secret_manager.secret_ids
}

# -----------------------------------------------------------------------------
# Vertex AI
# -----------------------------------------------------------------------------

output "tensorboard_resource_name" {
  description = "Full TensorBoard resource name — passed to CustomJob.job_spec.tensorboard and consumed by train.py via aiplatform.Tensorboard(...) per §15.0"
  value       = module.vertex_ai.tensorboard_resource_name
}

output "metadata_store_name" {
  description = "ML Metadata store resource name (empty when not created — default store is used)"
  value       = module.vertex_ai.metadata_store_name
}

# -----------------------------------------------------------------------------
# Cloud Build Private Worker Pool
# -----------------------------------------------------------------------------

output "cloud_build_worker_pool_id" {
  description = "Full Cloud Build worker pool resource id — consumed by cloudbuild YAMLs via options.pool.name to run builds inside the VPC"
  value       = module.cloud_build.worker_pool_id
}
