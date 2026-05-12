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
