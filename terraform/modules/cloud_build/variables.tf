variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "Region for the worker pool (must match the env's Cloud Run / Cloud SQL region so workers can reach private IPs without cross-region peering)"
  type        = string
}

variable "app_name" {
  description = "Application name (used in worker pool naming)"
  type        = string
}

variable "environment" {
  description = "Environment name (stage or prod)"
  type        = string
}

variable "network_self_link" {
  description = "VPC self_link to peer the worker pool into (output of networking module). Workers will be able to reach private-IP endpoints (Cloud SQL, internal Cloud Run) on this network."
  type        = string
}

variable "psa_dependency" {
  description = "Reference to the networking module's google_service_networking_connection. The worker pool's VPC peering uses the same Private Service Access plumbing as Cloud SQL, so it must exist before the pool is created."
  type        = string
}

variable "machine_type" {
  description = "Worker machine type. e2-standard-4 is enough for the trigger-workflow / image-build use cases."
  type        = string
  default     = "e2-standard-4"
}

variable "disk_size_gb" {
  description = "Worker disk size in GB. 100 GB accommodates the larger CUDA build images cached during stage 1 of Dockerfile.training.gpu."
  type        = number
  default     = 100
}

variable "labels" {
  description = "Labels merged onto the worker pool"
  type        = map(string)
  default     = {}
}
