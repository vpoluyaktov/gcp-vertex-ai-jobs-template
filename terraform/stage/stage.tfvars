project_id      = "dfh-stage-id"
region          = "us-central1"
environment     = "stage"
app_name        = "gcp-vertex-ai-jobs-template"
tf_state_bucket = "dfh-stage-tfstate"

# DNS records are managed in the shared ops project.
dns_zone_project = "dfh-ops-id"
dns_zone_name    = "demo-devops-for-hire-com"
custom_domain    = "gcp-vertex-ai-jobs-template.stage.demo.devops-for-hire.com"

# Self-hosted Temporal (Cloud Run + Cloud SQL). See ARCHITECTURE.md §12.6.
temporal_namespace = "default"
# TODO(architect): replace the tag with a digest-pinned reference, e.g.
# temporalio/auto-setup@sha256:<digest>. Tag-only references are not reproducible.
temporal_server_image = "temporalio/auto-setup:1.25"
cloud_sql_tier        = "db-custom-2-7680" # 2 vCPU, 7.5 GiB
cloud_sql_disk_gb     = 20

# Networking — required. Stage subnet uses 10.20.0.0/24, connector /28 in .1.0/28.
vpc_cidr           = "10.20.0.0/24"
vpc_connector_cidr = "10.20.1.0/28"

# Feature flags
enable_w_and_b         = false
notification_channels  = []
worker_image_tag       = "latest"
enable_firestore_audit = false
