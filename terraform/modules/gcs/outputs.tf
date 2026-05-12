output "bucket_names" {
  description = "Map of bucket purpose (e.g. \"checkpoints\") → full bucket name. Consumed by the iam module for scoped bindings."
  value       = { for k, b in google_storage_bucket.buckets : k => b.name }
}

output "bucket_self_links" {
  description = "Map of bucket purpose → self_link"
  value       = { for k, b in google_storage_bucket.buckets : k => b.self_link }
}

output "raw_documents_bucket" {
  description = "Bucket holding customer-supplied source documents"
  value       = google_storage_bucket.buckets["raw-documents"].name
}

output "processed_datasets_bucket" {
  description = "Bucket holding output of the data_prep pipeline (JSONL)"
  value       = google_storage_bucket.buckets["processed-datasets"].name
}

output "checkpoints_bucket" {
  description = "Bucket holding training checkpoints (transient, 14d retention)"
  value       = google_storage_bucket.buckets["checkpoints"].name
}

output "final_models_bucket" {
  description = "Bucket holding adapters, merged weights, eval scores"
  value       = google_storage_bucket.buckets["final-models"].name
}

output "build_artifacts_bucket" {
  description = "Bucket holding Cloud Build logs and SBOMs"
  value       = google_storage_bucket.buckets["build-artifacts"].name
}

output "configs_bucket" {
  description = "Bucket mirroring configs/jobs/*.yaml"
  value       = google_storage_bucket.buckets["configs"].name
}

output "temporal_data_bucket" {
  description = "Bucket for Temporal worker scratch data"
  value       = google_storage_bucket.buckets["temporal-data"].name
}
