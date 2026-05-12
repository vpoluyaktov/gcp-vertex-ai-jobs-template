# TODO: implement self-hosted Temporal server module.
#
# Per ARCHITECTURE.md §12.6.2:
#   - Resource: google_cloud_run_v2_service (NOT a Job — long-running).
#   - Name: `temporal-server-<environment>`.
#   - Image: var.image (digest-pinned temporalio/auto-setup:1.25 family).
#   - Service account: `temporal-server-<env>@…` with roles/cloudsql.client and
#     roles/secretmanager.secretAccessor scoped to temporal-postgres-password.
#   - min_instances = 1, max_instances = 1 (single-instance Temporal server).
#   - VPC connector: var.vpc_connector (egress through Serverless VPC Access).
#   - Container env vars (from §12.6.2):
#       DB=postgres12
#       POSTGRES_SEEDS=<cloud-sql-private-ip>
#       DB_PORT=5432
#       POSTGRES_USER=temporal
#       POSTGRES_PWD=<secret-ref:temporal-postgres-password>
#       DBNAME=temporal
#       VISIBILITY_DBNAME=temporal_visibility
#       TEMPORAL_BROADCAST_ADDRESS=0.0.0.0
#       BIND_ON_IP=0.0.0.0
#   - Ports: 7233 (frontend gRPC).
#   - Ingress: INGRESS_TRAFFIC_INTERNAL_ONLY — reachable only from within the VPC.
#
# Required inputs (TODO: declare in variables.tf):
#   project_id, app_name, environment, region, image, temporal_namespace,
#   cloud_sql_instance, cloud_sql_private_ip, db_password_secret, vpc_connector
#
# Required outputs (TODO: declare in outputs.tf):
#   service_url, internal_grpc_host (used by cloud_run_worker)
