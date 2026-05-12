# TODO: implement Cloud SQL for PostgreSQL module (Temporal persistence).
#
# Per ARCHITECTURE.md §12.6.1:
#   - database_version: POSTGRES_15
#   - tier:             var.cloud_sql_tier (recommended db-custom-2-7680)
#   - disk_size:        var.cloud_sql_disk_gb GB SSD, autoresize on
#   - private IP only — no public IP, accessible only via the VPC peering
#     created by the networking module (Private Service Access).
#   - Databases: `temporal`, `temporal_visibility`.
#   - User: `temporal` with password from random_password → Secret Manager
#     `temporal-postgres-password`.
#
# Required inputs (TODO: declare in variables.tf):
#   project_id, app_name, environment, region, tier, disk_gb, private_network
#
# Required outputs (TODO: declare in outputs.tf):
#   instance_connection_name, private_ip_address, database_user
