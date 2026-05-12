###############################################################################
# GCS buckets module — 7 buckets per ARCHITECTURE.md §6.
#
# Naming pattern (§6):  <project_id>-<purpose>
# Common settings:      uniform bucket-level access, public_access_prevention
#                       enforced, force_destroy false.
# Lifecycle/versioning: per-bucket, exactly as the §6 table specifies.
###############################################################################

locals {
  common_labels = merge(
    {
      app = var.app_name
      env = var.environment
    },
    var.labels,
  )

  # Per-bucket spec table mirroring ARCHITECTURE.md §6.
  # `lifecycle_rules` is the list of {age_days, action, condition_prefix?} tuples
  # this module turns into google_storage_bucket.lifecycle_rule blocks.
  buckets = {
    raw-documents = {
      purpose    = "raw-documents"
      versioning = true
      lifecycle = [
        # §6: Delete after 90d only in `_quarantine/` prefix.
        { action = "Delete", age_days = 90, matches_prefix = ["_quarantine/"] },
      ]
    }
    processed-datasets = {
      purpose    = "processed-datasets"
      versioning = true
      lifecycle = [
        # §6: Delete after 30d; transition to Nearline after 7d.
        { action = "Delete", age_days = 30, matches_prefix = [] },
        { action = "SetStorageClass", storage_class = "NEARLINE", age_days = 7, matches_prefix = [] },
      ]
    }
    checkpoints = {
      purpose    = "checkpoints"
      versioning = false
      lifecycle = [
        # §6: Delete after 14d (transient).
        { action = "Delete", age_days = 14, matches_prefix = [] },
      ]
    }
    final-models = {
      purpose    = "final-models"
      versioning = true
      lifecycle = [
        # §6: Delete after 365d; Nearline after 30d.
        { action = "Delete", age_days = 365, matches_prefix = [] },
        { action = "SetStorageClass", storage_class = "NEARLINE", age_days = 30, matches_prefix = [] },
      ]
    }
    build-artifacts = {
      purpose    = "build-artifacts"
      versioning = false
      lifecycle = [
        # §6: Delete after 90d.
        { action = "Delete", age_days = 90, matches_prefix = [] },
      ]
    }
    configs = {
      purpose    = "configs"
      versioning = true
      lifecycle  = [] # §6: none
    }
    temporal-data = {
      purpose    = "temporal-data"
      versioning = false
      lifecycle = [
        # §6: Delete after 30d.
        { action = "Delete", age_days = 30, matches_prefix = [] },
      ]
    }
  }
}

resource "google_storage_bucket" "buckets" {
  for_each = local.buckets

  project  = var.project_id
  name     = "${var.project_id}-${each.value.purpose}"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = each.value.versioning
  }

  dynamic "lifecycle_rule" {
    for_each = each.value.lifecycle
    content {
      action {
        type          = lifecycle_rule.value.action
        storage_class = lookup(lifecycle_rule.value, "storage_class", null)
      }
      condition {
        age            = lifecycle_rule.value.age_days
        matches_prefix = lifecycle_rule.value.matches_prefix
      }
    }
  }

  labels = merge(
    local.common_labels,
    {
      purpose = each.value.purpose
    },
  )
}
