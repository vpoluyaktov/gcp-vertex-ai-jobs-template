output "training_images_repo_name" {
  description = "AR repository ID for training images"
  value       = google_artifact_registry_repository.repo["training-images"].name
}

output "serving_images_repo_name" {
  description = "AR repository ID for serving images"
  value       = google_artifact_registry_repository.repo["serving-images"].name
}

output "training_images_repo_url" {
  description = "Fully-qualified registry URL for pushing/pulling training images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo["training-images"].name}"
}

output "serving_images_repo_url" {
  description = "Fully-qualified registry URL for pushing/pulling serving images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo["serving-images"].name}"
}

output "repo_ids" {
  description = "Map of logical name → AR repository_id (i.e. the bare name like \"training-images\")"
  value       = { for k, r in google_artifact_registry_repository.repo : k => r.repository_id }
}
