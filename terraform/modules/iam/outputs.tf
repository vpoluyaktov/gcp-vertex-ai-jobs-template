output "service_account_emails" {
  description = "Map of SA logical key (tf-deploy, worker, training, …) → email"
  value       = { for k, sa in google_service_account.sa : k => sa.email }
}

output "tf_deploy_sa_email" {
  description = "Terraform deploy SA — stored as GCP_<ENV>_SA_KEY or impersonated via WIF"
  value       = google_service_account.sa["tf-deploy"].email
}

output "worker_sa_email" {
  description = "Temporal worker SA"
  value       = google_service_account.sa["worker"].email
}

output "training_sa_email" {
  description = "Vertex AI training SA (CustomJob runtime)"
  value       = google_service_account.sa["training"].email
}

output "serving_sa_email" {
  description = "Serving runtime SA (consumed by the future serving template)"
  value       = google_service_account.sa["serving"].email
}

output "cloudbuild_sa_email" {
  description = "Cloud Build SA"
  value       = google_service_account.sa["cloudbuild"].email
}

output "ingest_sa_email" {
  description = "Data ingestion SA"
  value       = google_service_account.sa["ingest"].email
}

output "scheduler_sa_email" {
  description = "Cloud Scheduler SA"
  value       = google_service_account.sa["scheduler"].email
}

output "temporal_server_sa_email" {
  description = "Temporal server SA — Cloud Run Service identity for temporalio/auto-setup (§12.6.4)"
  value       = google_service_account.sa["temporal-server"].email
}

output "workload_identity_pool_name" {
  description = "Full WIF pool resource name (empty string when WIF is disabled)"
  value       = var.enable_workload_identity_federation ? google_iam_workload_identity_pool.github[0].name : ""
}

output "workload_identity_provider_name" {
  description = "Full WIF provider resource name (empty string when WIF is disabled)"
  value       = var.enable_workload_identity_federation ? google_iam_workload_identity_pool_provider.github[0].name : ""
}
