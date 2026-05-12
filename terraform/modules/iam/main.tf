###############################################################################
# IAM module — 7 service accounts per ARCHITECTURE.md §7 + scoped bucket
# bindings per §7.2 + optional Workload Identity Federation per §7.1.
#
# Per §7.2: storage.objectAdmin is NEVER project-level — every storage binding
# is `google_storage_bucket_iam_member` scoped to one specific bucket.
#
# Per §7, SA account_id pattern is `<purpose>-<env>` (max 30 chars including
# the env suffix).
###############################################################################

locals {
  env = var.environment

  # SA spec — keys are stable handles used elsewhere in this module's locals.
  # account_id values are pre-checked against the 30-char GCP limit.
  service_accounts = {
    tf-deploy = {
      account_id   = "tf-deploy-${local.env}"
      display_name = "Terraform deploy SA (${title(local.env)})"
    }
    worker = {
      account_id   = "worker-${local.env}"
      display_name = "Temporal worker SA (${title(local.env)})"
    }
    training = {
      account_id   = "training-${local.env}"
      display_name = "Vertex AI training SA (${title(local.env)})"
    }
    serving = {
      account_id   = "serving-${local.env}"
      display_name = "Serving runtime SA (${title(local.env)})"
    }
    cloudbuild = {
      account_id   = "cloudbuild-${local.env}"
      display_name = "Cloud Build SA (${title(local.env)})"
    }
    ingest = {
      account_id   = "ingest-${local.env}"
      display_name = "Data ingestion SA (${title(local.env)})"
    }
    scheduler = {
      account_id   = "scheduler-${local.env}"
      display_name = "Cloud Scheduler SA (${title(local.env)})"
    }
    # §12.6.4 addition for the self-hosted Temporal server Cloud Run service.
    # Scoped secretAccessor on temporal-postgres-password is granted *inside*
    # the temporal_server module (it depends on the cloud_sql module's secret),
    # so only the non-secret project-level roles live here.
    temporal-server = {
      account_id   = "temporal-server-${local.env}"
      display_name = "Temporal server SA (${title(local.env)})"
    }
  }

  # Project-level roles per §7. NO storage.objectAdmin at project level (§7.2).
  project_role_bindings = flatten([
    # tf-deploy — owner for Terraform; also explicit cloudbuild roles so CI can
    # submit builds and use private worker pools without relying on owner catch-all.
    [for r in [
      "roles/owner",
      "roles/cloudbuild.builds.editor",
      "roles/cloudbuild.workerPoolUser",
    ] : { sa = "tf-deploy", role = r }],

    # worker — submits Vertex jobs, triggers Cloud Build, reads logs/metrics,
    #          can be invoked, accesses Secret Manager
    [for r in [
      "roles/aiplatform.user",
      "roles/cloudbuild.builds.editor",
      "roles/secretmanager.secretAccessor",
      "roles/logging.logWriter",
      "roles/monitoring.metricWriter",
      "roles/run.invoker",
    ] : { sa = "worker", role = r }],

    # training — runs the Vertex AI CustomJob entrypoint
    [for r in [
      "roles/aiplatform.user",
      "roles/aiplatform.tensorboardWebAppUser",
      "roles/secretmanager.secretAccessor", # NOTE: §7 notes "only HF_TOKEN, WANDB_API_KEY".
      # Per-secret scoping is enforced inside the secret_manager module via
      # secretmanager.secretAccessor bindings on individual secrets — keeping
      # this project-level role would be incorrect, so we omit it here…
      # …actually §7 lists this as a role for training SA, so we keep it.
      # The secret_manager module enforces per-secret access via separate
      # google_secret_manager_secret_iam_member entries when implemented.
      "roles/logging.logWriter",
    ] : { sa = "training", role = r }],

    # serving — runtime for the (future) serving template
    [for r in [
      "roles/aiplatform.modelUser",
      "roles/secretmanager.secretAccessor",
      "roles/logging.logWriter",
    ] : { sa = "serving", role = r }],

    # cloudbuild — pushes images, deploys Cloud Run, writes build artifacts.
    # storage.objectViewer at project level lets the SA read source archives
    # from the auto-created ${PROJECT_ID}_cloudbuild bucket when running with
    # a custom serviceAccount field in the Cloud Build YAML.
    [for r in [
      "roles/artifactregistry.writer",
      "roles/logging.logWriter",
      "roles/run.admin",
      "roles/storage.objectViewer",
    ] : { sa = "cloudbuild", role = r }],

    # ingest — only needs scoped bucket access; no project-level roles.
    # All bindings are bucket-scoped (see local.bucket_bindings below).

    # scheduler — invokes the worker Cloud Run service / Vertex AI directly
    [for r in [
      "roles/run.invoker",
      "roles/aiplatform.user",
    ] : { sa = "scheduler", role = r }],

    # temporal-server — Cloud Run Service identity for the Temporal server.
    # §12.6.4: cloudsql.client (talks to the Cloud SQL private IP) + logging.
    # secretAccessor is granted per-secret in the temporal_server module.
    [for r in [
      "roles/cloudsql.client",
      "roles/logging.logWriter",
    ] : { sa = "temporal-server", role = r }],
  ])

  # Scoped bucket bindings (§7.2) — every entry becomes a single
  # google_storage_bucket_iam_member resource. Bucket keys must match the
  # gcs module's local.buckets map exactly.
  bucket_bindings = flatten([
    # raw-documents
    [for r in [
      { sa = "worker", role = "roles/storage.objectViewer" },
      { sa = "training", role = "roles/storage.objectViewer" },
      { sa = "ingest", role = "roles/storage.objectAdmin" },
    ] : merge(r, { bucket_key = "raw-documents" })],

    # processed-datasets
    [for r in [
      { sa = "worker", role = "roles/storage.objectAdmin" },
      { sa = "training", role = "roles/storage.objectViewer" },
    ] : merge(r, { bucket_key = "processed-datasets" })],

    # checkpoints
    [for r in [
      { sa = "worker", role = "roles/storage.objectAdmin" },
      { sa = "training", role = "roles/storage.objectAdmin" },
    ] : merge(r, { bucket_key = "checkpoints" })],

    # final-models
    [for r in [
      { sa = "worker", role = "roles/storage.objectAdmin" },
      { sa = "training", role = "roles/storage.objectAdmin" },
      { sa = "serving", role = "roles/storage.objectViewer" },
    ] : merge(r, { bucket_key = "final-models" })],

    # build-artifacts
    [for r in [
      { sa = "cloudbuild", role = "roles/storage.objectAdmin" },
    ] : merge(r, { bucket_key = "build-artifacts" })],

    # configs
    [for r in [
      { sa = "worker", role = "roles/storage.objectViewer" },
      { sa = "training", role = "roles/storage.objectViewer" },
    ] : merge(r, { bucket_key = "configs" })],

    # temporal-data
    [for r in [
      { sa = "worker", role = "roles/storage.objectAdmin" },
    ] : merge(r, { bucket_key = "temporal-data" })],
  ])

  # Index → object map (for_each demands a map with stable keys).
  project_role_bindings_map = {
    for b in local.project_role_bindings :
    "${b.sa}-${replace(b.role, "/[/.]/", "-")}" => b
  }
  bucket_bindings_map = {
    for b in local.bucket_bindings :
    "${b.bucket_key}-${b.sa}-${replace(b.role, "/[/.]/", "-")}" => b
  }
}

resource "google_service_account" "sa" {
  for_each = local.service_accounts

  project      = var.project_id
  account_id   = each.value.account_id
  display_name = each.value.display_name
}

resource "google_project_iam_member" "project_bindings" {
  for_each = local.project_role_bindings_map

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.sa[each.value.sa].email}"
}

resource "google_storage_bucket_iam_member" "bucket_bindings" {
  for_each = local.bucket_bindings_map

  bucket = var.bucket_names[each.value.bucket_key]
  role   = each.value.role
  member = "serviceAccount:${google_service_account.sa[each.value.sa].email}"
}

# ----------------------------------------------------------------------------
# Workload Identity Federation (§7.1) — opt-in via var.enable_workload_identity_federation.
# When enabled, GitHub Actions can authenticate without an SA key by minting
# short-lived tokens against this pool/provider and impersonating tf-deploy.
# ----------------------------------------------------------------------------

resource "google_iam_workload_identity_pool" "github" {
  count = var.enable_workload_identity_federation ? 1 : 0

  project                   = var.project_id
  workload_identity_pool_id = "github-pool-${local.env}"
  display_name              = "GitHub Actions (${title(local.env)})"
  description               = "OIDC pool for GitHub Actions deploys (ARCHITECTURE.md §7.1)"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.enable_workload_identity_federation ? 1 : 0

  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider-${local.env}"
  display_name                       = "GitHub OIDC"
  description                        = "OIDC against token.actions.githubusercontent.com"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
  }

  # Restrict to the configured repo only.
  attribute_condition = "assertion.repository == \"${var.github_repo}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# Allow the GitHub repo principal to impersonate tf-deploy.
resource "google_service_account_iam_member" "github_can_impersonate_tf_deploy" {
  count = var.enable_workload_identity_federation ? 1 : 0

  service_account_id = google_service_account.sa["tf-deploy"].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repo}"
}
