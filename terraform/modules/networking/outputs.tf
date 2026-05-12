output "network_name" {
  description = "VPC network name"
  value       = google_compute_network.vpc.name
}

output "network_self_link" {
  description = "VPC network self_link (consumed by Cloud SQL private IP, firewall rules)"
  value       = google_compute_network.vpc.self_link
}

output "network_id" {
  description = "VPC network id"
  value       = google_compute_network.vpc.id
}

output "subnet_name" {
  description = "Subnet name"
  value       = google_compute_subnetwork.subnet.name
}

output "subnet_self_link" {
  description = "Subnet self_link"
  value       = google_compute_subnetwork.subnet.self_link
}

output "connector_name" {
  description = "Serverless VPC Access Connector name"
  value       = google_vpc_access_connector.connector.name
}

output "connector_self_link" {
  description = "Serverless VPC Access Connector self_link (consumed by Cloud Run vpc_access)"
  value       = google_vpc_access_connector.connector.self_link
}

output "psa_range_name" {
  description = "Name of the Private Service Access reserved IP range"
  value       = google_compute_global_address.psa_range.name
}

output "private_service_connection" {
  description = "Reference to the service-networking connection; depend on this for Cloud SQL private-IP provisioning"
  value       = google_service_networking_connection.psa.id
}
