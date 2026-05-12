###############################################################################
# Cloud Build module — Private Worker Pool.
#
# Why: standard Cloud Build workers run in a Google-managed project outside
# the customer VPC, so they cannot reach services with INTERNAL ingress (e.g.
# `temporal-server.<env>.internal:443`, the Cloud SQL private IP). A Private
# Worker Pool peers into the customer VPC, granting access to private-IP
# endpoints via the same PSA peering used by Cloud SQL.
#
# Consumed by:
#   - cloudbuild/cloudbuild-trigger-workflow.yaml (so it can reach the
#     INTERNAL Temporal server to submit smoke workflows post-deploy)
#   - Future builds that need to read from the temporal-data bucket via
#     private google access, or talk to the Postgres private IP for
#     migrations
#
# Apply order: depends on networking's PSA peering being in place.
###############################################################################

locals {
  pool_name = "${var.app_name}-${var.environment}"

  common_labels = merge(
    {
      app     = var.app_name
      env     = var.environment
      purpose = "cloud-build-private-pool"
    },
    var.labels,
  )
}

resource "google_cloudbuild_worker_pool" "private" {
  project  = var.project_id
  name     = local.pool_name
  location = var.region

  worker_config {
    machine_type = var.machine_type
    disk_size_gb = var.disk_size_gb
    # no_external_ip = true forces all worker egress through the peered VPC.
    # This is what makes private-IP services reachable from within builds.
    no_external_ip = true
  }

  network_config {
    # peered_network requires the form `projects/<project>/global/networks/<name>` —
    # the API rejects the full https:// self_link. Strip the GCE API prefix from
    # the self_link to produce that form.
    peered_network = replace(var.network_self_link, "https://www.googleapis.com/compute/v1/", "")
    # An optional /29 from the VPC's reserved PSA range — letting GCP pick
    # keeps the module simple and avoids running out of space in the pool's
    # peering range as the env grows.
    peered_network_ip_range = ""
  }

  # The pool's peering is layered on top of the PSA connection. Without this
  # depends_on, `terraform apply` can race the connection creation and fail
  # with "service networking connection does not exist".
  depends_on = [var.psa_dependency]
}

# NOTE on IAM: Cloud Build worker pools don't have a per-pool IAM resource in
# the google provider (5.x). roles/cloudbuild.workerPoolUser must be granted
# at project level. tf-deploy-<env> already holds roles/owner per §7 which
# subsumes it; no additional binding is needed here. If you later restrict
# tf-deploy below owner, add roles/cloudbuild.workerPoolUser to the iam
# module's project_role_bindings for tf-deploy.
