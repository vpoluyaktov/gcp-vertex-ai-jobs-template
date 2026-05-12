###############################################################################
# Artifact Registry module — 2 Docker repos.
#
#   training-images : built by Cloud Build, consumed by Vertex AI CustomJob
#                     (training SA pushes user-built images; worker pulls
#                     references for job submission).
#   serving-images  : built by Cloud Build, consumed by the future serving
#                     template (Cloud Run / GKE).
#
# Scope: per-repo IAM only. No project-level artifactregistry roles.
###############################################################################

locals {
  common_labels = merge(
    {
      app = var.app_name
      env = var.environment
    },
    var.labels,
  )

  repos = {
    training-images = {
      description = "Container images for Vertex AI training jobs"
    }
    serving-images = {
      description = "Container images for serving (consumed by the future serving template)"
    }
  }
}

resource "google_artifact_registry_repository" "repo" {
  for_each = local.repos

  project       = var.project_id
  location      = var.region
  repository_id = each.key
  format        = "DOCKER"
  description   = each.value.description
  labels        = local.common_labels
}

# ---- IAM ----

# training-images: writer = cloudbuild + training; reader = worker
resource "google_artifact_registry_repository_iam_member" "training_writer_cloudbuild" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.repo["training-images"].name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.cloudbuild_sa_email}"
}

resource "google_artifact_registry_repository_iam_member" "training_writer_training" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.repo["training-images"].name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.training_sa_email}"
}

resource "google_artifact_registry_repository_iam_member" "training_reader_worker" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.repo["training-images"].name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${var.worker_sa_email}"
}

# serving-images: writer = cloudbuild; reader = serving + worker (worker may
# need to read serving images to trigger blue/green or smoke tests).
resource "google_artifact_registry_repository_iam_member" "serving_writer_cloudbuild" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.repo["serving-images"].name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.cloudbuild_sa_email}"
}

resource "google_artifact_registry_repository_iam_member" "serving_reader_serving" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.repo["serving-images"].name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${var.serving_sa_email}"
}

resource "google_artifact_registry_repository_iam_member" "serving_reader_worker" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.repo["serving-images"].name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${var.worker_sa_email}"
}
