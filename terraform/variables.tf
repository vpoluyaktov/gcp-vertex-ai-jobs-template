variable "project_id" {
  description = "GCP project ID (dfh-stage-id or dfh-prod-id)"
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

# -----------------------------------------------------------------------------
# Self-hosted Temporal on Cloud Run + Cloud SQL (ARCHITECTURE.md §12.6).
# The previous Temporal Cloud path (var.temporal_cloud) was rejected in design
# review and has been removed.
# -----------------------------------------------------------------------------

variable "temporal_namespace" {
  description = "Temporal namespace name (recommended value: \"default\")"
  type        = string
}

variable "temporal_server_image" {
  description = "Container image for the self-hosted Temporal server. Architect publishes a digest-pinned reference of temporalio/auto-setup:1.25; per-env override allowed."
  type        = string
}

variable "cloud_sql_tier" {
  description = "Cloud SQL instance tier backing Temporal persistence (recommended: db-custom-2-7680 — 2 vCPU, 7.5 GiB)"
  type        = string
}

variable "cloud_sql_disk_gb" {
  description = "Postgres disk size in GB (recommended: 20)"
  type        = number
}

# -----------------------------------------------------------------------------
# Networking (REQUIRED — no longer optional). VPC + subnet + Serverless VPC
# Access Connector + Private Service Access are mandatory so the Cloud Run
# worker and temporal-server can reach Cloud SQL over a private IP.
# -----------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR for the env subnet (e.g. 10.20.0.0/24 stage, 10.30.0.0/24 prod)"
  type        = string
}

variable "vpc_connector_cidr" {
  description = "/28 CIDR reserved for the Serverless VPC Access Connector (e.g. 10.20.1.0/28 stage, 10.30.1.0/28 prod)"
  type        = string
}

# -----------------------------------------------------------------------------
# Feature flags (optional).
# -----------------------------------------------------------------------------

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
