# This file is intentionally a placeholder.
#
# Each environment (terraform/stage, terraform/prod) is its OWN Terraform
# root module per ARCHITECTURE.md §12 — they do not share a root config.
# Variables are declared inside each env's variables.tf and supplied via
# the env's terraform.tfvars.
#
# Shared module inputs live in terraform/modules/<name>/variables.tf once
# the module bodies are implemented.
