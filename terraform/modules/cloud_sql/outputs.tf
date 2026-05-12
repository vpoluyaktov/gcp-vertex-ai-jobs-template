output "instance_name" {
  description = "Cloud SQL instance name"
  value       = google_sql_database_instance.temporal.name
}

output "instance_connection_name" {
  description = "Instance connection name in the form project:region:instance — used by Cloud SQL Auth Proxy (optional sidecar)"
  value       = google_sql_database_instance.temporal.connection_name
}

output "private_ip_address" {
  description = "Private IP of the SQL instance. Mounted into the temporal-server container as POSTGRES_SEEDS."
  value       = google_sql_database_instance.temporal.private_ip_address
}

output "database_user" {
  description = "Postgres user name used by the temporal-server container"
  value       = google_sql_user.temporal.name
}

output "temporal_database_name" {
  description = "Application database name (auto-setup DBNAME)"
  value       = google_sql_database.temporal.name
}

output "temporal_visibility_database_name" {
  description = "Visibility database name (auto-setup VISIBILITY_DBNAME)"
  value       = google_sql_database.temporal_visibility.name
}

output "password_secret_id" {
  description = "Full Secret Manager resource id for temporal-postgres-password (e.g. projects/123/secrets/temporal-postgres-password)"
  value       = google_secret_manager_secret.postgres_password.id
}

output "password_secret_name" {
  description = "Short secret name (temporal-postgres-password) — convenient for secret_ref env mounts on Cloud Run"
  value       = google_secret_manager_secret.postgres_password.secret_id
}

output "password_secret_version" {
  description = "Latest version resource id of the postgres password secret"
  value       = google_secret_manager_secret_version.postgres_password.id
}
