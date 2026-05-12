# TODO: implement Secret Manager module.
#
# Per ARCHITECTURE.md §8, this module provisions empty Secret Manager secrets
# (payloads are populated out-of-band — never stored in Terraform state).
#
# Secret list:
#   - `temporal-postgres-password`  (REQUIRED, always — generated at apply time
#                                    by random_password and stored here; mounted
#                                    into the temporal-server Cloud Run Service)
#   - `hf-token`                    (optional — only when artifacts.upload_hf_hub=true)
#   - `wandb-api-key`               (optional — only when var.enable_w_and_b)
#   - `slack-webhook`               (optional)
#   - `sendgrid-api-key`            (optional)
#   - `github-pat-readonly`         (optional — Cloud Build → private repos)
#
# REMOVED in design review: temporal-api-key, temporal-tls-cert, temporal-tls-key.
# Temporal is now self-hosted in-VPC (§12.6); mTLS to Temporal is disabled inside
# the VPC and there is no Temporal Cloud dependency.
#
# Required inputs (TODO: declare in variables.tf):
#   project_id, app_name, environment, enable_w_and_b,
#   enable_optional_secrets (slack/sendgrid/github-pat)
#
# Required outputs (TODO: declare in outputs.tf):
#   temporal_postgres_password_secret_id  (consumed by temporal_server module)
#   secret_ids (map of secret_name → secret_id)
