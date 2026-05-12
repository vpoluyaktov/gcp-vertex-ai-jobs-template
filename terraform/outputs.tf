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

# TODO(devops): re-expose module outputs (bucket names, SA emails, AR repo URLs,
# secret IDs, TensorBoard resource name, Cloud Run worker URL, …) once the
# modules are wired in main.tf.
