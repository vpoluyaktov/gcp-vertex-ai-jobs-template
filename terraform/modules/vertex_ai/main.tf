###############################################################################
# Vertex AI module — TensorBoard instance + ML Metadata store.
#
# Per ARCHITECTURE.md §15.0:
#   - This template uses Vertex AI TensorBoard as the SINGLE source of truth
#     for training metrics. One TensorBoard instance per environment.
#   - The CustomJob's `job_spec.tensorboard` field (set by the worker at
#     submission time) auto-uploads training logs to this instance.
#   - The Terraform output `tensorboard_resource_name` is read by train.py via
#     aiplatform.Tensorboard(TENSORBOARD_RESOURCE_NAME).
#
# Model Registry note (§3.4 step 6 + §19.9):
#   - Models are uploaded at training time via the aiplatform Python SDK.
#   - There is NO terraform resource for "an empty Model Registry" — the
#     registry is a property of the project, surfaced once aiplatform.googleapis.com
#     is enabled (handled in terraform/apis.tf). Aliases (`default`, `staging`,
#     `production`) are moved at runtime by scripts/promote_model.py.
#   - The optional google_vertex_ai_metadata_store below backs the Experiments
#     surface that the Model Registry uses for lineage.
#
# IAM note:
#   - aiplatform.user / aiplatform.tensorboardWebAppUser / aiplatform.modelUser
#     are already granted at the project level in the iam module (§7). This
#     module therefore does NOT add IAM bindings — running `aiplatform.user`
#     on this TensorBoard instance is sufficient.
###############################################################################

locals {
  common_labels = merge(
    {
      app     = var.app_name
      env     = var.environment
      purpose = "training-metrics"
    },
    var.labels,
  )

  tensorboard_display_name = "${var.app_name}-${var.environment}"
  metadata_store_id        = "${var.app_name}-${var.environment}"
}

# TensorBoard instance — one per environment. CustomJobs reference this by
# resource name (projects/<num>/locations/<region>/tensorboards/<id>).
resource "google_vertex_ai_tensorboard" "main" {
  project      = var.project_id
  region       = var.region
  display_name = local.tensorboard_display_name
  description  = "Vertex AI TensorBoard for ${var.app_name} (${var.environment}). Single source of truth for training metrics per ARCHITECTURE.md §15.0."

  labels = local.common_labels

  # Optional customer-managed encryption. Empty string falls back to Google-
  # managed encryption (the default).
  dynamic "encryption_spec" {
    for_each = var.tensorboard_kms_key != "" ? [1] : []
    content {
      kms_key_name = var.tensorboard_kms_key
    }
  }
}

# ML Metadata store note: the google provider (5.x) does NOT expose a
# `google_vertex_ai_metadata_store` resource. The project's `default` store
# is auto-created the first time aiplatform.init() runs against it, so no
# Terraform action is required for Experiments / Model Registry lineage to
# work. `var.create_metadata_store` is accepted for forward-compatibility
# but is currently a no-op (kept so downstream callers don't have to change
# when/if the provider adds support).
