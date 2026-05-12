###############################################################################
# Secret Manager module — §8 inventory.
#
# Contract (§8):
#   - This module creates EMPTY Secret Manager placeholders.
#   - Secret payloads are populated out-of-band by an operator running
#     `gcloud secrets versions add <name> --data-file=-` (or the
#     scripts/bootstrap_secrets.sh helper). They are NEVER stored in Terraform
#     state.
#   - Every secret is optional and gated by an enable flag.
#   - IAM is scoped per §7.2: `secretmanager.secretAccessor` is granted on each
#     individual secret to only the SA(s) that legitimately need it. Project-
#     level secretmanager.secretAccessor is NOT granted here (or anywhere —
#     the iam module deliberately omits it for training/worker SAs).
#   - `temporal-postgres-password` is owned by the cloud_sql module (lifecycle
#     coupled to the SQL user); it is intentionally absent from this list.
###############################################################################

locals {
  common_labels = merge(
    {
      app = var.app_name
      env = var.environment
    },
    var.labels,
  )

  # Map SA role labels (stable, plan-time known) to their email (only known
  # after the iam module's google_service_account resources are created).
  # The role label is what we use as part of for_each keys; the email is
  # only consumed inside the resource body.
  sa_email_by_role = {
    training   = var.training_sa_email
    worker     = var.worker_sa_email
    cloudbuild = var.cloudbuild_sa_email
  }

  # Secret spec: enabled flag + list of role-label consumers (NOT emails).
  # Keys are the bare secret IDs from §8. Using role labels keeps the
  # for_each map keys plan-time known even though SA emails aren't.
  secret_specs = {
    "hf-token" = {
      enabled        = var.enable_hf_token
      consumer_roles = ["training", "worker"]
    }
    "wandb-api-key" = {
      enabled        = var.enable_w_and_b
      consumer_roles = ["training"]
    }
    "slack-webhook" = {
      enabled        = var.enable_slack_webhook
      consumer_roles = ["worker"]
    }
    "sendgrid-api-key" = {
      enabled        = var.enable_sendgrid_api_key
      consumer_roles = ["worker"]
    }
    "github-pat-readonly" = {
      enabled        = var.enable_github_pat_readonly
      consumer_roles = ["cloudbuild"]
    }
  }

  # Only the enabled secrets actually become resources.
  enabled_secrets = {
    for k, v in local.secret_specs : k => v if v.enabled
  }

  # Flatten enabled-secret × consumer pairs into a for_each-friendly map.
  # The composite key uses only static strings (secret_id, role label) so
  # Terraform can determine the full set of resources at plan time even when
  # the SA emails themselves aren't yet known.
  enabled_consumer_bindings = {
    for pair in flatten([
      for secret_id, spec in local.enabled_secrets : [
        for role_label in spec.consumer_roles : {
          secret_id  = secret_id
          role_label = role_label
          role       = "roles/secretmanager.secretAccessor"
        }
      ]
    ]) :
    "${pair.secret_id}-${pair.role_label}" => pair
  }
}

resource "google_secret_manager_secret" "secret" {
  for_each = local.enabled_secrets

  project   = var.project_id
  secret_id = each.key

  replication {
    auto {}
  }

  labels = merge(
    local.common_labels,
    {
      purpose = each.key
    },
  )
}

resource "google_secret_manager_secret_iam_member" "consumers" {
  for_each = local.enabled_consumer_bindings

  project   = var.project_id
  secret_id = google_secret_manager_secret.secret[each.value.secret_id].id
  role      = each.value.role
  # sa_email_by_role lookup happens inside the resource body, so it's allowed
  # to be apply-time known.
  member = "serviceAccount:${local.sa_email_by_role[each.value.role_label]}"
}
