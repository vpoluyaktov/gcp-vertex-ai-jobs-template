output "tensorboard_id" {
  description = "TensorBoard short id (auto-assigned numeric ID by Vertex AI)"
  value       = google_vertex_ai_tensorboard.main.id
}

output "tensorboard_resource_name" {
  description = "Full TensorBoard resource name (projects/PROJECT_NUM/locations/REGION/tensorboards/TB_ID). Read by train.py via aiplatform.Tensorboard(...) per §15.0."
  value       = google_vertex_ai_tensorboard.main.name
}

output "tensorboard_region" {
  description = "Region the TensorBoard instance lives in"
  value       = google_vertex_ai_tensorboard.main.region
}

output "tensorboard_display_name" {
  description = "Display name shown in the Cloud Console TensorBoard list"
  value       = google_vertex_ai_tensorboard.main.display_name
}

output "metadata_store_name" {
  description = "ML Metadata store resource name. Currently always empty — the provider has no `google_vertex_ai_metadata_store` resource, so the `default` store is auto-created by the Vertex AI SDK on first use. Output kept for forward-compatibility."
  value       = ""
}
