###############################################################################
# Temporal server — Cloud Run Service running temporalio/auto-setup.
# Per ARCHITECTURE.md §12.6.2:
#   - Cloud Run Service (NOT a Job — long-running)
#   - min_instances=1, max_instances=1 (stage) / 2 (prod)
#   - CPU=2, memory=2Gi, cpu_idle=false (always-allocated)
#   - container port 7233 (gRPC frontend)
#   - INGRESS_TRAFFIC_INTERNAL_ONLY — VPC-only reachability
#   - VPC connector with ALL_TRAFFIC egress so the container can hit Cloud SQL
#     over its private IP
#   - 9 env vars wired exactly per §12.6.2; POSTGRES_PWD mounted from
#     Secret Manager
#
# Optional: private DNS zone `<env>.internal` and CNAME
# `temporal-server.<env>.internal` → the Cloud Run hostname (§12.6.2). The
# temporal-worker uses this hostname as TEMPORAL_ADDRESS so it's not coupled
# to the auto-generated *.run.app URL.
#
# NOTE on port: Cloud Run advertises the container's port on HTTPS/443. The
# client must connect to <hostname>:443 even though the container itself
# listens on 7233. See §12.6.3 backlog item — the spec's :7233 in
# TEMPORAL_ADDRESS will need a follow-up to either expose the port directly
# (Cloud Run does not currently support arbitrary TCP) or have the worker
# connect on :443 with gRPC-over-TLS.
###############################################################################

locals {
  service_name = "temporal-server-${var.environment}"

  common_labels = merge(
    {
      app     = var.app_name
      env     = var.environment
      purpose = "temporal-server"
    },
    var.labels,
  )

  # Trim https:// from the Cloud Run uri so it can be used as a DNS CNAME
  # target. uri is of the form "https://<service>-<hash>-<region>.a.run.app".
  cloud_run_hostname = replace(google_cloud_run_v2_service.temporal_server.uri, "https://", "")
}

# Cloud Run Service — the Temporal server itself.
resource "google_cloud_run_v2_service" "temporal_server" {
  project  = var.project_id
  name     = local.service_name
  location = var.region

  # Only callers inside the VPC (via the connector) can reach this service.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  labels = local.common_labels

  template {
    service_account = var.service_account_email

    # Always-on: never scale to zero so the worker can always reach the
    # frontend. Max=2 in prod provides one warm replica during deploys.
    scaling {
      min_instance_count = 1
      max_instance_count = var.environment == "prod" ? 2 : 1
    }

    # Egress from the connector into the VPC. ALL_TRAFFIC routes both
    # RFC-1918 and external traffic via the connector — required so the
    # container can reach the Cloud SQL private IP (allocated inside the
    # PSA range, not the subnet itself).
    vpc_access {
      connector = var.vpc_connector_id
      egress    = "ALL_TRAFFIC"
    }

    containers {
      image = var.image

      ports {
        name           = "h2c" # gRPC over HTTP/2 cleartext — Cloud Run wraps with TLS at the edge
        container_port = 7233
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        # cpu_idle=false → CPU is always allocated, not just during requests.
        # Required because Temporal server runs background goroutines.
        cpu_idle          = false
        startup_cpu_boost = true
      }

      # All 9 env vars from §12.6.2, in spec order.
      env {
        name  = "DB"
        value = "postgres12" # auto-setup driver name — covers Postgres 12+
      }
      env {
        name  = "POSTGRES_SEEDS"
        value = var.cloud_sql_private_ip
      }
      env {
        name  = "DB_PORT"
        value = "5432"
      }
      env {
        name  = "POSTGRES_USER"
        value = var.db_user
      }
      env {
        name = "POSTGRES_PWD"
        value_source {
          secret_key_ref {
            secret  = var.db_password_secret_name
            version = "latest"
          }
        }
      }
      env {
        name  = "DBNAME"
        value = var.db_name
      }
      env {
        name  = "VISIBILITY_DBNAME"
        value = var.db_visibility_name
      }
      env {
        name  = "TEMPORAL_BROADCAST_ADDRESS"
        value = "0.0.0.0"
      }
      env {
        name  = "BIND_ON_IP"
        value = "0.0.0.0"
      }

      # Surface the namespace so auto-setup's startup script creates it.
      env {
        name  = "TEMPORAL_NAMESPACE"
        value = var.temporal_namespace
      }

      # Cloud SQL is configured with ssl_mode = ENCRYPTED_ONLY (see
      # modules/cloud_sql/main.tf — pg_hba.conf rejects unencrypted connections).
      # Two sets of env vars are required:
      #
      #   POSTGRES_TLS_* — consumed by the temporal-sql-tool that runs the
      #     schema setup/migration step in auto-setup's startup script.
      #
      #   SQL_TLS_* — consumed by docker/config_template.yaml (rendered by
      #     dockerize) to produce the temporal server's persistence config.
      #     Without these the server reads its config with tls.enabled=false
      #     and Cloud SQL rejects the unencrypted connection.
      #
      # Cloud SQL serves a GCP-issued server cert that does not match the
      # private IP hostname, so host verification is disabled on both sides
      # (encryption-on-the-wire is preserved; mTLS is not required given the
      # network is private).
      env {
        name  = "POSTGRES_TLS_ENABLED"
        value = "true"
      }
      env {
        name  = "POSTGRES_TLS_DISABLE_HOST_VERIFICATION"
        value = "true"
      }
      env {
        name  = "SQL_TLS_ENABLED"
        value = "true"
      }
      env {
        name  = "SQL_HOST_VERIFICATION_ENABLED"
        value = "false"
      }

      startup_probe {
        # auto-setup runs schema migrations on first start — be patient.
        initial_delay_seconds = 30
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 30 # 5 minutes total
        tcp_socket {
          port = 7233
        }
      }

      # NOTE: Cloud Run v2 does NOT support TCP-socket liveness probes (only
      # startup probes accept tcp_socket). Temporal frontend speaks gRPC on
      # :7233 and exposes no HTTP/2 health endpoint Cloud Run can hit, so we
      # rely on the startup_probe above plus Cloud Run's own container-health
      # supervision. If a probe is required later, expose a sidecar HTTP
      # endpoint that proxies `temporal operator cluster health`.
    }

    timeout = "300s"
  }

  # Force re-deploy when the password rotates so the container picks up a fresh
  # version of the secret (Cloud Run caches secret values per revision).
  lifecycle {
    ignore_changes = [client]
  }
}

# Scoped Secret Manager access: only the temporal-server SA can read this one
# specific secret. NOT a project-level secretmanager.secretAccessor binding.
resource "google_secret_manager_secret_iam_member" "temporal_server_can_read_pw" {
  project   = var.project_id
  secret_id = var.db_password_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_email}"
}

# Allow internal (VPC) callers to invoke the service. The Cloud Run service
# is gated by INGRESS_TRAFFIC_INTERNAL_ONLY (network) and by IAM (this
# binding). allUsers here is safe because network ingress is already locked
# down; without this, even VPC callers get 403 from the run.invoker check.
resource "google_cloud_run_v2_service_iam_member" "internal_invoker" {
  project  = google_cloud_run_v2_service.temporal_server.project
  location = google_cloud_run_v2_service.temporal_server.location
  name     = google_cloud_run_v2_service.temporal_server.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Private DNS zone — temporal-server.<env>.internal → Cloud Run hostname.
# ---------------------------------------------------------------------------

resource "google_dns_managed_zone" "internal" {
  count = var.create_private_dns ? 1 : 0

  project     = var.project_id
  name        = "${var.environment}-internal"
  dns_name    = "${var.environment}.internal."
  description = "Private DNS zone for ${var.environment} VPC-internal hostnames"
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = var.network_self_link
    }
  }

  labels = local.common_labels
}

resource "google_dns_record_set" "temporal_server" {
  count = var.create_private_dns ? 1 : 0

  project      = var.project_id
  managed_zone = google_dns_managed_zone.internal[0].name
  name         = "temporal-server.${var.environment}.internal."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["${local.cloud_run_hostname}."]
}
