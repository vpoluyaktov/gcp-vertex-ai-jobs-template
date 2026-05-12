variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "app_name" {
  description = "Application name (used only for labels)"
  type        = string
}

variable "environment" {
  description = "Environment name (stage or prod)"
  type        = string
}

variable "labels" {
  description = "Labels merged onto every secret"
  type        = map(string)
  default     = {}
}

# -----------------------------------------------------------------------------
# Per-secret enable flags. Every secret listed in §8 is OPTIONAL — they are
# only provisioned when the corresponding flag is true. Payloads are populated
# out-of-band; this module never writes secret_data (matching §8's contract:
# "Secret payloads are NEVER stored in Terraform state").
# Note: `temporal-postgres-password` is intentionally NOT here. It is owned by
# the cloud_sql module because its lifecycle is tied to the SQL user.
# -----------------------------------------------------------------------------

variable "enable_hf_token" {
  description = "Provision the hf-token secret (consumed by training + worker when artifacts.upload_hf_hub=true)"
  type        = bool
  default     = false
}

variable "enable_w_and_b" {
  description = "Provision the wandb-api-key secret (consumed by training when logging profile is `wandb`)"
  type        = bool
  default     = false
}

variable "enable_slack_webhook" {
  description = "Provision the slack-webhook secret (consumed by worker for Step 9 notifications)"
  type        = bool
  default     = false
}

variable "enable_sendgrid_api_key" {
  description = "Provision the sendgrid-api-key secret (consumed by worker for email notifications)"
  type        = bool
  default     = false
}

variable "enable_github_pat_readonly" {
  description = "Provision the github-pat-readonly secret (consumed by cloudbuild for private submodule pulls)"
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# Consumer SA emails — the `iam` module's outputs are passed in. We use these
# to grant scoped secretmanager.secretAccessor per §7.2 (no project-level
# secretmanager.secretAccessor is granted from this module).
# -----------------------------------------------------------------------------

variable "training_sa_email" {
  description = "Training SA — needs read on hf-token (optional) and wandb-api-key (optional)"
  type        = string
}

variable "worker_sa_email" {
  description = "Worker SA — needs read on hf-token, slack-webhook, sendgrid-api-key (all optional)"
  type        = string
}

variable "cloudbuild_sa_email" {
  description = "Cloud Build SA — needs read on github-pat-readonly (optional)"
  type        = string
}
