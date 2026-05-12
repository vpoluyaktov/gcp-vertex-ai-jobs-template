terraform {
  backend "gcs" {
    bucket = "dfh-prod-tfstate"
    prefix = "gcp-vertex-ai-jobs-template/state"
  }
}
