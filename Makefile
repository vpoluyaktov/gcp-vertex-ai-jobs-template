.PHONY: help setup \
        tf-init-stage tf-plan-stage tf-apply-stage \
        tf-init-prod tf-plan-prod tf-apply-prod \
        build-training-image build-serving-image \
        trigger-training check-status \
        lint test clean

# Default GCP project (override on the command line: make tf-plan-stage PROJECT_ID=...)
PROJECT_ID_STAGE ?= dfh-stage-id
PROJECT_ID_PROD  ?= dfh-prod-id
REGION           ?= us-central1
APP_NAME         ?= vertex-ai-jobs-template

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2}'

## -------- Setup --------
setup: ## Install Python deps and initialize Terraform for both envs
	python -m pip install --upgrade pip
	pip install -r temporal/worker/requirements.txt
	pip install -r temporal/client/requirements.txt
	pip install -r training/requirements.txt
	pip install -r data_prep/requirements.txt
	pip install -r serving/requirements.txt
	$(MAKE) tf-init-stage
	$(MAKE) tf-init-prod

## -------- Terraform: staging --------
tf-init-stage: ## terraform init for stage
	cd terraform/stage && terraform init

tf-plan-stage: ## terraform plan for stage
	cd terraform/stage && terraform plan

tf-apply-stage: ## terraform apply for stage
	cd terraform/stage && terraform apply

## -------- Terraform: production --------
tf-init-prod: ## terraform init for prod
	cd terraform/prod && terraform init

tf-plan-prod: ## terraform plan for prod
	cd terraform/prod && terraform plan

tf-apply-prod: ## terraform apply for prod
	cd terraform/prod && terraform apply

## -------- Container builds (via Cloud Build) --------
build-training-image: ## Build training image via Cloud Build
	gcloud builds submit --config=cloudbuild/cloudbuild-training.yaml --project=$(PROJECT_ID_STAGE) .

build-serving-image: ## Build serving image via Cloud Build
	gcloud builds submit --config=cloudbuild/cloudbuild-serving.yaml --project=$(PROJECT_ID_STAGE) .

## -------- Workflow triggers --------
trigger-training: ## Trigger Temporal fine-tuning workflow
	./scripts/trigger_training.sh

check-status: ## Check Temporal workflow and Vertex AI job status
	./scripts/check_job_status.sh

## -------- Quality --------
lint: ## Lint Python sources
	ruff check . || flake8 .

test: ## Run pytest
	pytest -q

## -------- Cleanup --------
clean: ## Remove temp files and caches
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf build dist .coverage htmlcov wandb
