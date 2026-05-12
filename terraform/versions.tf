terraform {
  # >= 1.6 required: ARCHITECTURE.md §9.5 — the template uses variables in
  # import {} blocks, which is unsupported on 1.5.x. Pin matches template-standards.md.
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
