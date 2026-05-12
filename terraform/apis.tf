###############################################################################
# Project-level API enablement. Centralised here (not per-module) so a fresh
# project bootstrap only needs one apply to come up.
#
# Disabled-on-destroy is FALSE — destroying this template should not knock out
# APIs the project may still need (e.g. shared firestore, dns).
###############################################################################

resource "google_project_service" "apis" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "serviceusage.googleapis.com",

    # Storage / AR
    "storage.googleapis.com",
    "artifactregistry.googleapis.com",

    # Networking (VPC, connector, PSA)
    "compute.googleapis.com",
    "vpcaccess.googleapis.com",
    "servicenetworking.googleapis.com",
    "dns.googleapis.com",

    # Compute services consumed by future modules
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudbuild.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "firestore.googleapis.com",
  ])

  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}
