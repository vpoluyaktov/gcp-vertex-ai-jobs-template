variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "Cloud SQL region"
  type        = string
}

variable "app_name" {
  description = "Application name (for resource naming + labels)"
  type        = string
}

variable "environment" {
  description = "Environment name (stage or prod). Drives availability_type: ZONAL for stage, REGIONAL for prod."
  type        = string
}

variable "tier" {
  description = "Cloud SQL instance tier (e.g. db-custom-2-7680). Per ARCHITECTURE.md §12.6.1."
  type        = string
}

variable "disk_gb" {
  description = "Postgres disk size in GB. Autoresize is always enabled."
  type        = number
}

variable "network_id" {
  description = "VPC network id (full resource id) — used as the Cloud SQL private_network"
  type        = string
}

variable "psa_dependency" {
  description = "Reference to the networking module's google_service_networking_connection. Forces apply order: PSA must exist before the SQL instance can be created with a private IP."
  type        = string
}

variable "deletion_protection" {
  description = "If true, deletion via Terraform requires explicit override. Recommended on for prod, off for stage."
  type        = bool
  default     = true
}

variable "labels" {
  description = "Labels merged onto the SQL instance"
  type        = map(string)
  default     = {}
}
