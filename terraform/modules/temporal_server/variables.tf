variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "Cloud Run region (must match networking subnet/connector region)"
  type        = string
}

variable "app_name" {
  description = "Application name"
  type        = string
}

variable "environment" {
  description = "Environment name (stage or prod). Drives max-instances: 1 for stage, 2 for prod (§12.6.2)."
  type        = string
}

variable "image" {
  description = "Container image for the Temporal server (digest-pinned reference of temporalio/auto-setup:1.25)"
  type        = string
}

variable "temporal_namespace" {
  description = "Temporal namespace name (typically \"default\"). auto-setup creates it on first start."
  type        = string
}

variable "service_account_email" {
  description = "Service account email this Cloud Run service runs as (the temporal-server SA from the iam module)"
  type        = string
}

variable "vpc_connector_id" {
  description = "Serverless VPC Access Connector self_link (output of networking module)"
  type        = string
}

variable "cloud_sql_private_ip" {
  description = "Private IP of the Cloud SQL instance — mounted as POSTGRES_SEEDS"
  type        = string
}

variable "cloud_sql_instance_connection_name" {
  description = "Cloud SQL instance connection_name (project:region:instance). Currently informational; reserved for a future Auth Proxy sidecar."
  type        = string
}

variable "db_user" {
  description = "Postgres user name (output of cloud_sql module — typically \"temporal\")"
  type        = string
}

variable "db_name" {
  description = "Application database name (typically \"temporal\")"
  type        = string
}

variable "db_visibility_name" {
  description = "Visibility database name (typically \"temporal_visibility\")"
  type        = string
}

variable "db_password_secret_id" {
  description = "Full Secret Manager resource id for the postgres password (projects/.../secrets/temporal-postgres-password)"
  type        = string
}

variable "db_password_secret_name" {
  description = "Short secret name (just the secret_id portion, e.g. \"temporal-postgres-password\") — Cloud Run env value_source needs this form"
  type        = string
}

variable "db_password_secret_version" {
  description = "Version resource id of the postgres password — referenced by depends_on to force re-deploy when the secret rotates"
  type        = string
}

variable "create_private_dns" {
  description = "If true, create a private DNS zone <env>.internal and a CNAME temporal-server.<env>.internal → the Cloud Run hostname (§12.6.2). Off if a shared zone already exists."
  type        = bool
  default     = true
}

variable "network_self_link" {
  description = "VPC self_link — required when create_private_dns=true (private zone is scoped to this network)"
  type        = string
  default     = ""
}

variable "cpu" {
  description = "CPU allocation per instance (§12.6.2 default: 2)"
  type        = string
  default     = "2"
}

variable "memory" {
  description = "Memory per instance (§12.6.2 default: 2Gi)"
  type        = string
  default     = "2Gi"
}

variable "labels" {
  description = "Labels merged onto the Cloud Run service"
  type        = map(string)
  default     = {}
}
