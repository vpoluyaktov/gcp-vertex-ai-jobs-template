variable "project_id" {
  description = "GCP project ID (e.g. dfh-stage-id)"
  type        = string
}

variable "region" {
  description = "Default GCP region"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment name: stage or prod"
  type        = string
}

variable "app_name" {
  description = "Application name — drives all resource naming"
  type        = string
}

variable "tf_state_bucket" {
  description = "GCS bucket holding the Terraform state for this environment"
  type        = string
}

variable "dns_zone_project" {
  description = "GCP project hosting the Cloud DNS zone (dfh-ops-id)"
  type        = string
}

variable "dns_zone_name" {
  description = "Cloud DNS managed zone name (demo-devops-for-hire-com)"
  type        = string
}

variable "custom_domain" {
  description = "Public hostname reserved for this environment (used by a future serving deployment)"
  type        = string
}

variable "temporal_cloud" {
  description = "If true, use Temporal Cloud and skip the self-hosted module"
  type        = bool
  default     = false
}

variable "temporal_namespace" {
  description = "Temporal Cloud namespace (required only when temporal_cloud=true)"
  type        = string
  default     = ""
}

variable "vpc_enabled" {
  description = "If true, create VPC + private service access for private Vertex AI endpoints"
  type        = bool
  default     = false
}

variable "enable_w_and_b" {
  description = "If true, provision the wandb-api-key secret in Secret Manager"
  type        = bool
  default     = false
}

variable "notification_channels" {
  description = "Cloud Monitoring notification channel resource IDs (alerts route here)"
  type        = list(string)
  default     = []
}

variable "worker_image_tag" {
  description = "Tag for the Temporal worker image deployed to Cloud Run"
  type        = string
  default     = "latest"
}

variable "enable_firestore_audit" {
  description = "If true, mirror audit-relevant workflow events to Firestore"
  type        = bool
  default     = false
}
