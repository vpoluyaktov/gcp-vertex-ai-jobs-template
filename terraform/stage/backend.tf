terraform {
  backend "gcs" {
    bucket = "dfh-stage-tfstate"
    prefix = "gcp-vertex-ai-jobs-template/state"
  }
}
