variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "app_name" {
  description = "Application name"
  type        = string
}

variable "environment" {
  description = "Environment name (stage or prod)"
  type        = string
}

variable "bucket_names" {
  description = "Map of bucket purpose → full bucket name (output of the gcs module). Required for scoped storage IAM per §7.2."
  type        = map(string)
}

variable "github_repo" {
  description = "GitHub repo in `org/name` form, used to constrain the Workload Identity Federation principal"
  type        = string
}

variable "enable_workload_identity_federation" {
  description = "If true, provision the WIF pool + provider + tf-deploy binding (§7.1). The legacy SA-key path keeps working either way."
  type        = bool
  default     = true
}
