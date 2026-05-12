variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "AR repository region"
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

variable "training_sa_email" {
  description = "Training SA email — granted push access to training-images"
  type        = string
}

variable "serving_sa_email" {
  description = "Serving SA email — granted pull access to serving-images"
  type        = string
}

variable "cloudbuild_sa_email" {
  description = "Cloud Build SA email — granted push access to both repositories"
  type        = string
}

variable "worker_sa_email" {
  description = "Temporal worker SA — granted pull access to both repositories (the worker triggers builds and reads images for deploys)"
  type        = string
}

variable "labels" {
  description = "Labels merged onto each repository"
  type        = map(string)
  default     = {}
}
