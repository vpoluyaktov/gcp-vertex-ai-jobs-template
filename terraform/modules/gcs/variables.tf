variable "project_id" {
  description = "GCP project ID — used as the bucket-name prefix per ARCHITECTURE.md §6"
  type        = string
}

variable "region" {
  description = "Bucket location. ARCHITECTURE.md treats buckets as multi-region (US) for resilience; override per-env if needed."
  type        = string
  default     = "US"
}

variable "app_name" {
  description = "Application name (used as a label, not in the bucket name itself per §6)"
  type        = string
}

variable "environment" {
  description = "Environment name (stage or prod) — applied as a label"
  type        = string
}

variable "labels" {
  description = "Additional labels merged onto every bucket"
  type        = map(string)
  default     = {}
}
