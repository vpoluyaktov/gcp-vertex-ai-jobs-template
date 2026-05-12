project_id      = "dfh-stage-id"
region          = "us-central1"
environment     = "stage"
app_name        = "gcp-vertex-ai-jobs-template"
tf_state_bucket = "dfh-stage-tfstate"

# DNS records are managed in the shared ops project.
dns_zone_project = "dfh-ops-id"
dns_zone_name    = "demo-devops-for-hire-com"
custom_domain    = "gcp-vertex-ai-jobs-template.stage.demo.devops-for-hire.com"

# Feature flags (defaults; flip when needed)
temporal_cloud         = false
temporal_namespace     = ""
vpc_enabled            = false
enable_w_and_b         = false
notification_channels  = []
worker_image_tag       = "latest"
enable_firestore_audit = false
