variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for regional resources (subnet, VPC connector)"
  type        = string
}

variable "app_name" {
  description = "Application name — drives resource naming"
  type        = string
}

variable "environment" {
  description = "Environment name (stage or prod)"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the env subnet (e.g. 10.20.0.0/24)"
  type        = string
}

variable "vpc_connector_cidr" {
  description = "/28 CIDR for the Serverless VPC Access Connector"
  type        = string
}

variable "psa_range_prefix_length" {
  description = "Prefix length for the Private Service Access allocated range (Cloud SQL peering)"
  type        = number
  default     = 16
}

variable "labels" {
  description = "Labels applied to labelable resources"
  type        = map(string)
  default     = {}
}
