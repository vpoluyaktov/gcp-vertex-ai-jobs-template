###############################################################################
# Networking module — VPC + subnet + Serverless VPC Access connector + Private
# Service Access peering for Cloud SQL. Mandatory per ARCHITECTURE.md §12.1.
#
# Apply order: this module has no upstream dependencies. cloud_sql, temporal_server,
# and cloud_run_worker all depend on it via vpc_self_link / connector_self_link
# / private_service_connection.
###############################################################################

locals {
  network_name   = "${var.app_name}-${var.environment}"
  subnet_name    = "${var.app_name}-${var.environment}-subnet"
  connector_name = "vpcconn-${var.environment}" # ≤ 25 chars, GCP limit
  psa_range_name = "psa-${var.app_name}-${var.environment}"
}

# Custom-mode VPC — no auto-created subnets. We provision exactly one regional
# subnet plus the connector's own /28 subnet.
resource "google_compute_network" "vpc" {
  project                         = var.project_id
  name                            = local.network_name
  auto_create_subnetworks         = false
  routing_mode                    = "REGIONAL"
  delete_default_routes_on_create = false
}

resource "google_compute_subnetwork" "subnet" {
  project                  = var.project_id
  name                     = local.subnet_name
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = var.vpc_cidr
  private_ip_google_access = true # Cloud Run worker → google APIs without NAT
}

# Serverless VPC Access Connector — Cloud Run Service/Job egress into the VPC.
# Used by both temporal-server (to reach Cloud SQL private IP) and the worker
# Cloud Run Job (to reach temporal-server's internal endpoint).
resource "google_vpc_access_connector" "connector" {
  project       = var.project_id
  name          = local.connector_name
  region        = var.region
  network       = google_compute_network.vpc.name
  ip_cidr_range = var.vpc_connector_cidr

  # f1-micro keeps idle cost low; the connector autoscales up under load.
  machine_type   = "e2-micro"
  min_throughput = 200
  max_throughput = 300
}

# Private Service Access — reserves an IP range and creates the VPC peering
# that Cloud SQL needs to expose a private IP.
resource "google_compute_global_address" "psa_range" {
  project       = var.project_id
  name          = local.psa_range_name
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.psa_range_prefix_length
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa_range.name]
}

# Allow internal traffic within the VPC and from the serverless connector subnet.
# Cloud Run via connector appears with source IPs in vpc_connector_cidr.
resource "google_compute_firewall" "allow_internal" {
  project = var.project_id
  name    = "${local.network_name}-allow-internal"
  network = google_compute_network.vpc.name

  direction = "INGRESS"
  priority  = 1000

  source_ranges = [
    var.vpc_cidr,
    var.vpc_connector_cidr,
  ]

  allow {
    protocol = "tcp"
  }
  allow {
    protocol = "udp"
  }
  allow {
    protocol = "icmp"
  }
}

# Explicit deny-all for ingress from the public internet (defence in depth —
# the implicit default is the same, but stating it makes audits straightforward).
resource "google_compute_firewall" "deny_external" {
  project = var.project_id
  name    = "${local.network_name}-deny-external"
  network = google_compute_network.vpc.name

  direction          = "INGRESS"
  priority           = 65534
  source_ranges      = ["0.0.0.0/0"]
  destination_ranges = []

  deny {
    protocol = "all"
  }
}
