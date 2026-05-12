output "worker_pool_name" {
  description = "Short worker pool name (e.g. \"gcp-vertex-ai-jobs-template-stage\")"
  value       = google_cloudbuild_worker_pool.private.name
}

output "worker_pool_id" {
  description = "Full resource id of the worker pool (projects/<project>/locations/<region>/workerPools/<name>). This is what Cloud Build YAMLs reference under options.pool.name."
  value       = "projects/${var.project_id}/locations/${var.region}/workerPools/${google_cloudbuild_worker_pool.private.name}"
}

output "worker_pool_location" {
  description = "Worker pool location"
  value       = google_cloudbuild_worker_pool.private.location
}
