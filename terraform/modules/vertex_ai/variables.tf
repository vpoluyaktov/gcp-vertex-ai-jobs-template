variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "Vertex AI region for the TensorBoard instance and metadata store. Both must be in the same region the CustomJobs run in."
  type        = string
}

variable "app_name" {
  description = "Application name (used in TensorBoard display name)"
  type        = string
}

variable "environment" {
  description = "Environment name (stage or prod)"
  type        = string
}

variable "labels" {
  description = "Labels merged onto Vertex AI resources that accept them"
  type        = map(string)
  default     = {}
}

variable "create_metadata_store" {
  description = "If true, create a named ML Metadata store for experiment lineage tracking. When false, the project's auto-created `default` store is used."
  type        = bool
  default     = true
}

variable "tensorboard_kms_key" {
  description = "Optional CMEK key resource name to encrypt the TensorBoard instance. Empty string disables CMEK (uses Google-managed key)."
  type        = string
  default     = ""
}
