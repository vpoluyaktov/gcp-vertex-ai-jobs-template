output "service_name" {
  description = "Cloud Run service name"
  value       = google_cloud_run_v2_service.temporal_server.name
}

output "service_uri" {
  description = "Cloud Run-issued URI (https://...run.app). Reachable only from within the VPC because ingress is INTERNAL."
  value       = google_cloud_run_v2_service.temporal_server.uri
}

output "service_hostname" {
  description = "Bare hostname of the Cloud Run service (https:// stripped) — used as the CNAME target for the private DNS record"
  value       = local.cloud_run_hostname
}

output "internal_hostname" {
  description = "Stable internal hostname (temporal-server.<env>.internal). Use this as TEMPORAL_ADDRESS host. Empty string when create_private_dns=false."
  value       = var.create_private_dns ? trimsuffix(google_dns_record_set.temporal_server[0].name, ".") : ""
}

output "grpc_port" {
  description = "Container's gRPC port (7233). Note: Cloud Run terminates TLS and exposes the service on 443 externally — see the §12.6.3 note in main.tf."
  value       = 7233
}
