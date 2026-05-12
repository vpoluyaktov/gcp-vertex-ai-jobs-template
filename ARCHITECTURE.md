# ARCHITECTURE.md — `gcp-vertex-ai-jobs-template`

**Production-grade LLM Fine-tuning Template on Google Cloud Platform**
**Architect of record:** Principal Architect, DevOps-for-Hire
**Version:** 1.0.0
**Last updated:** 2026-05-12

---

## 1. Overview

### 1.1 Purpose

`gcp-vertex-ai-jobs-template` is a **reusable, opinionated template** for automated supervised fine-tuning (SFT) of open-source large language models (Llama 3.1, Mistral, Qwen, Phi, etc.) on domain-specific data such as investment documents, contracts, invoices, and financial statements. It is designed to be **forked once per domain/customer**, parameterized via Terraform variables and Hydra/YAML configs, and operated unattended end-to-end:

- Drop raw documents (PDF, DOCX, HTML, JSON) into a GCS bucket.
- A Temporal workflow validates the data, builds a training image (if needed), submits a Vertex AI `CustomJob`, polls until done, registers the resulting model, optionally pushes the LoRA adapter to Hugging Face Hub, and prepares a vLLM serving container.
- Spot VMs are used by default; the workflow transparently falls back to On-Demand if the Spot capacity request is rejected or the job is preempted more than `N` times.

### 1.2 Design goals

| Goal | Realised by |
|------|-------------|
| **Durable & retryable** | Temporal workflows with heartbeats, configurable retry policies, idempotent activities |
| **Cost-optimised** | Spot VMs with fallback, LoRA/QLoRA defaults, autoscale-to-zero Cloud Run worker, lifecycle rules on GCS |
| **Reproducible** | All hyperparameters in version-controlled YAML; container images pinned by digest |
| **Multi-model** | Single `train.py` driven by `model_id` config; tested against Llama 3.1 8B, Mistral 7B, Qwen 2.5 7B/14B |
| **Production observability** | Cloud Logging, Vertex AI TensorBoard, optional W&B; Cloud Monitoring alerts on job failure, cost overrun, preemption rate |
| **Secure by default** | Workload Identity, no SA keys, Secret Manager for tokens, private Artifact Registry, optional VPC-SC |
| **Multi-environment** | `terraform/stage/` and `terraform/prod/` with shared modules; per-env GCS state |

### 1.3 Technology stack

| Layer | Technology | Version pin |
|-------|------------|------------|
| **Orchestration** | Temporal (Cloud or self-hosted) | Python SDK `temporalio>=1.7,<2` |
| **Worker runtime** | Cloud Run Job (or Cloud Run Service for long-poll worker) | n/a |
| **Training** | Vertex AI `CustomJob` API | `google-cloud-aiplatform>=1.71,<2` |
| **Training framework** | HF `transformers`, `peft`, `trl`, `accelerate`, `bitsandbytes` | `transformers>=4.45,<4.55` |
| **Config** | Hydra + OmegaConf + YAML | `hydra-core>=1.3,<1.4` |
| **Serving** | vLLM | `vllm>=0.6.3,<0.7` |
| **IaC** | Terraform | **>= 1.6** |
| **CI/CD** | GitHub Actions + Cloud Build | actions-runner: ubuntu-24.04 |
| **Language (worker, train, data_prep)** | Python | 3.11 |
| **Container base (training)** | `nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04` | digest-pinned |
| **Container base (serving)** | `vllm/vllm-openai:v0.6.3` | digest-pinned |
| **Storage** | GCS, Vertex AI Model Registry, optional HF Hub | n/a |
| **Secrets** | Google Secret Manager | n/a |

### 1.4 Non-goals

This template does **not** cover:
- RLHF / DPO / PPO post-training (only SFT and LoRA/QLoRA SFT).
- Multi-tenant model serving at scale (vLLM is a starting point; production serving belongs in a separate template).
- Pretraining from scratch.
- Browser-based labelling UI (data is assumed to be programmatically convertible to instruction format).

---

## 2. Directory and File Structure

```
gcp-vertex-ai-jobs-template/
├── ARCHITECTURE.md                       # This document (authoritative spec)
├── README.md                             # Getting-started guide with Mermaid diagrams
├── LICENSE                               # Apache 2.0
├── Makefile                              # Developer shortcuts (see §13)
├── .env.example                          # Template for local env vars
├── .gitignore
├── .dockerignore
├── pyproject.toml                        # Top-level project metadata; ruff/black config
├── .pre-commit-config.yaml               # ruff, black, terraform fmt, hadolint
│
├── terraform/                            # All infrastructure-as-code
│   ├── modules/                          # Reusable, environment-agnostic modules
│   │   ├── gcs_buckets/                  # 5 buckets + lifecycle rules + IAM
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   ├── iam/                          # Service accounts + role bindings
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   ├── artifact_registry/            # Training + serving repos
│   │   ├── cloud_run_worker/             # Temporal worker as Cloud Run Job/Service
│   │   ├── secret_manager/               # HF_TOKEN, WANDB_API_KEY, TEMPORAL_API_KEY
│   │   ├── vertex_ai/                    # TensorBoard instance, Model Registry config
│   │   ├── cloud_build/                  # Triggers + SA + workerpool (optional)
│   │   ├── scheduler/                    # Cloud Scheduler jobs + Eventarc triggers
│   │   ├── networking/                   # VPC, private service access, NAT (optional)
│   │   └── monitoring/                   # Alert policies, notification channels, dashboards
│   ├── stage/                            # Staging environment
│   │   ├── backend.tf                    # gs://dfh-stage-tfstate
│   │   ├── main.tf                       # Wires all modules with stage values
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── terraform.tfvars              # stage-specific values (NOT secrets)
│   ├── prod/                             # Production environment
│   │   ├── backend.tf                    # gs://dfh-prod-tfstate
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── terraform.tfvars              # prod-specific values
│   └── README.md                         # IaC-specific README (apply order, gotchas)
│
├── temporal/                             # Temporal workflows, activities, worker entrypoint
│   ├── __init__.py
│   ├── worker.py                         # Worker entrypoint (run by Cloud Run Job)
│   ├── client.py                         # CLI to submit / signal / query workflows
│   ├── workflows/
│   │   ├── __init__.py
│   │   ├── finetune_workflow.py          # The 9-step SFT workflow
│   │   └── shared.py                     # Workflow-level dataclasses & exceptions
│   ├── activities/
│   │   ├── __init__.py
│   │   ├── data_validation.py            # Step 1
│   │   ├── build_image.py                # Step 2 (Cloud Build trigger)
│   │   ├── submit_training.py            # Step 3 (Vertex AI CustomJob.create)
│   │   ├── monitor_training.py           # Step 4 (polling + heartbeats)
│   │   ├── save_artifacts.py             # Step 5 (LoRA adapter + checkpoints → GCS)
│   │   ├── register_model.py             # Step 6 (Vertex AI Model Registry)
│   │   ├── upload_hf_hub.py              # Step 7 (Hugging Face Hub)
│   │   ├── prepare_serving.py            # Step 8 (build serving artifact)
│   │   └── notify.py                     # Step 9 (success/failure)
│   ├── policies.py                       # RetryPolicy, timeouts, fallback rules
│   └── tests/                            # Unit + integration tests for activities/workflow
│       ├── conftest.py
│       ├── test_finetune_workflow.py
│       └── test_activities.py
│
├── training/                             # Vertex AI training code (entrypoint = train.py)
│   ├── train.py                          # HF Transformers + PEFT + TRL SFT loop
│   ├── requirements.txt                  # Pinned: transformers, peft, trl, accelerate, …
│   ├── conf/                             # Hydra config root
│   │   ├── config.yaml                   # Defaults + composition list
│   │   ├── model/
│   │   │   ├── llama3_1_8b.yaml
│   │   │   ├── llama3_1_70b.yaml
│   │   │   ├── mistral_7b.yaml
│   │   │   └── qwen2_5_7b.yaml
│   │   ├── peft/
│   │   │   ├── lora.yaml
│   │   │   ├── qlora.yaml
│   │   │   └── none.yaml                 # Full fine-tune (gated by checks)
│   │   ├── data/
│   │   │   ├── chat.yaml                 # Conversational SFT
│   │   │   └── instruct.yaml             # Instruction tuning
│   │   ├── train/
│   │   │   ├── default.yaml              # bf16, paged_adamw_8bit, grad-accum, etc.
│   │   │   └── debug.yaml                # small steps for smoke testing
│   │   └── logging/
│   │       ├── default.yaml              # Cloud Logging + TensorBoard
│   │       └── wandb.yaml                # + W&B
│   ├── callbacks/
│   │   ├── __init__.py
│   │   ├── gcs_checkpoint.py             # Checkpoint sync to GCS every N steps
│   │   ├── tensorboard_gcs.py            # TB logs → Vertex AI TensorBoard
│   │   └── eval_score.py                 # Computes & writes eval_score.json
│   ├── utils/
│   │   ├── data_loader.py                # GCS JSONL → datasets.Dataset
│   │   ├── tokenization.py               # Chat template handling per model family
│   │   └── resume.py                     # Find latest checkpoint in GCS
│   └── tests/
│
├── data_prep/                            # Raw docs → instruction tuning JSONL
│   ├── __init__.py
│   ├── pipeline.py                       # CLI entrypoint: `python -m data_prep.pipeline ...`
│   ├── extractors/                       # 1 file per source format
│   │   ├── pdf.py                        # pdfplumber + ocrmypdf fallback
│   │   ├── docx.py                       # python-docx
│   │   ├── html.py                       # BeautifulSoup + readability-lxml
│   │   └── json_native.py
│   ├── chunkers/
│   │   ├── semantic.py                   # SentenceSplitter (LlamaIndex-style)
│   │   └── fixed.py
│   ├── synth/                            # Optional: synthetic Q/A generation via teacher LLM
│   │   ├── qa_from_chunks.py
│   │   └── prompts.yaml
│   ├── schema.py                         # Pydantic models for the canonical JSONL row
│   ├── validators/
│   │   ├── chat_format.py                # Validates {messages: [...]} per OpenAI chat schema
│   │   └── instruction_format.py         # Validates {instruction, input, output}
│   ├── conf/
│   │   └── default.yaml                  # Chunk size, overlap, formats, filters
│   └── tests/
│
├── serving/                              # vLLM inference container + helpers
│   ├── Dockerfile                        # Stage-2: vllm/vllm-openai + LoRA loader
│   ├── server.py                         # Thin wrapper: launches vLLM with merged adapter
│   ├── merge_adapter.py                  # `python merge_adapter.py <base> <adapter> <out>`
│   ├── requirements.txt
│   └── tests/
│
├── docker/                               # Training-side Dockerfiles
│   ├── train.Dockerfile                  # CUDA 12.4 + Python 3.11 + train code (no model)
│   ├── train.requirements.txt            # Frozen training deps (mirrors training/requirements.txt)
│   ├── worker.Dockerfile                 # Temporal worker (Python 3.11, slim, no CUDA)
│   └── data_prep.Dockerfile              # Optional: pre-built data-prep image
│
├── cloudbuild/                           # Cloud Build YAML files
│   ├── train_image.yaml                  # Builds + pushes training image to AR
│   ├── serving_image.yaml                # Builds + pushes serving image to AR
│   ├── worker_image.yaml                 # Builds + pushes worker image, deploys Cloud Run Job
│   └── data_prep_image.yaml
│
├── configs/                              # YAML job configurations (the "what to fine-tune")
│   ├── jobs/                             # One file per fine-tune campaign
│   │   ├── invoices_llama3_8b_lora.yaml
│   │   ├── contracts_mistral_7b_qlora.yaml
│   │   └── financials_qwen2_5_14b_lora.yaml
│   └── schema.json                       # JSON Schema for job configs (validated in CI)
│
├── scripts/                              # Utility scripts (run locally or from CI)
│   ├── bootstrap_project.sh              # Enables APIs, creates state bucket
│   ├── submit_job.py                     # `submit_job.py configs/jobs/foo.yaml`
│   ├── tail_logs.sh                      # Cloud Logging tail for a workflow id
│   ├── seed_buckets.sh                   # Uploads example raw docs
│   ├── promote_model.py                  # Stage Model Registry version → Prod alias
│   ├── cost_report.py                    # Pulls BQ billing export → markdown cost summary
│   └── smoke_test_serving.py             # End-to-end inference test against vLLM
│
├── docs/                                 # Long-form docs
│   ├── temporal_workflow.md              # Deep-dive on each activity
│   ├── data_format.md                    # Canonical JSONL spec + examples
│   ├── spot_vm_fallback.md               # Decision flowchart for fallback
│   ├── hyperparameters.md                # LoRA/QLoRA recommended starting points by model
│   ├── monitoring.md                     # Dashboards, alert policies, runbooks
│   ├── cost.md                           # Cost model + worked examples
│   └── security.md                       # Threat model + controls
│
└── .github/workflows/
    ├── lint.yml                          # Ruff, Black, Terraform fmt, hadolint, yamllint
    ├── test.yml                          # pytest (data_prep + temporal unit tests)
    ├── validate-configs.yml              # Validates configs/jobs/*.yaml against schema.json
    ├── tf-plan-stage.yml                 # On PR: terraform plan against stage
    ├── deploy-stage.yml                  # On push to `stage`: apply + build + deploy worker
    ├── deploy-prod.yml                   # On push to `main`: apply + build + deploy worker
    └── release.yml                       # Tag-based release: builds versioned images
```

Every file listed above MUST exist in the repository. Files not listed MUST NOT be created without an architecture review.

---

## 3. Temporal Workflow Specification

### 3.1 Workflow identity

- **Workflow type:** `FineTuneWorkflow`
- **Task queue:** `vertex-finetune-{environment}` (e.g. `vertex-finetune-stage`)
- **Workflow ID format:** `ft-{config_hash[:8]}-{yyyymmdd-hhmmss}` — deterministic, allows re-submission and de-duplication via `WorkflowIDReusePolicy.REJECT_DUPLICATE`.
- **Execution timeout:** 24 h (default), configurable per job.
- **Run timeout:** 23 h.
- **Task timeout:** 10 s.

### 3.2 Workflow input contract

The workflow takes a single `FineTuneRequest` (Python `pydantic.BaseModel` shared by client and worker):

```yaml
# FineTuneRequest schema (also exposed as JSON Schema in configs/schema.json)
job_name: string                   # required, ≤ 60 chars, [a-z0-9-]
config_uri: string                 # required, gs://… path to job YAML (configs/jobs/*.yaml)
model:
  base_model_id: string            # e.g. "meta-llama/Llama-3.1-8B-Instruct"
  revision: string|null            # HF commit SHA; null → "main"
peft:
  type: enum[lora, qlora, none]
  lora_r: int                      # default 16
  lora_alpha: int                  # default 32
  lora_dropout: float              # default 0.05
  target_modules: list[string]|null
data:
  raw_uri: string                  # gs://…/raw-documents/<job_name>/
  processed_uri: string|null       # gs://…/processed-datasets/<job_name>/ (if provided, skip Step 1's prep)
  format: enum[chat, instruct]
  train_split: float               # default 0.95
  validation_split: float          # default 0.05
training:
  epochs: int                      # default 3
  per_device_batch_size: int       # default 4
  gradient_accumulation_steps: int # default 4
  learning_rate: float             # default 2e-4 (LoRA), 1e-5 (full)
  warmup_ratio: float              # default 0.03
  max_seq_length: int              # default 4096
  bf16: bool                       # default true
  gradient_checkpointing: bool     # default true
infrastructure:
  machine_type: string             # default "a2-highgpu-1g"
  accelerator_type: string         # default "NVIDIA_TESLA_A100"
  accelerator_count: int           # default 1
  spot: bool                       # default true
  fallback_on_demand: bool         # default true
  max_preemptions: int             # default 2
  region: string                   # default "us-central1"
artifacts:
  output_uri: string               # gs://…/final-models/<job_name>/
  checkpoint_uri: string           # gs://…/checkpoints/<job_name>/
  upload_hf_hub: bool              # default false
  hf_repo_id: string|null          # e.g. "devops-for-hire/invoices-llama3-8b"
  hf_private: bool                 # default true
  register_in_vertex: bool         # default true
  prepare_serving_image: bool      # default true
evaluation:
  min_eval_score: float|null       # default null (no gate)
  retry_on_low_score: bool         # default false
  retry_lr_multiplier: float       # default 0.5
notifications:
  slack_webhook_secret: string|null
  email_to: string|null
```

A concrete **request JSON example** the client sends to Temporal:

```json
{
  "job_name": "invoices-llama3-8b-lora-v1",
  "config_uri": "gs://dfh-stage-id-configs/jobs/invoices_llama3_8b_lora.yaml",
  "model": {
    "base_model_id": "meta-llama/Llama-3.1-8B-Instruct",
    "revision": "main"
  },
  "peft": {
    "type": "lora",
    "lora_r": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05,
    "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"]
  },
  "data": {
    "raw_uri": "gs://dfh-stage-id-raw-documents/invoices/",
    "processed_uri": null,
    "format": "chat",
    "train_split": 0.95,
    "validation_split": 0.05
  },
  "training": {
    "epochs": 3,
    "per_device_batch_size": 4,
    "gradient_accumulation_steps": 4,
    "learning_rate": 2e-4,
    "warmup_ratio": 0.03,
    "max_seq_length": 4096,
    "bf16": true,
    "gradient_checkpointing": true
  },
  "infrastructure": {
    "machine_type": "a2-highgpu-1g",
    "accelerator_type": "NVIDIA_TESLA_A100",
    "accelerator_count": 1,
    "spot": true,
    "fallback_on_demand": true,
    "max_preemptions": 2,
    "region": "us-central1"
  },
  "artifacts": {
    "output_uri": "gs://dfh-stage-id-final-models/invoices-llama3-8b-lora-v1/",
    "checkpoint_uri": "gs://dfh-stage-id-checkpoints/invoices-llama3-8b-lora-v1/",
    "upload_hf_hub": false,
    "hf_repo_id": null,
    "hf_private": true,
    "register_in_vertex": true,
    "prepare_serving_image": true
  },
  "evaluation": {
    "min_eval_score": 0.65,
    "retry_on_low_score": true,
    "retry_lr_multiplier": 0.5
  },
  "notifications": {
    "slack_webhook_secret": "projects/dfh-stage-id/secrets/slack-webhook/versions/latest",
    "email_to": "ml-team@example.com"
  }
}
```

### 3.3 Workflow output contract

```json
{
  "status": "succeeded",
  "workflow_id": "ft-9a3b1c7e-20260512-141022",
  "vertex_job_id": "projects/123/locations/us-central1/customJobs/8821937429347",
  "model": {
    "base_model_id": "meta-llama/Llama-3.1-8B-Instruct",
    "adapter_uri": "gs://dfh-stage-id-final-models/invoices-llama3-8b-lora-v1/adapter/",
    "merged_uri": null,
    "vertex_model_resource": "projects/123/locations/us-central1/models/9182736455@1",
    "hf_repo_id": null
  },
  "metrics": {
    "train_loss_final": 0.812,
    "eval_loss_final": 0.934,
    "eval_score": 0.71,
    "steps_completed": 4680,
    "wall_clock_seconds": 9874
  },
  "cost_estimate_usd": 23.47,
  "preemption_count": 1,
  "fallback_to_on_demand": false,
  "serving_image": "us-central1-docker.pkg.dev/dfh-stage-id/serving/invoices-llama3-8b:v1",
  "started_at": "2026-05-12T14:10:22Z",
  "completed_at": "2026-05-12T16:55:01Z"
}
```

A **failure** payload looks like:

```json
{
  "status": "failed",
  "workflow_id": "ft-9a3b1c7e-20260512-141022",
  "failure_step": "monitor_training",
  "error_type": "VertexJobFailed",
  "message": "CustomJob exited with state JOB_STATE_FAILED: OOM at step 1240",
  "vertex_job_id": "projects/123/locations/us-central1/customJobs/8821937429347",
  "preemption_count": 0,
  "fallback_to_on_demand": false
}
```

### 3.4 Workflow steps (activities)

Each step below documents: **purpose, inputs, outputs, retry policy, timeouts, heartbeat behaviour, idempotency strategy, edge cases.**

#### Step 1 — `data_validation_and_prep`

- **Purpose:** Resolve the dataset. If `data.processed_uri` is provided and contains valid `train.jsonl` + `val.jsonl`, skip preparation; otherwise run the `data_prep` pipeline against `data.raw_uri` and write results to a derived `processed_uri`.
- **Inputs:** `FineTuneRequest.data`, plus the resolved `processed_uri` (derived if null).
- **Outputs:** `DataPrepResult { processed_uri, train_rows, val_rows, sample_uri }`.
- **Retry:** `RetryPolicy(initial_interval=10s, backoff=2.0, max_attempts=3, non_retryable=["DataValidationError"])`.
- **Heartbeat:** every 30 s during long extraction loops; the `data_prep.pipeline` yields a `progress` int.
- **Idempotency:** Output path includes a SHA-256 of the input listing — re-runs are no-ops when the hash matches.
- **Edge cases:**
  - **Empty raw bucket prefix** → raise `DataValidationError("no input files under {raw_uri}")`. Non-retryable.
  - **Single document** → still proceeds (training will warn but not fail). Logged as `WARN: dataset_size_low (n=1)`.
  - **All documents extract to empty text** → raise `DataValidationError("all documents extracted to empty text")`.
  - **`train_split + validation_split != 1.0`** → fail at workflow validation before this step runs.
  - **Existing `processed_uri` with mismatched hash** → re-prepare and overwrite (logged WARN).

#### Step 2 — `build_training_image_if_needed`

- **Purpose:** Resolve the training Docker image. If `request.image_uri` is unset OR points to a manifest that does not exist, trigger a Cloud Build run of `cloudbuild/train_image.yaml`.
- **Inputs:** `image_uri | null`, current git commit SHA (from workflow metadata).
- **Outputs:** `BuildResult { image_uri (full digest), build_id, cached: bool }`.
- **Retry:** 3 attempts, exponential backoff to 5 min.
- **Heartbeat:** every 60 s while polling Cloud Build.
- **Idempotency:** Image tag is `train:{commit_sha}` AND digest-pinned in the output — re-runs return the existing digest without rebuild.
- **Edge cases:**
  - **Build OOM / failed:** activity fails; workflow surfaces `BuildFailed`.
  - **Cloud Build region different from training region:** allowed (cross-region pulls cost is documented in `docs/cost.md`).

#### Step 3 — `submit_training`

- **Purpose:** Create a Vertex AI `CustomJob`. Builds the `worker_pool_specs` from `infrastructure` config, attaches the training image, mounts a TensorBoard instance, passes environment variables for secrets (via Secret Manager references), and sets `scheduling.strategy = SPOT` when `infrastructure.spot=true`.
- **Inputs:** Image URI, full `FineTuneRequest`, plus a workflow-derived `job_display_name`.
- **Outputs:** `SubmitResult { vertex_job_id, scheduling_strategy }`.
- **Retry:** Up to `max_preemptions + 1` for `ResourceExhausted` (Spot-capacity errors), 1 attempt otherwise. Non-retryable: `InvalidArgument`, `PermissionDenied`.
- **Heartbeat:** none (one-shot RPC).
- **Idempotency:** Vertex AI does not support a client-supplied job ID. The activity records the returned `vertex_job_id` in workflow state and, on retry, **first lists CustomJobs filtered by `display_name == job_display_name AND state IN (JOB_STATE_QUEUED, JOB_STATE_PENDING, JOB_STATE_RUNNING)`**. If exactly one match is found, the activity returns that ID instead of submitting again.
- **Edge cases:**
  - **Spot capacity unavailable (`ResourceExhausted` from Vertex AI):** if `fallback_on_demand=true`, the activity sets `scheduling.strategy = STANDARD` and re-submits once. Otherwise raises `SpotUnavailable`.
  - **Quota exceeded for A100 in region:** non-retryable; surfaced as `QuotaExceeded`.
  - **Display-name collision listing returns >1 match:** raise `AmbiguousVertexJob` (non-retryable) — operator must clean up manually.

#### Step 4 — `monitor_training`

- **Purpose:** Poll Vertex AI `CustomJob.get` until terminal state. Emit Temporal **heartbeats** every 30 s containing current state, step counter (read from a sentinel file `gs://…/checkpoints/<job_name>/_progress.json`), and last loss. Stream logs to Cloud Logging.
- **Inputs:** `vertex_job_id`.
- **Outputs:** `MonitorResult { final_state, final_step, train_loss, eval_loss, preemption_count }`.
- **Retry:** Local activity retries 5× on transient `Aiplatform` API errors; the **workflow** then decides whether to re-enter Step 3 based on `preemption_count` and `infrastructure.max_preemptions`.
- **Heartbeat:** every 30 s; `start_to_close_timeout=24h`, `heartbeat_timeout=2m`.
- **Idempotency:** Pure read.
- **Edge cases:**
  - **`JOB_STATE_CANCELLED`:** treat as failure unless cancelled by us (workflow records its own cancellation calls).
  - **`JOB_STATE_FAILED` with `error.code = PREEMPTED`:** increment `preemption_count`; if `< max_preemptions`, return `preempted` and let workflow re-submit (Step 3 again with same config). If `>= max_preemptions` AND `fallback_on_demand=true`, switch to STANDARD and re-submit. Otherwise fail.
  - **Heartbeat timeout:** the activity is retried by Temporal automatically; the underlying Vertex job continues. The workflow tolerates this transparently because the polling activity is idempotent.

#### Step 5 — `save_artifacts`

- **Purpose:** Confirm that `adapter_model.safetensors`, `adapter_config.json`, `tokenizer.json`, and `training_metrics.json` exist under `artifacts.output_uri`. If full-fine-tune (PEFT=none) was used, instead confirm a sharded `model.safetensors.index.json` + shards. Optionally merge LoRA into base weights and write to `output_uri/merged/`.
- **Inputs:** `FineTuneRequest.artifacts`, `MonitorResult.final_step`.
- **Outputs:** `SaveResult { adapter_uri, merged_uri | null, metrics }`.
- **Retry:** 5× exponential to 1 min.
- **Heartbeat:** every 60 s during merge (merging 70B can take 20+ minutes).
- **Idempotency:** Writes are versioned by step count; merging is skipped if `merged_uri` already exists with the expected manifest.
- **Edge cases:**
  - **Missing adapter file:** non-retryable `ArtifactsMissing`.
  - **Bulk delete required (cleanup of partial checkpoints):** use `BulkWriter`-equivalent for GCS — `storage.Client.bucket.delete_blobs(...)` with chunks of 100, NOT a per-object loop in Python (which would be O(N) RPCs). For Firestore audit writes (see §6.4) the Python SDK exposes `bulk_writer = db.bulk_writer()` — always use this rather than the deprecated `WriteBatch`.

#### Step 6 — `register_model`

- **Purpose:** Upload to Vertex AI Model Registry as a new **version** of a Model named `<job_name_prefix>` (the prefix strips the `-vN` suffix). Sets labels `{environment, framework, base_model, peft_type, workflow_id}`.
- **Inputs:** `SaveResult`, `FineTuneRequest`.
- **Outputs:** `RegisterResult { model_resource_name, version_id, aliases }`.
- **Retry:** 3× exponential to 30 s.
- **Skip when:** `artifacts.register_in_vertex == false`.
- **Edge cases:**
  - **First version of a new model:** create the Model with `model_id = <job_name_prefix>` and alias `default`.
  - **Model exists but version count = 100:** Vertex AI default cap. Activity raises `VersionLimitReached` non-retryable; operator must prune.

#### Step 7 — `upload_to_hf_hub`

- **Purpose:** Optionally push the adapter (or merged model) to Hugging Face Hub.
- **Inputs:** `SaveResult.adapter_uri`, `FineTuneRequest.artifacts.hf_repo_id`, `hf_private`, HF_TOKEN from Secret Manager.
- **Outputs:** `HFResult { repo_url, commit_sha }`.
- **Retry:** 5× exponential to 5 min (HF Hub rate-limits aggressively).
- **Skip when:** `artifacts.upload_hf_hub == false`.
- **Edge cases:**
  - **Repo does not exist:** create with `private=hf_private`.
  - **HF_TOKEN invalid:** non-retryable `HFAuthError`.
  - **Upload partial / network drop:** HF SDK supports resumable uploads — re-attempts are safe.

#### Step 8 — `prepare_serving_artifacts`

- **Purpose:** Build a serving image that bundles the chosen adapter (or merged model) on top of `vllm/vllm-openai`. Tags it `serving/<job_name>:<version>`.
- **Inputs:** `RegisterResult.version_id`, `SaveResult.adapter_uri` (or `merged_uri`).
- **Outputs:** `ServingResult { image_uri }`.
- **Implementation:** Triggers Cloud Build of `cloudbuild/serving_image.yaml` with substitutions `_ADAPTER_URI`, `_BASE_MODEL_ID`.
- **Retry:** 3×.
- **Skip when:** `artifacts.prepare_serving_image == false`.
- **Edge cases:** Same as Step 2.

#### Step 9 — `notify`

- **Purpose:** Send a Slack and/or email message with the outcome.
- **Inputs:** Final `WorkflowResult` (success or failure dict).
- **Outputs:** None (best-effort).
- **Retry:** 3× exponential, then **swallow** (a notification failure must not mark the workflow as failed).
- **Skip when:** No notification channels configured.

### 3.5 Conditional logic and signals

- **Low-eval-score retry:** After Step 4 returns successfully, the workflow reads `eval_score.json` from GCS. If `evaluation.retry_on_low_score == true AND eval_score < min_eval_score`, the workflow mutates an internal copy of `training.learning_rate *= retry_lr_multiplier`, increments an internal `retry_round` counter (capped at 1), and re-enters Step 3. This keeps the **same workflow** alive and reuses idempotent activities.
- **Manual cancellation:** Exposed via the Temporal signal `cancel_with_reason(reason: str)`. The workflow calls `CustomJob.cancel`, awaits terminal state, then runs Step 9 with `status=cancelled`.
- **Query handlers:** `get_progress() -> { current_step: str, vertex_state: str, step_count: int, preemptions: int }` — pollable from the CLI for human-friendly status.

### 3.6 Default retry policies (`temporal/policies.py`)

```python
DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
    non_retryable_error_types=[
        "DataValidationError",
        "InvalidArgument",
        "PermissionDenied",
        "QuotaExceeded",
        "ArtifactsMissing",
        "VersionLimitReached",
        "HFAuthError",
    ],
)

LONG_POLL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,  # infinite — workflow controls termination
)
```

---

## 4. Vertex AI CustomJob Specification

### 4.1 `worker_pool_specs` construction

```python
worker_pool_spec = {
  "machine_spec": {
    "machine_type": req.infrastructure.machine_type,          # e.g. "a2-highgpu-1g"
    "accelerator_type": req.infrastructure.accelerator_type,  # e.g. "NVIDIA_TESLA_A100"
    "accelerator_count": req.infrastructure.accelerator_count
  },
  "replica_count": 1,
  "disk_spec": { "boot_disk_type": "pd-ssd", "boot_disk_size_gb": 500 },
  "container_spec": {
    "image_uri": image_uri,
    "command": ["python", "-m", "training.train"],
    "args": [
      f"+job.config_uri={req.config_uri}",
      f"+job.checkpoint_uri={req.artifacts.checkpoint_uri}",
      f"+job.output_uri={req.artifacts.output_uri}",
      f"+job.workflow_id={workflow.info().workflow_id}",
    ],
    "env": [
      { "name": "HF_TOKEN", "value": SECRET_REF("hf-token") },
      { "name": "WANDB_API_KEY", "value": SECRET_REF("wandb-api-key") },
      { "name": "TRANSFORMERS_CACHE", "value": "/gcs-fuse/model-cache" },
    ]
  }
}
```

### 4.2 Job-level fields

| Field | Value | Notes |
|-------|-------|-------|
| `display_name` | `ft-{job_name}-{ts}` | Used by Step 3's idempotency listing |
| `job_spec.worker_pool_specs` | (above) | |
| `job_spec.scheduling.strategy` | `SPOT` or `STANDARD` | Switched by fallback logic |
| `job_spec.scheduling.timeout` | `82800s` (23h) | Match workflow run timeout |
| `job_spec.scheduling.restart_job_on_worker_restart` | `true` | Required for Spot to auto-resume from checkpoint |
| `job_spec.tensorboard` | `projects/.../tensorboards/{tb_id}` | From Terraform output |
| `job_spec.base_output_directory.output_uri_prefix` | `gs://…/checkpoints/{job_name}/` | Vertex writes `output/` & `logs/` here |
| `job_spec.service_account` | Runtime SA (see §6) | |
| `job_spec.network` | `null` or `projects/.../networks/...` | Only if VPC enabled |
| `job_spec.enable_web_access` | `true` (stage), `false` (prod) | Stage allows interactive debugging |
| `labels` | `{ environment, workflow_id, base_model, peft_type }` | For billing analysis |

### 4.3 Spot VM + On-Demand fallback logic

```
        ┌────────────────────────┐
Step 3  │ Submit with strategy=  │
        │ SPOT (if spot=true)    │
        └──────────┬─────────────┘
                   │
   ┌───────────────┴───────────────┐
   │                               │
   ▼                               ▼
ResourceExhausted             Submitted OK
(no capacity)                       │
   │                               ▼
   │ fallback_on_demand=true?  Step 4: monitor
   │   ├─ yes → re-submit STANDARD     │
   │   └─ no  → fail (SpotUnavailable) ▼
   ▼                          JOB_STATE_FAILED
                              error.code=PREEMPTED?
                                   │
                       ┌───────────┴───────────┐
                       │                       │
                preemption_count++         not preempted
                       │                       ▼
                   < max_preemptions?     return failure
                       │
              ┌────────┴────────┐
              │                 │
              ▼                 ▼
        re-submit SPOT     fallback_on_demand?
        (restart from        ├─ yes → STANDARD
        last checkpoint)     └─ no  → fail
```

Implementation: encoded inside the **workflow** (not inside Step 3) so that all decisions are deterministic and replayable.

---

## 5. Data Preparation Pipeline

### 5.1 Pipeline stages (`data_prep/pipeline.py`)

1. **List** all blobs under `data.raw_uri`. Group by extension. Fail-closed on unknown extensions (logged WARN + skipped, unless `--strict`).
2. **Extract** text per file using the matching extractor (`pdf`, `docx`, `html`, `json_native`). Each extractor returns `Iterable[ExtractedDocument]`.
3. **Chunk** each document with the configured chunker (default: `semantic`, max_tokens=1024, overlap=128).
4. **Synthesize** instruction pairs (optional, only if `synth.enabled=true` in config). Uses Vertex AI Gemini 2.0 Flash as the teacher LLM with prompts from `data_prep/synth/prompts.yaml`. Produces `(question, answer)` pairs grounded in chunks. Defaults disabled — most domains supply pre-labelled data.
5. **Format** rows into the configured target format:
   - `chat` → `{"messages": [{"role": "system", "content": ...}, {"role": "user", "content": ...}, {"role": "assistant", "content": ...}]}`
   - `instruct` → `{"instruction": ..., "input": ..., "output": ...}`
6. **Validate** every row using the appropriate Pydantic model. Bad rows are written to `processed_uri/rejected.jsonl` with `_reason`.
7. **Split** rows into `train.jsonl` and `val.jsonl` per the requested ratios. Random seed = `int(sha256(job_name)[:8], 16)` for reproducibility.
8. **Sample** the first 5 rows to `processed_uri/sample.jsonl` for human review.
9. **Write** a `manifest.json` containing `input_listing_sha256`, `n_train`, `n_val`, `n_rejected`, `format`, `tokenizer_validation_pass`, `pipeline_version`.

### 5.2 Canonical JSONL example — `chat` format

```json
{"messages":[{"role":"system","content":"You are an accounts-payable assistant."},{"role":"user","content":"Extract vendor, total, and due date from this invoice:\n\nACME Corp\nInvoice #12345\nDue: 2026-06-01\nTotal: $1,250.00"},{"role":"assistant","content":"{\"vendor\":\"ACME Corp\",\"total\":1250.00,\"due_date\":\"2026-06-01\"}"}]}
```

### 5.3 Canonical JSONL example — `instruct` format

```json
{"instruction":"Extract invoice fields as JSON.","input":"ACME Corp\nInvoice #12345\nDue: 2026-06-01\nTotal: $1,250.00","output":"{\"vendor\":\"ACME Corp\",\"total\":1250.00,\"due_date\":\"2026-06-01\"}"}
```

### 5.4 Edge cases

| Input | Behaviour |
|-------|-----------|
| Empty raw bucket | `DataValidationError` from Step 1 |
| Single document | Proceeds; `WARN: dataset_size_low (n=1)` |
| Document extracts to empty string | Skipped; counted in `n_rejected` |
| All documents extract to empty | `DataValidationError("all documents extracted to empty text")` |
| Document larger than `max_seq_length × 4 chars` | Chunked. Each chunk becomes a separate training row. |
| Duplicate (sha256 of content) | Deduplicated; first occurrence kept |
| Tokenizer fails on a row (e.g. surrogate pair) | Row rejected; reason `tokenization_failed` |
| `train_split + validation_split != 1.0` | Validator rejects request **before** Step 1 |
| `n_train == 0` after splitting (e.g. only 1 row with 0.95 split) | `DataValidationError("empty training split")` |

### 5.5 Sentinel / progress file

`gs://<bucket>/processed-datasets/<job_name>/_progress.json` is written every 50 files extracted, so monitoring activities can heartbeat with progress.

```json
{ "stage": "extract", "files_done": 250, "files_total": 1184, "started_at": "2026-05-12T14:11:03Z" }
```

---

## 6. GCS Bucket Layout

All buckets are created in `terraform/modules/gcs_buckets`. **Naming pattern:** `{project_id}-{purpose}` (lowercased, dashed).

| Logical bucket | Purpose | Lifecycle | Versioning | Public |
|----------------|---------|-----------|------------|--------|
| `<project>-raw-documents` | Inputs from customer/dropbox | Delete after 90 d in `_quarantine/` prefix only | enabled | private |
| `<project>-processed-datasets` | Output of data_prep | Delete after 30 d; transition Nearline after 7 d | enabled | private |
| `<project>-checkpoints` | Training checkpoints (transient) | Delete after 14 d | disabled | private |
| `<project>-final-models` | Adapters, merged weights, eval scores | Delete after 365 d; Nearline after 30 d | enabled | private |
| `<project>-build-artifacts` | Cloud Build logs, SBOMs | Delete after 90 d | disabled | private |
| `<project>-configs` | Job YAMLs (mirror of `configs/jobs/`) | none | enabled | private |
| `<project>-temporal-data` | Temporal worker scratch / signal artifacts (only if self-hosted) | Delete after 30 d | disabled | private |

### 6.1 Bucket-level IAM

| Bucket | Reader | Writer | Admin |
|--------|--------|--------|-------|
| `*-raw-documents` | Worker SA, Training SA | Data-Ingestion SA, humans (group) | `tf-deploy@…` |
| `*-processed-datasets` | Training SA | Worker SA | `tf-deploy@…` |
| `*-checkpoints` | Training SA | Training SA, Worker SA | `tf-deploy@…` |
| `*-final-models` | Training SA, Serving SA, humans (group) | Training SA, Worker SA | `tf-deploy@…` |
| `*-build-artifacts` | humans (group) | Cloud Build SA | `tf-deploy@…` |
| `*-configs` | Worker SA, Training SA, humans (group) | humans (group), CI SA | `tf-deploy@…` |

### 6.2 Folder conventions inside each bucket

```
*-raw-documents/
  └── <job_name>/<incoming_filename>

*-processed-datasets/
  └── <job_name>/
      ├── train.jsonl
      ├── val.jsonl
      ├── rejected.jsonl
      ├── sample.jsonl
      ├── manifest.json
      └── _progress.json

*-checkpoints/
  └── <job_name>/
      ├── checkpoint-100/
      ├── checkpoint-200/
      ├── _progress.json
      └── logs/

*-final-models/
  └── <job_name>/
      ├── adapter/
      │   ├── adapter_model.safetensors
      │   ├── adapter_config.json
      │   └── tokenizer.json
      ├── merged/                  # optional
      ├── training_metrics.json
      ├── eval_score.json
      └── model_card.md
```

### 6.3 Bulk delete in cleanup scripts

When deleting many objects (e.g. clearing old checkpoints in `scripts/cleanup_checkpoints.py`), **always use** `storage.Client.bucket.delete_blobs(blobs, on_error=...)` in batches of 100. Do not loop `blob.delete()` per object — that's O(N) RPCs and is the GCS analog of Firestore's deprecated `Batch()`.

### 6.4 Optional Firestore audit log

If the deployment enables `firestore_audit = true` (off by default in this template), workflow events are mirrored to a Firestore collection `finetune_jobs/{workflow_id}/events/{seq}`. Bulk inserts/deletes **must** use the `BulkWriter` API (`db.bulk_writer()` in Python) — the deprecated `WriteBatch` is forbidden.

---

## 7. IAM Bindings Table

Each service account is created in `terraform/modules/iam`. Conventions: `<purpose>-<env>@<project>.iam.gserviceaccount.com`. Maximum SA name length is 30 chars including suffix — keep `<purpose>` short.

| SA (logical name) | Email pattern | Roles | Bindings rationale |
|---|---|---|---|
| **`tf-deploy`** | `tf-deploy-<env>@<proj>.iam.gserviceaccount.com` | `roles/owner` on project (scoped, used only from GitHub Actions OIDC) | Terraform apply. Stored as `GCP_STAGE_SA_KEY` / `GCP_PROD_SA_KEY` (or via Workload Identity Federation — preferred). |
| **`worker`** | `worker-<env>@…` | `roles/aiplatform.user`, `roles/cloudbuild.builds.editor`, `roles/storage.objectAdmin` (scoped to relevant buckets), `roles/secretmanager.secretAccessor`, `roles/logging.logWriter`, `roles/monitoring.metricWriter`, `roles/run.invoker` | Temporal worker submits Vertex jobs, triggers Cloud Build, reads/writes data buckets. |
| **`training`** | `training-<env>@…` | `roles/aiplatform.user`, `roles/storage.objectAdmin` (scoped), `roles/secretmanager.secretAccessor` (only HF_TOKEN, WANDB_API_KEY), `roles/logging.logWriter`, `roles/aiplatform.tensorboardWebAppUser` | Used by Vertex AI CustomJob runtime to read processed data, write checkpoints/artifacts, push TensorBoard logs, read secrets. |
| **`serving`** | `serving-<env>@…` | `roles/storage.objectViewer` (final-models only), `roles/aiplatform.modelUser`, `roles/secretmanager.secretAccessor` | Runtime SA for serving container (Cloud Run / GKE — separate template). |
| **`cloudbuild`** | `cloudbuild-<env>@…` | `roles/artifactregistry.writer`, `roles/storage.objectAdmin` (build-artifacts bucket), `roles/logging.logWriter`, `roles/run.admin` (for worker image deploys) | Cloud Build builds + pushes images, can deploy Cloud Run Job. |
| **`data-ingestion`** | `ingest-<env>@…` | `roles/storage.objectAdmin` (raw-documents only) | Optional. Used by external customer-side uploaders. |
| **`scheduler`** | `scheduler-<env>@…` | `roles/run.invoker` on worker, `roles/aiplatform.user` (if scheduling Vertex jobs directly) | Used by Cloud Scheduler to trigger periodic workflows. |

### 7.1 Workload Identity Federation (preferred over SA keys)

For GitHub Actions deploys, configure:

- `google_iam_workload_identity_pool` (`github-pool-<env>`)
- `google_iam_workload_identity_pool_provider` (OIDC against `https://token.actions.githubusercontent.com`)
- `google_service_account_iam_binding` granting `roles/iam.workloadIdentityUser` to the GitHub repo principal on `tf-deploy-<env>`.

This eliminates JSON key material in GitHub Secrets — only the pool/provider IDs and SA email are stored.

### 7.2 Conditional IAM bindings (least-privilege)

All `roles/storage.objectAdmin` bindings on the worker / training SA are **scoped to specific buckets** using `google_storage_bucket_iam_member` (not project-level). Project-level `objectAdmin` is forbidden.

---

## 8. Secret Manager Secrets

Secrets are created in `terraform/modules/secret_manager`. **Secret payloads are NEVER stored in Terraform state.** They are created as empty placeholders by Terraform; values are populated by:
- A human via `gcloud secrets versions add … --data-file=-` (preferred), OR
- A bootstrap script `scripts/bootstrap_secrets.sh` that prompts for each value.

| Secret name | Purpose | Consumers (SA principals) | Required? |
|---|---|---|---|
| `hf-token` | Hugging Face Hub auth (download gated models, push adapters) | `training-<env>`, `worker-<env>` | yes if `upload_hf_hub=true` OR base model is gated |
| `wandb-api-key` | Weights & Biases logging | `training-<env>` | only if logging profile `wandb` |
| `temporal-api-key` | Auth to Temporal Cloud namespace | `worker-<env>` | yes if `TEMPORAL_CLOUD=true` |
| `temporal-tls-cert` | mTLS client cert | `worker-<env>` | yes if `TEMPORAL_CLOUD=true` |
| `temporal-tls-key` | mTLS client key | `worker-<env>` | yes if `TEMPORAL_CLOUD=true` |
| `slack-webhook` | Slack notifications from Step 9 | `worker-<env>` | optional |
| `sendgrid-api-key` | Email notifications | `worker-<env>` | optional |
| `github-pat-readonly` | Cloud Build → private GitHub repos (if any submodules) | `cloudbuild-<env>` | optional |

Secrets are referenced in Vertex AI CustomJob env vars using the form `projects/<project>/secrets/<name>/versions/latest` (Vertex AI auto-resolves Secret Manager references when granted `roles/secretmanager.secretAccessor`).

---

## 9. Cloud Build CI/CD

### 9.1 Triggers (Terraform-managed in `modules/cloud_build/`)

| Trigger name | Source | Trigger event | YAML | Substitutions |
|---|---|---|---|---|
| `train-image-<env>` | GitHub: `vpoluyaktov/gcp-vertex-ai-jobs-template` | push to `stage` / `main` (path filters: `training/**`, `docker/train.Dockerfile`) | `cloudbuild/train_image.yaml` | `_REGION`, `_AR_REPO`, `_IMAGE_NAME=train` |
| `serving-image-<env>` | GitHub | push (path: `serving/**`) OR manual (from Step 8) | `cloudbuild/serving_image.yaml` | `_BASE_MODEL_ID`, `_ADAPTER_URI`, `_TAG` |
| `worker-image-<env>` | GitHub | push (path: `temporal/**`, `docker/worker.Dockerfile`) | `cloudbuild/worker_image.yaml` | `_CLOUD_RUN_JOB`, `_REGION` |
| `data-prep-image-<env>` | GitHub | push (path: `data_prep/**`, `docker/data_prep.Dockerfile`) | `cloudbuild/data_prep_image.yaml` | — |

### 9.2 `cloudbuild/train_image.yaml` (skeleton — illustrative, not implementation)

```yaml
steps:
  - id: build
    name: gcr.io/cloud-builders/docker
    args:
      - build
      - --file=docker/train.Dockerfile
      - --tag=${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_AR_REPO}/${_IMAGE_NAME}:${SHORT_SHA}
      - --tag=${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_AR_REPO}/${_IMAGE_NAME}:latest
      - --cache-from=${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_AR_REPO}/${_IMAGE_NAME}:latest
      - .
  - id: push
    name: gcr.io/cloud-builders/docker
    args:
      - push
      - --all-tags
      - ${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_AR_REPO}/${_IMAGE_NAME}
  - id: scan
    name: gcr.io/cloud-builders/gcloud
    entrypoint: bash
    args:
      - -c
      - gcloud artifacts docker images scan ${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_AR_REPO}/${_IMAGE_NAME}:${SHORT_SHA} --format=json > sbom.json && gsutil cp sbom.json gs://${PROJECT_ID}-build-artifacts/sboms/${SHORT_SHA}.json
options:
  machineType: E2_HIGHCPU_8
  logging: CLOUD_LOGGING_ONLY
timeout: 1800s
serviceAccount: projects/${PROJECT_ID}/serviceAccounts/cloudbuild-${_ENV}@${PROJECT_ID}.iam.gserviceaccount.com
images:
  - ${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_AR_REPO}/${_IMAGE_NAME}:${SHORT_SHA}
  - ${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_AR_REPO}/${_IMAGE_NAME}:latest
```

### 9.3 GitHub Actions workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `lint.yml` | PR + push | ruff, black --check, terraform fmt -check, hadolint, yamllint, jsonschema-validate `configs/jobs/*.yaml` |
| `test.yml` | PR + push | `pytest` for `data_prep/`, `temporal/`, `training/utils/` (no GPU tests) |
| `validate-configs.yml` | PR (on `configs/**`) | Validates every job YAML against `configs/schema.json` |
| `tf-plan-stage.yml` | PR | `terraform plan -no-color -input=false` against stage; comments diff |
| `deploy-stage.yml` | push to `stage` | terraform apply (stage) → build worker image → update Cloud Run Job → smoke-submit a `debug` workflow |
| `deploy-prod.yml` | push to `main` | Same as stage but against prod, with manual approval (GitHub Environments) |
| `release.yml` | `v*` tag | Builds versioned train/serving/worker images, attaches SBOM to GitHub release |

**GitHub Actions versions** — mandated by `~/.claude/rules/template-standards.md`. Pin to these exact versions in every workflow file:

- `actions/checkout@v4`
- `actions/setup-go@v5` (if any Go helpers are added later)
- `actions/setup-python@v5`
- `actions/cache@v4`
- `google-github-actions/auth@v2`
- `google-github-actions/setup-gcloud@v2`
- `hashicorp/setup-terraform@v3`
- `golangci/golangci-lint-action@v6` (reserved)
- `docker/login-action@v3`
- `docker/setup-buildx-action@v3`

No `@v3` or older versions for any of the above.

### 9.4 Pre-deploy resource conflict check

Before `terraform apply` in `deploy-stage.yml` / `deploy-prod.yml`, run:

```bash
gcloud artifacts repositories list --location=us-central1 --format="value(name)" | grep -E '<app>-(training|serving)$' || true
gcloud run jobs list --region=us-central1 --format="value(name)" | grep -E '^<app>-worker-<env>$' || true
```

The CI step **fails fast with a clear message** if a resource that Terraform expects to create already exists outside its state — this prevents mid-apply 409s.

### 9.5 Terraform version

All Terraform-using CI steps **MUST pin to >= 1.6** (required because the template uses variables in `import {}` blocks; 1.5 does not support this).

```yaml
- uses: hashicorp/setup-terraform@v3
  with:
    terraform_version: 1.9.5     # ≥ 1.6
```

---

## 10. Training Container Specification

### 10.1 `docker/train.Dockerfile` design intent

- **Base:** `nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04` (digest-pinned).
- **Python:** 3.11 via `deadsnakes` PPA.
- **System deps:** `git`, `build-essential`, `libaio-dev` (for DeepSpeed if enabled), `curl`, `gcsfuse` (optional mount for large model caches).
- **Python deps:** Frozen `training/requirements.txt` (sha256-locked via `pip-compile`).
- **App code:** Copies `training/` only. Data and model weights are downloaded at runtime.
- **User:** Non-root `trainer` uid 1000.
- **Entrypoint:** `python -m training.train`.
- **HEALTHCHECK:** None (Vertex AI does its own).
- **Multi-stage:** Stage 1 builds wheels for `flash-attn` & `bitsandbytes`; stage 2 copies them in.

### 10.2 Training entrypoint contract

`training/train.py` must:

1. Parse Hydra config from `+job.config_uri` (downloaded from GCS).
2. Initialise `accelerate` for single-GPU or multi-GPU (`accelerate.PartialState()`).
3. Load tokenizer + model with `peft.LoraConfig` or `peft.PrepareModelForKbitTraining` (QLoRA).
4. Build `datasets.Dataset` from `gs://…/processed-datasets/<job>/train.jsonl` and `val.jsonl`.
5. Build `trl.SFTTrainer` with callbacks: `GcsCheckpointCallback`, `TensorBoardCallback`, `EvalScoreCallback`, optionally `WandbCallback`.
6. Resume from latest checkpoint in `checkpoint_uri` if present (`utils.resume.find_latest`).
7. Call `trainer.train()`.
8. Save adapter (`trainer.save_model(output_uri/adapter/)`), tokenizer, `training_metrics.json`, `eval_score.json`.
9. Optionally merge LoRA → base and save to `output_uri/merged/` if `peft.merge_after_training=true`.
10. Exit 0 on success, non-zero on failure.

### 10.3 Resume behaviour

On startup, list `checkpoint_uri`; if `checkpoint-*` directories exist, pick the highest step number, download it locally, and call `trainer.train(resume_from_checkpoint=<local_path>)`. This is the key to making Spot VM preemption transparent.

### 10.4 Hyperparameter config schema (LoRA / QLoRA)

```yaml
# training/conf/peft/lora.yaml
peft:
  type: lora                       # lora | qlora | none
  r: 16                            # int, 4..256
  alpha: 32                        # int
  dropout: 0.05                    # float, 0..1
  bias: none                       # none | all | lora_only
  task_type: CAUSAL_LM
  target_modules:                  # list[str] — defaults inferred per model family
    - q_proj
    - k_proj
    - v_proj
    - o_proj
  modules_to_save: []              # extra trainable modules (e.g. embed_tokens)
  merge_after_training: false      # if true, also produce a merged-weight model
```

```yaml
# training/conf/peft/qlora.yaml — extends lora.yaml
peft:
  type: qlora
  r: 32
  alpha: 64
  bnb_4bit_quant_type: nf4         # nf4 | fp4
  bnb_4bit_compute_dtype: bfloat16 # float16 | bfloat16
  bnb_4bit_use_double_quant: true
```

### 10.5 Training callbacks (concrete behaviour)

- **`GcsCheckpointCallback`** — on every `save_steps`, uploads `<local>/checkpoint-N/` to `gs://…/checkpoints/<job>/checkpoint-N/` using `gsutil -m rsync -r`. Writes `_progress.json` with `{step, loss, lr}`.
- **`EvalScoreCallback`** — at end of each epoch, runs a held-out scoring function (configurable per data format — defaults: exact-match for JSON outputs, ROUGE-L for free text) and writes `eval_score.json`.
- **`TensorBoardCallback`** — writes to local `tb_logs/`, then `aip-tensorboard-log-directory` env var sync.
- **`WandbCallback`** — only if `logging=wandb` profile.

---

## 11. Serving Container Specification (vLLM)

### 11.1 `serving/Dockerfile`

- **Base:** `vllm/vllm-openai:v0.6.3` (digest-pinned).
- **At build time:** Optionally bake an adapter into the image via the substitution `_ADAPTER_URI` — the build step downloads the adapter and either:
  - Saves it under `/models/adapter` (vLLM dynamically loads via `--enable-lora --lora-modules`), OR
  - Pre-merges into the base weights and saves under `/models/merged` (via `serving/merge_adapter.py`).
- **Selection rule:** If `peft.merge_after_training=true` was used during fine-tuning, the merged weights are used. Otherwise the adapter is loaded at runtime.
- **Entrypoint:** `serving/server.py`, which `exec`s `python -m vllm.entrypoints.openai.api_server` with model path & LoRA flags.

### 11.2 Serving container env vars

| Env var | Purpose |
|---|---|
| `BASE_MODEL_ID` | Hugging Face base model id |
| `ADAPTER_PATH` | Local path of adapter (or `none` if merged) |
| `MAX_MODEL_LEN` | Inference context cap |
| `GPU_MEMORY_UTILIZATION` | Default 0.90 |
| `PORT` | vLLM HTTP port; default 8000 |

### 11.3 Smoke test (`scripts/smoke_test_serving.py`)

Posts a chat-completions request and asserts a 200 response with non-empty content. Run in CI for every serving image build.

---

## 12. Terraform Module Layout

Terraform files: **Architect specifies; DevOps implements.** This template does not ship `.tf` files; the DevOps Engineer creates them from this spec.

### 12.1 Per-environment wiring (`terraform/stage/main.tf` outline)

```
module "gcs_buckets"      → 5 buckets + lifecycle + IAM
module "iam"              → all SAs + role bindings (incl. Workload Identity)
module "artifact_registry"→ 2 repos: training, serving (+ data_prep if used)
module "secret_manager"   → 5–8 secrets (per §8)
module "vertex_ai"        → TensorBoard instance, Model Registry placeholders
module "cloud_run_worker" → Cloud Run Job + scheduler (and/or Service)
module "cloud_build"      → 4 triggers
module "scheduler"        → optional Cloud Scheduler jobs
module "networking"       → optional VPC + private service access
module "monitoring"       → alert policies + uptime checks + dashboards
```

### 12.2 Required Terraform variables

| Variable | Type | Required | Description |
|---|---|---|---|
| `project_id` | string | yes | GCP project ID (e.g. `dfh-stage-id`) |
| `region` | string | yes | Default `us-central1` |
| `environment` | string | yes | `stage` or `prod` |
| `app_name` | string | yes | `gcp-vertex-ai-jobs-template` (or override per fork) |
| `dns_zone_project` | string | yes | `dfh-ops-id` |
| `dns_zone_name` | string | yes | `demo-devops-for-hire-com` |
| `temporal_cloud` | bool | no, default `false` | If true, skip self-hosted Temporal module |
| `temporal_namespace` | string | conditional | Required if `temporal_cloud=true` |
| `vpc_enabled` | bool | no, default `false` | If true, create VPC + private service access |
| `enable_w_and_b` | bool | no, default `false` | Provision `wandb-api-key` secret |
| `notification_channels` | list(string) | no, default `[]` | Resource IDs for monitoring alerts |
| `tf_state_bucket` | string | yes | `dfh-stage-tfstate` or `dfh-prod-tfstate` |
| `worker_image_tag` | string | no, default `latest` | Allows pinning the Cloud Run worker |
| `enable_firestore_audit` | bool | no, default `false` | Optional audit log mirror to Firestore |

### 12.3 Terraform state backend

```hcl
# terraform/stage/backend.tf
terraform {
  required_version = ">= 1.6"
  backend "gcs" {
    bucket = "dfh-stage-tfstate"
    prefix = "gcp-vertex-ai-jobs-template/state"
  }
}
```

Mirror for prod: `bucket = "dfh-prod-tfstate"`.

### 12.4 Required providers

```hcl
required_providers {
  google      = { source = "hashicorp/google",      version = "~> 5.40" }
  google-beta = { source = "hashicorp/google-beta", version = "~> 5.40" }
  random      = { source = "hashicorp/random",      version = "~> 3.6" }
}
```

### 12.5 DNS records

If a public-facing serving endpoint is provisioned (out-of-scope for this template's default config but supported via the GKE template), the DNS records are:

| Environment | Hostname | Type | Target |
|---|---|---|---|
| Staging | `gcp-vertex-ai-jobs-template.stage.demo.devops-for-hire.com` | A | (filled by serving template) |
| Production | `gcp-vertex-ai-jobs-template.demo.devops-for-hire.com` | A | (filled by serving template) |

This template **does not** by default expose a public endpoint — fine-tuning is an internal workflow. The DNS names are reserved so the eventual serving deployment can claim them.

---

## 13. Makefile targets

The `Makefile` provides a single source of truth for developer commands. All targets must be implemented:

| Target | What it does |
|---|---|
| `make help` | Prints all targets with descriptions |
| `make bootstrap ENV=stage` | Runs `scripts/bootstrap_project.sh` (enables APIs, creates state bucket) |
| `make init ENV=stage` | `terraform -chdir=terraform/$(ENV) init` |
| `make plan ENV=stage` | `terraform -chdir=terraform/$(ENV) plan` |
| `make apply ENV=stage` | `terraform -chdir=terraform/$(ENV) apply` |
| `make destroy ENV=stage` | `terraform -chdir=terraform/$(ENV) destroy` |
| `make lint` | ruff + black + tf fmt + hadolint + yamllint |
| `make test` | `pytest` for `temporal/`, `data_prep/`, `training/utils/` |
| `make build-worker ENV=stage` | Triggers `worker-image-<env>` Cloud Build |
| `make build-train ENV=stage` | Triggers `train-image-<env>` Cloud Build |
| `make build-serving ENV=stage ADAPTER_URI=...` | Triggers `serving-image-<env>` |
| `make submit CONFIG=configs/jobs/foo.yaml ENV=stage` | `python scripts/submit_job.py …` |
| `make tail WORKFLOW=ft-abc123 ENV=stage` | `scripts/tail_logs.sh` |
| `make cancel WORKFLOW=ft-abc123 ENV=stage` | Sends `cancel_with_reason` signal |
| `make promote MODEL=foo VERSION=3` | `scripts/promote_model.py` — moves a Model Registry version to prod alias |
| `make cost-report START=2026-04-01 END=2026-04-30` | `scripts/cost_report.py` |
| `make smoke-serving IMAGE=...` | `scripts/smoke_test_serving.py` |

---

## 14. Configuration Reference

### 14.1 Job YAML — concrete example (`configs/jobs/invoices_llama3_8b_lora.yaml`)

```yaml
# A single fine-tune campaign. This is what `make submit` reads.
job_name: invoices-llama3-8b-lora-v1
model:
  base_model_id: meta-llama/Llama-3.1-8B-Instruct
  revision: main
peft:
  type: lora
  lora_r: 16
  lora_alpha: 32
  lora_dropout: 0.05
  target_modules: [q_proj, k_proj, v_proj, o_proj]
data:
  raw_uri: gs://dfh-stage-id-raw-documents/invoices/
  processed_uri: null
  format: chat
  train_split: 0.95
  validation_split: 0.05
training:
  epochs: 3
  per_device_batch_size: 4
  gradient_accumulation_steps: 4
  learning_rate: 2e-4
  warmup_ratio: 0.03
  max_seq_length: 4096
  bf16: true
  gradient_checkpointing: true
infrastructure:
  machine_type: a2-highgpu-1g
  accelerator_type: NVIDIA_TESLA_A100
  accelerator_count: 1
  spot: true
  fallback_on_demand: true
  max_preemptions: 2
  region: us-central1
artifacts:
  output_uri: gs://dfh-stage-id-final-models/invoices-llama3-8b-lora-v1/
  checkpoint_uri: gs://dfh-stage-id-checkpoints/invoices-llama3-8b-lora-v1/
  upload_hf_hub: false
  hf_repo_id: null
  hf_private: true
  register_in_vertex: true
  prepare_serving_image: true
evaluation:
  min_eval_score: 0.65
  retry_on_low_score: true
  retry_lr_multiplier: 0.5
notifications:
  slack_webhook_secret: projects/dfh-stage-id/secrets/slack-webhook/versions/latest
  email_to: null
```

### 14.2 Job YAML — JSON Schema validation

`configs/schema.json` enforces the structure of every file under `configs/jobs/`. CI rejects PRs that introduce invalid YAML. The schema mirrors the Pydantic `FineTuneRequest` model.

### 14.3 `.env.example`

```
# Project + environment
GCP_PROJECT_ID=dfh-stage-id
GCP_REGION=us-central1
ENVIRONMENT=stage

# Temporal
TEMPORAL_CLOUD=false
TEMPORAL_ADDRESS=temporal-frontend.temporal.svc.cluster.local:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TASK_QUEUE=vertex-finetune-stage

# Temporal Cloud (only if TEMPORAL_CLOUD=true)
TEMPORAL_CLOUD_ADDRESS=
TEMPORAL_CLOUD_NAMESPACE=
TEMPORAL_CLOUD_API_KEY=

# Optional integrations
WANDB_PROJECT=
HF_USERNAME=

# Local dev
LOG_LEVEL=INFO
```

---

## 15. Monitoring & Alerting

### 15.1 Metrics emitted by the worker

The worker exposes Prometheus-style metrics on `:9090/metrics` and writes the same to Cloud Monitoring as custom metrics under `custom.googleapis.com/finetune/*`:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `finetune_workflows_total` | counter | `status`, `model`, `peft_type` | Total workflows by terminal status |
| `finetune_workflow_duration_seconds` | histogram | `model`, `peft_type` | Wall-clock |
| `finetune_preemptions_total` | counter | `model` | Spot preemptions observed |
| `finetune_fallback_to_on_demand_total` | counter | `model` | Times we switched to STANDARD |
| `finetune_eval_score` | gauge | `model`, `workflow_id` | Last computed eval score |
| `finetune_cost_estimate_usd` | gauge | `model`, `workflow_id` | Estimated cost |

### 15.2 Default alert policies

| Alert | Condition | Severity |
|---|---|---|
| **Workflow failure** | `finetune_workflows_total{status="failed"}` increases by ≥ 1 in 5 min | critical |
| **Preemption storm** | `finetune_preemptions_total` rate > 0.5/min over 30 min | warning |
| **Fallback to On-Demand** | `finetune_fallback_to_on_demand_total` increases by ≥ 1 | info |
| **No workflows in 24h (prod)** | Absence: `finetune_workflows_total` flat for 24h | info |
| **Cost overrun** | Daily project cost > $X (configurable) | warning |
| **Worker down** | Cloud Run Job no successful execution in 2h | critical |
| **TensorBoard write failure** | Log-based metric on `aiplatform.googleapis.com` error events | warning |

Channels: configurable via `var.notification_channels` (Slack via webhook, email, PagerDuty).

### 15.3 Dashboards

A single Cloud Monitoring dashboard `Finetune Overview` shipping in `modules/monitoring`, with widgets:
- Active CustomJobs (gauge)
- Workflow throughput (24h, 7d)
- Preemption + fallback counts
- Eval score distribution (heatmap)
- GCS storage by bucket (line)
- Cost by SKU (stacked, from billing export)

---

## 16. Cost Optimization

### 16.1 Practices encoded into the template

1. **Spot VMs by default.** Saves ~60–80% on GPU compute. Fallback is opt-in but enabled by default.
2. **LoRA / QLoRA over full fine-tuning.** A 8B LoRA SFT at `r=16` typically completes in 1–3 GPU-hours; full fine-tune of the same model takes 20–40× longer.
3. **`gradient_checkpointing=true`** by default — trades ~30% throughput for ~2× lower VRAM, enabling smaller machine types.
4. **GCS lifecycle rules** auto-delete checkpoints after 14 d, processed datasets after 30 d.
5. **Single replica.** No multi-node by default — only added by config for 70B-class training.
6. **Vertex AI base output directory + checkpoint resume** means preempted Spot jobs don't waste prior progress.
7. **Artifact Registry vulnerability scanning** on demand (cost ~$0.10 per image per scan); enabled in CI only at release.
8. **Cloud Run worker scales to zero** when no workflows are running (Cloud Run Job model: invocation-based).
9. **Logs sampled.** Training logs at INFO; DEBUG only in `debug.yaml` training profile.

### 16.2 Cost monitoring

`scripts/cost_report.py` queries the BigQuery billing export and outputs a Markdown table broken down by service (Vertex AI, GCS, Artifact Registry, Cloud Build, Cloud Logging, Secret Manager).

### 16.3 Indicative cost table

(Reproduced from `docs/cost.md`; numbers are rough order-of-magnitude.)

| Workload | Machine | Duration | Spot? | Rough cost (USD) |
|---|---|---|---|---|
| 8B LoRA SFT, 50k rows | 1× A100 40GB | 2 h | yes | ~$3–6 |
| 8B LoRA SFT, 50k rows | 1× A100 40GB | 2 h | no | ~$8–12 |
| 8B QLoRA SFT, 200k rows | 1× L4 24GB | 6 h | yes | ~$2–4 |
| 70B LoRA SFT, 50k rows | 4× A100 80GB | 12 h | yes | ~$80–120 |
| 70B Full fine-tune | 8× H100 | 48 h | yes | ~$1,500–2,500 |

---

## 17. Security

### 17.1 Practices encoded into the template

- **Workload Identity Federation** for GitHub Actions → GCP. No long-lived JSON keys.
- **Workload Identity** for Cloud Run Job worker → SA. No SA keys mounted.
- **Vertex AI CustomJob runs under a dedicated `training` SA** (not the default Compute Engine SA, which has broad permissions).
- **Bucket IAM is per-bucket** — never project-level `objectAdmin`.
- **Secret Manager** for HF_TOKEN, WANDB_API_KEY, Temporal credentials. Vertex AI auto-resolves secret refs in env vars; secrets never appear in container args, logs, or workflow histories.
- **Container images are private** — Artifact Registry repos with `allUsers` access denied. Vulnerability scanning enabled.
- **VPC + Private Service Access** is supported (off by default; opt-in via `var.vpc_enabled`). When enabled, Vertex AI CustomJob uses peering to access Cloud Storage and Secret Manager via private endpoints.
- **Audit logging.** Data Access audit logs enabled for Secret Manager and GCS in prod (off in stage to control cost).
- **No HF_TOKEN in workflow input.** Always referenced by Secret Manager resource name, never as a plain value in the `FineTuneRequest`.
- **Customer data isolation.** Each fork of this template lives in a dedicated GCP project; cross-project IAM bindings are forbidden.
- **Model artifacts are private** by default (HF Hub `private=true`).

### 17.2 Threat model summary

| Threat | Mitigation |
|---|---|
| Leaked HF_TOKEN | Stored in Secret Manager; rotated quarterly; access audited |
| Compromised CI runner | OIDC short-lived tokens; per-env SA scoping; branch protection on `stage`/`main` |
| Malicious training data injecting prompts | Data prep validators reject non-UTF-8 / oversize rows; model-card warns about untrusted inputs |
| Container supply chain | Base images digest-pinned; AR vulnerability scan; SBOM artifact attached to release |
| Cost-DoS via runaway training | Vertex job timeout 23 h; budget alert; max_preemptions cap |
| Cross-tenant data leak | Per-project isolation; bucket IAM per-bucket; no shared SAs |

---

## 18. Cross-cutting Edge Cases

| Scenario | Documented behaviour |
|---|---|
| Workflow restarted by Temporal due to worker crash | Idempotent activities ensure no duplicate Vertex jobs; Step 3 dedups by display_name |
| Vertex job ID lost mid-workflow | Step 3's de-dup listing recovers it from `display_name` |
| Manual cancellation mid-training | Workflow signals `cancel_with_reason`; calls `CustomJob.cancel`; runs Step 9 with `status=cancelled` |
| Re-submit same `job_name` while a previous run is active | `WorkflowIDReusePolicy.REJECT_DUPLICATE` rejects at the client side |
| Eval score callback misses (e.g. dataset has no val split) | `eval_score.json` is written as `{"eval_score": null}`; retry-on-low-score branch is skipped |
| 0-step training (epochs=0) | Validator rejects: `training.epochs >= 1` |
| LoRA target_modules unknown for base model | Resolver in `training/utils/tokenization.py` falls back to common attention modules; warns |
| HF Hub upload partial | HF SDK resumable; on retry it diffs and continues |
| GCS eventual consistency on freshly-written manifest | `save_artifacts` reads with `if_generation_match` to ensure the manifest is the one it just wrote |
| Region without A100 capacity | Operator pre-checks via `gcloud ai locations describe`; quota errors surface as `QuotaExceeded` |
| Regex-based parsing of model logs (e.g. for step count) | Regex `r"step\s+(\d+)"` — for a zero-length pattern `r""`, Python's `re.findall` returns `len(string)+1` matches (one between each character pair, plus head and tail). We never use zero-length patterns; this is documented because the template's log-parser must reject empty patterns at config-load time with `ValueError("empty regex pattern not allowed")`. |
| Empty `target_modules` list | Validator rejects: at least one entry required |
| Single-document dataset | Proceeds with `WARN`; tests cover this path explicitly |

---

## 19. Design Decisions and Rationale

### 19.1 Temporal vs. Cloud Workflows / Argo / step-functions equivalents

**Decision:** Temporal.
**Why:** Durable execution, first-class long-running activities with heartbeats, mature Python SDK, can target both Temporal Cloud (managed) and self-hosted clusters. Cloud Workflows lacks polling-with-heartbeat ergonomics for >1h Vertex jobs and ties us to GCP. Argo would require a GKE cluster just for the orchestrator.

### 19.2 Cloud Run Job vs. Cloud Run Service for the worker

**Decision:** Cloud Run **Job** (executed on a schedule or one-shot by Cloud Scheduler), with the option to switch to Cloud Run **Service** (long-poll) when workflow throughput is high enough to justify keeping a worker warm.
**Why:** Most fine-tuning shops run < 10 workflows/day. Cloud Run Job's pay-per-execution model is cheaper at this scale. The worker code is the same in both modes — the only difference is the entrypoint loop (one-shot vs. forever).

### 19.3 Vertex AI CustomJob vs. GKE training pods

**Decision:** Vertex AI CustomJob.
**Why:** Managed, no cluster to maintain, built-in TensorBoard integration, native Spot support, billed per second. GKE adds operational overhead unjustified by the workload pattern (bursty, GPU-heavy, short-lived).

### 19.4 Hydra/YAML vs. CLI args / Pydantic

**Decision:** Hydra for `training/`, Pydantic for the **workflow request contract**, JSON Schema for static validation of job YAMLs.
**Why:** Hydra excels at training-time hyperparameter composition (`+peft=lora`, `+model=llama3_1_8b`). Pydantic gives strong typing at the workflow boundary. JSON Schema gives PR-time validation in CI without importing Python.

### 19.5 LoRA / QLoRA as defaults; full fine-tune is opt-in

**Decision:** `peft.type=lora` is the default.
**Why:** LoRA SFT delivers 90%+ of full-fine-tune quality at 1–5% of the cost and trains on a single A100. Full fine-tune is reserved for >70B base models or research workloads.

### 19.6 Cloud Build vs. GitHub Actions for container builds

**Decision:** Cloud Build for the training/serving/worker images, GitHub Actions for orchestration (test, lint, terraform plan/apply, smoke tests).
**Why:** Cloud Build runs in-project, has direct access to Artifact Registry with Workload Identity, supports private VPC if enabled, and offers high-CPU build pools. GitHub Actions remains the user-facing CI control plane.

### 19.7 Spot fallback inside the workflow vs. inside Vertex AI

**Decision:** Workflow-level fallback (Temporal re-submits with `STANDARD`).
**Why:** Vertex AI's `restart_job_on_worker_restart=true` handles within-job preemption transparently, but does **not** convert Spot → On-Demand. The workflow owns the budget decision: re-try Spot up to `max_preemptions`, then escalate if `fallback_on_demand=true`.

### 19.8 Why a separate serving template

**Decision:** This template **prepares** a serving image but does not deploy it. Deployment belongs in the GKE or Cloud Run template.
**Why:** Serving is a long-lived, stateful concern with its own SLOs, scaling, and security boundary. Mixing it into a fine-tuning template would dilute both.

### 19.9 Model Registry as canonical source of truth

**Decision:** Every successful training run is published to Vertex AI Model Registry with a new version. Aliases (`default`, `staging`, `production`) are moved via `scripts/promote_model.py`.
**Why:** Single mutable name with immutable versions enables A/B testing and instant rollback. HF Hub is optional and external.

### 19.10 No regex with zero-length patterns

**Decision:** Forbidden by config validator.
**Why:** Python `re.findall(r"", "abc")` returns `["", "", "", ""]` (length 4) — a footgun for anyone parsing logs. The validator rejects empty patterns with a clear message.

---

## 20. Open Questions / Risks

1. **Temporal Cloud vs. self-hosted** — the template supports both; the operator chooses via `var.temporal_cloud`. The default in `terraform/stage/terraform.tfvars` is `true` to avoid running a Temporal cluster in stage. The DevOps Engineer must document which path is chosen in `README.md`.
2. **HF gated model access** — Llama 3.1 requires HF Hub access approval. The operator must request access at fork time and populate `hf-token` before running any Llama job.
3. **70B-class jobs** — require either A100 80GB ×4 or H100 ×8; quotas must be requested in advance. The default machine type targets 8B; the operator overrides per-job.
4. **Region availability** — Spot A100 availability is region-dependent. The template defaults to `us-central1`, where capacity is generally good. For other regions, the fallback path should be tested in stage.

---

## 21. Quality Self-Check (Architect, before delivery)

- [x] Every workflow step has input/output contracts, retry policies, heartbeat behaviour, and edge cases.
- [x] Vertex AI CustomJob spec is concrete (machine_type, scheduling, env, output prefix).
- [x] GCS bucket layout is enumerated with IAM, lifecycle, versioning.
- [x] IAM bindings are per-bucket, per-SA, with rationale.
- [x] Secret Manager secrets are listed with consumers.
- [x] Cloud Build YAMLs are illustrated with substitutions and SAs.
- [x] GitHub Actions versions match `template-standards.md` (no `@v3` or older for the listed actions).
- [x] Terraform version pinned `>= 1.6`.
- [x] Pre-deploy resource conflict check in CI documented.
- [x] Bulk delete uses `BulkWriter` (Firestore) and batched `delete_blobs` (GCS) — deprecated `Batch()` is explicitly forbidden.
- [x] Concrete JSON / YAML examples for the workflow input, output, failure payload, job config, JSONL formats.
- [x] Edge cases for data prep (empty, single, all-empty, duplicates, tokenizer failures) documented.
- [x] Spot VM fallback flow documented (decision diagram).
- [x] Cost table and security threat model included.
- [x] No application code written (no `.py`, `.tf`, `.yaml` files produced — those are the implementing teams').

— *End of ARCHITECTURE.md* —
