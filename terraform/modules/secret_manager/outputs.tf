output "secret_ids" {
  description = "Map of bare secret name (e.g. \"hf-token\") → full Secret Manager resource id (projects/PROJECT/secrets/NAME). Only enabled secrets appear."
  value       = { for k, s in google_secret_manager_secret.secret : k => s.id }
}

output "secret_short_names" {
  description = "Map of bare secret name → secret_id (the short form). Cloud Run env value_source.secret_key_ref expects this form."
  value       = { for k, s in google_secret_manager_secret.secret : k => s.secret_id }
}

output "hf_token_secret_id" {
  description = "Full Secret Manager id for hf-token (empty when disabled)"
  value       = var.enable_hf_token ? google_secret_manager_secret.secret["hf-token"].id : ""
}

output "wandb_api_key_secret_id" {
  description = "Full Secret Manager id for wandb-api-key (empty when disabled)"
  value       = var.enable_w_and_b ? google_secret_manager_secret.secret["wandb-api-key"].id : ""
}

output "slack_webhook_secret_id" {
  description = "Full Secret Manager id for slack-webhook (empty when disabled)"
  value       = var.enable_slack_webhook ? google_secret_manager_secret.secret["slack-webhook"].id : ""
}

output "sendgrid_api_key_secret_id" {
  description = "Full Secret Manager id for sendgrid-api-key (empty when disabled)"
  value       = var.enable_sendgrid_api_key ? google_secret_manager_secret.secret["sendgrid-api-key"].id : ""
}

output "github_pat_readonly_secret_id" {
  description = "Full Secret Manager id for github-pat-readonly (empty when disabled)"
  value       = var.enable_github_pat_readonly ? google_secret_manager_secret.secret["github-pat-readonly"].id : ""
}
