# gcp-vertex-ai-jobs-template

**Production-grade template for automated supervised fine-tuning of open-source LLMs on Google Cloud Platform.**

> Fork this repository once per domain or customer. Drop raw documents into a GCS bucket, submit a job config, and a self-hosted Temporal workflow handles data prep → image build → Vertex AI training (CPU by default, GPU + Spot when `use_gpu=true`) → checkpointing → model registry → optional Hugging Face Hub push → **serving image build & push** (deployment is out of scope) — durably and cost-efficiently.

---

## Table of contents

- [What this template gives you](#what-this-template-gives-you)
- [Architecture diagrams](#architecture-diagrams)
  - [1. High-level architecture](#1-high-level-architecture)
  - [2. Temporal fine-tune workflow](#2-temporal-fine-tune-workflow)
  - [3. CI/CD and training flow](#3-cicd-and-training-flow)
- [Prerequisites](#prerequisites)
- [Quickstart](#quickstart)
- [Configuration reference](#configuration-reference)
- [Makefile targets](#makefile-targets)
- [Cost estimates](#cost-estimates)
- [Troubleshooting](#troubleshooting)
- [Live URLs](#live-urls)
- [Repository layout](#repository-layout)
- [Authoritative spec](#authoritative-spec)

---

## What this template gives you

| Capability | Implementation |
|---|---|
| **Durable orchestration** | **Self-hosted Temporal** on Cloud Run + Cloud SQL for PostgreSQL — no Temporal Cloud dependency |
| **Managed training** | Vertex AI `CustomJob` — **CPU-only `n1-standard-8` by default**, switch to A100 with `use_gpu: true` |
| **Spot fallback (GPU only)** | When `use_gpu=true`, Spot VMs are used first with automatic On-Demand fallback |
| **Multi-model — Unsloth quantized** | `unsloth/Meta-Llama-3.1-8B-Instruct`, `unsloth/mistral-7b-instruct-v0.3`, `unsloth/Qwen2.5-7B-Instruct` — **no HF gating**, ~2× faster QLoRA |
| **Cost-optimised** | LoRA / QLoRA defaults, Spot for GPU, GCS lifecycle rules |
| **Reproducible** | Hydra configs, digest-pinned images, deterministic data splits |
| **Training metrics → Vertex AI TensorBoard** | Per-run scalars (`train/loss`, `eval/loss`, `eval/score`) and text samples; no separate metrics serving endpoint |
| **Secure by default** | Workload Identity, Secret Manager, private Artifact Registry, **VPC + private IP Cloud SQL (mandatory)** |
| **Two-env deploy** | `terraform/stage/` and `terraform/prod/` with shared modules |
| **Serving image build (no deploy)** | Step 8 builds + pushes the vLLM serving image to Artifact Registry; **deploying it is out of scope** — see `gcp-cloudrun-template` or `gcp-clouddeploy-gke-template` |

---

## Architecture diagrams

### 1. High-level architecture

```mermaid
flowchart LR
    subgraph GitHub["GitHub"]
        REPO[("gcp-vertex-ai-jobs-template<br/>repo")]
        GHA["GitHub Actions<br/>(lint, test, plan, apply)"]
    end

    subgraph GCP["GCP — dfh-stage-id / dfh-prod-id"]
        subgraph Ingress["Ingress & Triggers"]
            SCHED["Cloud Scheduler"]
            EVENTARC["Eventarc<br/>(GCS object created)"]
        end

        subgraph Worker["Temporal Worker"]
            CRJOB["Cloud Run Job<br/>(temporal/worker.py)"]
        end

        subgraph Orchestration["Orchestration (self-hosted)"]
            TEMPORAL_SVC["Cloud Run Service<br/>temporal-server<br/>(min-instances=1, VPC-only)"]
            CSQL[("Cloud SQL Postgres<br/>(temporal + temporal_visibility)")]
            TEMPORAL_SVC <--> CSQL
        end

        subgraph CI["Build"]
            CB["Cloud Build<br/>(train/serving/worker images)"]
            AR["Artifact Registry<br/>(training, serving)"]
        end

        subgraph Training["Training"]
            VAI["Vertex AI CustomJob<br/>(Spot → fallback STANDARD)"]
            TB["Vertex AI TensorBoard"]
            MR["Vertex AI Model Registry"]
        end

        subgraph Data["Data & Artifacts"]
            GCS_RAW[("GCS raw-documents")]
            GCS_PROC[("GCS processed-datasets")]
            GCS_CKPT[("GCS checkpoints")]
            GCS_FINAL[("GCS final-models")]
        end

        subgraph Security["Security & Config"]
            SM["Secret Manager<br/>(temporal-postgres-password,<br/>HF_TOKEN [optional], WANDB [optional])"]
            IAM["IAM<br/>(per-SA, per-bucket)"]
        end

        subgraph Obs["Observability"]
            LOG["Cloud Logging"]
            MON["Cloud Monitoring<br/>(alerts + dashboards)"]
        end
    end

    HF[("Hugging Face Hub<br/>(optional adapter push)")]
    WANDB[("Weights & Biases<br/>(optional)")]

    REPO --> GHA
    GHA -->|terraform apply| GCP
    GHA -->|trigger| CB
    CB -->|push| AR
    SCHED --> CRJOB
    EVENTARC --> CRJOB
    GCS_RAW -.->|object created| EVENTARC
    CRJOB <-->|gRPC over VPC<br/>temporal-server.&lt;env&gt;.internal:7233| TEMPORAL_SVC
    CRJOB -->|submit CustomJob| VAI
    VAI -->|pulls image| AR
    VAI -->|read| GCS_PROC
    VAI -->|read/write| GCS_CKPT
    VAI -->|write adapter| GCS_FINAL
    VAI -->|metrics| TB
    VAI -.->|logs/metrics| WANDB
    GCS_FINAL --> MR
    GCS_FINAL -.->|push| HF
    SM -->|secret refs| VAI
    SM --> CRJOB
    VAI --> LOG
    CRJOB --> LOG
    LOG --> MON
```

### 2. Temporal fine-tune workflow

```mermaid
flowchart TD
    START([FineTuneRequest received]) --> S1
    S1["Step 1: data_validation_and_prep<br/>raw → processed JSONL<br/>heartbeat 30s"]
    S1 -->|DataValidationError| FAIL
    S1 --> S2["Step 2: build_training_image_if_needed<br/>Cloud Build → AR"]
    S2 -->|BuildFailed| FAIL
    S2 --> S3
    S3["Step 3: submit_training<br/>strategy = SPOT (default)"]
    S3 -->|ResourceExhausted<br/>+ fallback_on_demand| S3B["Re-submit STANDARD"]
    S3 -->|QuotaExceeded| FAIL
    S3 --> S4
    S3B --> S4["Step 4: monitor_training<br/>poll + heartbeat every 30s"]
    S4 -->|preempted &<br/>count &lt; max_preemptions| S3
    S4 -->|preempted &<br/>count ≥ max & fallback| S3B
    S4 -->|FAILED| FAIL
    S4 -->|SUCCEEDED| S5
    S5["Step 5: save_artifacts<br/>verify + optional merge LoRA"]
    S5 -->|ArtifactsMissing| FAIL
    S5 --> EVAL{"eval_score &lt;<br/>min_eval_score?<br/>(retry_round &lt; 1)"}
    EVAL -->|yes — retry with<br/>lr × retry_lr_multiplier| S3
    EVAL -->|no| S6
    S6["Step 6: register_model<br/>Vertex AI Model Registry"]
    S6 -->|skip if<br/>register_in_vertex=false| S7
    S6 --> S7
    S7["Step 7: upload_to_hf_hub<br/>(optional)"]
    S7 -->|skip if<br/>upload_hf_hub=false| S8
    S7 -->|HFAuthError| FAIL
    S7 --> S8
    S8["Step 8: prepare_serving_artifacts<br/>Cloud Build → serving image"]
    S8 -->|skip if<br/>prepare_serving_image=false| OK
    S8 --> OK
    OK["Step 9: notify (success)<br/>Slack / email"]
    FAIL["Step 9: notify (failure)<br/>Slack / email — never blocks"]
    OK --> END([WorkflowResult: succeeded])
    FAIL --> END_F([WorkflowResult: failed])

    classDef stepNode fill:#1f6feb,color:#fff,stroke:#0a4ea2
    classDef failNode fill:#cf222e,color:#fff,stroke:#82071e
    classDef okNode fill:#1a7f37,color:#fff,stroke:#0d5a26
    class S1,S2,S3,S3B,S4,S5,S6,S7,S8 stepNode
    class FAIL,END_F failNode
    class OK,END okNode
```

### 3. CI/CD and training flow

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant GH as GitHub
    participant GHA as GitHub Actions
    participant TF as Terraform
    participant CB as Cloud Build
    participant AR as Artifact Registry
    participant CRJ as Cloud Run Job<br/>(worker)
    participant TEM as Temporal
    participant VAI as Vertex AI
    participant GCS as GCS
    participant MR as Model Registry
    participant HF as Hugging Face

    Dev->>GH: push to `stage`
    GH->>GHA: trigger deploy-stage.yml
    GHA->>GHA: lint + test + validate-configs
    GHA->>TF: terraform apply (stage)
    TF-->>GHA: outputs (SAs, buckets, AR repos, …)
    GHA->>CB: trigger worker-image build
    CB->>AR: push worker:SHA
    CB->>CRJ: update Cloud Run Job image
    GHA->>CRJ: execute smoke job

    Note over Dev,CRJ: Day-2: submit a fine-tune

    Dev->>GH: edit configs/jobs/foo.yaml
    Dev->>CRJ: make submit CONFIG=…
    CRJ->>TEM: start FineTuneWorkflow
    TEM->>CRJ: dispatch activities
    CRJ->>GCS: data prep (raw → processed)
    CRJ->>CB: build training image (if needed)
    CB->>AR: push train:SHA
    CRJ->>VAI: CustomJob.create(image, SPOT)
    VAI->>GCS: read processed
    VAI->>GCS: checkpoint every N steps
    VAI-->>CRJ: state updates (polled)
    VAI->>GCS: write final adapter
    CRJ->>MR: register model version
    CRJ-->>HF: push adapter (optional)
    CRJ->>CB: build serving image
    CB->>AR: push serving:SHA
    CRJ-->>Dev: Slack/email notification

    Dev->>GH: merge `stage` → `main`
    GH->>GHA: trigger deploy-prod.yml (manual approval)
    GHA->>TF: terraform apply (prod)
    Note right of GHA: prod pipeline mirrors stage<br/>with Workload Identity + tighter alerts
```

---

## Prerequisites

### Local tools

| Tool | Min version | Purpose |
|---|---|---|
| `gcloud` | latest | GCP CLI; auth & ad-hoc inspection |
| `terraform` | **>= 1.6** | Required for variables in import blocks |
| `python` | 3.11 | Match the container Python |
| `uv` or `pip-tools` | latest | Dependency management |
| `docker` | 24+ | Local image testing only |
| `make` | any | Driver for all commands |
| `pre-commit` | latest | Local quality gate |

### GCP setup

1. Three GCP projects (already provisioned by the platform team):
   - `dfh-stage-id` (staging)
   - `dfh-prod-id` (production)
   - `dfh-ops-id` (DNS zone host)
2. Two Terraform state buckets:
   - `gs://dfh-stage-tfstate`
   - `gs://dfh-prod-tfstate`
3. Cloud DNS zone `demo-devops-for-hire-com` in `dfh-ops-id`.
4. **Hugging Face is optional.** Default base models are Unsloth quantized (ungated, Apache 2.0). You only need an HF account + User Access Token if you set `artifacts.upload_hf_hub: true` to push your fine-tuned adapter back to HF Hub. Tokens: https://huggingface.co/settings/tokens.
5. **GPU quota is optional for the CPU-only smoke path.** For real GPU training, request **NVIDIA A100 40GB** quota in `us-central1` (≥ 1 GPU); 70B-class jobs need A100 80GB ×4 or H100 ×8.

### GitHub secrets

For each environment, configure either Workload Identity Federation (**preferred**) or a service-account JSON key:

| Secret | Purpose |
|---|---|
| `GCP_WIF_PROVIDER` | OIDC provider resource name (preferred) |
| `GCP_WIF_SERVICE_ACCOUNT` | `tf-deploy-<env>@<project>.iam.gserviceaccount.com` |
| `GCP_STAGE_SA_KEY` / `GCP_PROD_SA_KEY` | JSON key (legacy fallback) |

---

## Quickstart

> The very first deployment requires a one-time bootstrap. Subsequent deployments happen automatically on push to `stage` / `main`.

### 0. One-time bootstrap (per environment)

```bash
# Authenticate locally
gcloud auth login
gcloud auth application-default login

# Enable APIs, ensure state bucket exists
make bootstrap ENV=stage

# Populate secrets (interactive)
./scripts/bootstrap_secrets.sh stage
#  → prompts for HF_TOKEN, optionally WANDB_API_KEY, Temporal credentials, Slack webhook
```

### 1. Apply infrastructure

```bash
make init  ENV=stage
make plan  ENV=stage          # review the diff
make apply ENV=stage          # creates SAs, buckets, AR repos, Cloud Run Job, …
```

Or push to the `stage` branch and let GitHub Actions do the same (recommended).

### 2. Upload raw documents

```bash
gsutil -m cp -r ./examples/invoices/*.pdf \
  gs://dfh-stage-id-raw-documents/invoices-llama3-8b-lora-v1/
```

### 3. Submit a fine-tune workflow

```bash
# CPU-only smoke test (template default — recommended first run):
make submit ENV=stage CONFIG=configs/jobs/smoke_llama3_8b_cpu.yaml

# Real GPU run (requires A100 quota in us-central1):
make submit ENV=stage CONFIG=configs/jobs/invoices_llama3_8b_lora.yaml
```

This prints a workflow ID like `ft-9a3b1c7e-20260512-141022`.

> **CPU vs. GPU.** The default `infrastructure.use_gpu` is **`false`** — runs on `n1-standard-8`. This exercises the entire workflow plumbing (data prep → submit → monitor → save → register → serving-image build) without consuming GPU quota. It is **not** suitable for training models >1B params for real use — set `use_gpu: true` for that.

### 4. Follow along

```bash
make tail ENV=stage WORKFLOW=ft-9a3b1c7e-20260512-141022
# Also:
#  - Temporal Web UI: VPC-only — see "Worker can't reach Temporal" troubleshooting
#  - Vertex AI UI:    https://console.cloud.google.com/vertex-ai/training/custom-jobs
#  - TensorBoard:     https://console.cloud.google.com/vertex-ai/experiments/tensorboard-instances
```

### 5. Inspect outputs

```bash
gsutil ls gs://dfh-stage-id-final-models/invoices-llama3-8b-lora-v1/
gcloud ai models list --region=us-central1 --filter="displayName:invoices-llama3-8b-lora"
```

### 6. Promote to prod

```bash
git checkout main
git merge stage
git push origin main
#  → triggers deploy-prod.yml (requires manual approval in GitHub Environments)
```

---

## Configuration reference

### Job config (`configs/jobs/*.yaml`)

Each job YAML is validated against `configs/schema.json` in CI. See `ARCHITECTURE.md §14.1` for the full schema with an example.

Key knobs you'll edit most often:

| Path | Default | When to change |
|---|---|---|
| `model.base_model_id` | `unsloth/Meta-Llama-3.1-8B-Instruct` | Switch to `unsloth/mistral-7b-instruct-v0.3` or `unsloth/Qwen2.5-7B-Instruct` |
| `peft.type` | `lora` | `qlora` for tighter memory; `none` for full fine-tune |
| `peft.lora_r` | `16` | `8` for tiny adapters, `32–64` for higher capacity |
| `training.epochs` | `3` | More epochs for small datasets; watch eval loss |
| `training.learning_rate` | `2e-4` (LoRA) | Lower for larger models or noisy data |
| `training.max_seq_length` | `4096` | Match your data's typical length; longer = more VRAM |
| `infrastructure.use_gpu` | `false` | **`true`** to run on A100 — required for any real (>1B param) training |
| `infrastructure.machine_type` | `n1-standard-8` (CPU) | `a2-highgpu-1g` (A100) when `use_gpu=true`; `g2-standard-12` (L4) for cheap QLoRA |
| `infrastructure.spot` | `true` | Ignored when `use_gpu=false`; `false` for time-critical GPU jobs |
| `infrastructure.max_preemptions` | `2` | Higher in spot-heavy regions; `0` to never tolerate preemption |
| `evaluation.min_eval_score` | `null` | Set to gate auto-retry |

### Environment variables (`.env.example`)

Copy `.env.example` → `.env` for local development:

```bash
cp .env.example .env
# Edit values; this file is gitignored
```

### Hydra training configs

Layered defaults from `training/conf/`:

```bash
# Run a debug training locally (smoke test, 50 steps)
python -m training.train +experiment=debug

# Override on the CLI
python -m training.train model=mistral_7b peft=qlora training.epochs=1
```

---

## Makefile targets

```text
make help                              # this list
make bootstrap ENV=stage               # enable APIs + create state bucket
make init      ENV=stage               # terraform init
make plan      ENV=stage               # terraform plan
make apply     ENV=stage               # terraform apply
make destroy   ENV=stage               # terraform destroy
make lint                              # ruff + black + tf fmt + hadolint + yamllint
make test                              # pytest (temporal, data_prep, training utils)
make build-worker  ENV=stage           # Cloud Build the worker image
make build-train   ENV=stage           # Cloud Build the training image
make build-serving ENV=stage ADAPTER_URI=...
make submit CONFIG=configs/jobs/foo.yaml ENV=stage
make tail   WORKFLOW=ft-... ENV=stage
make cancel WORKFLOW=ft-... ENV=stage
make promote MODEL=foo VERSION=3       # alias new version → "production"
make cost-report START=2026-04-01 END=2026-04-30
make smoke-serving IMAGE=us-central1-docker.pkg.dev/.../serving:tag
```

---

## Cost estimates

Order-of-magnitude per fine-tune campaign (USD). See `ARCHITECTURE.md §16.3` for inputs.

| Workload | Machine | Duration | Spot? | Cost |
|---|---|---|---|---|
| 8B LoRA SFT (50k rows) | 1× A100 40GB | 2 h | yes | $3–6 |
| 8B LoRA SFT (50k rows) | 1× A100 40GB | 2 h | no | $8–12 |
| 8B QLoRA SFT (200k rows) | 1× L4 24GB | 6 h | yes | $2–4 |
| 70B LoRA SFT (50k rows) | 4× A100 80GB | 12 h | yes | $80–120 |
| 70B Full fine-tune | 8× H100 | 48 h | yes | $1,500–2,500 |

Steady-state monthly storage (5 active jobs, 30-day retention defaults): **< $20**.

Cloud Build, Secret Manager, Cloud Logging, and Cloud Run Job worker: typically **< $10/month combined** at low/medium workflow throughput.

Run `make cost-report START=… END=…` against your BigQuery billing export to get exact numbers.

---

## Troubleshooting

### "Workflow stuck in step 3 / `ResourceExhausted`"

Spot capacity unavailable in the region. Choices:
1. Set `infrastructure.fallback_on_demand: true` (the default) and re-submit; the workflow will switch to STANDARD on next attempt.
2. Change `infrastructure.region` to a region with spot A100 capacity (e.g. `us-west1`, `europe-west4`).
3. Switch to a smaller machine (e.g. `g2-standard-12` with `NVIDIA_L4`).

### "Vertex CustomJob fails with OOM at step N"

- Lower `training.per_device_batch_size` (often `2` works where `4` doesn't).
- Increase `training.gradient_accumulation_steps` to preserve effective batch size.
- Enable `training.gradient_checkpointing: true` (the default).
- Switch `peft.type` from `lora` to `qlora`.
- Lower `training.max_seq_length`.

### "HF Hub: 401 Unauthorized"

You will only ever see this on the **upload-back** path (`artifacts.upload_hf_hub=true`) — Unsloth base model downloads are ungated. Fix:

- `hf-token` secret is missing or expired. Recreate:
  ```bash
  printf 'hf_xxx_your_new_token' | gcloud secrets versions add hf-token \
    --project=dfh-stage-id --data-file=-
  ```
- Confirm the HF user owns (or has write access to) the target `hf_repo_id`.

### "Workflow says `succeeded` but model isn't in Model Registry"

`artifacts.register_in_vertex` is `false` in your job YAML. Set it to `true` and re-run, or register manually via `scripts/promote_model.py`.

### "GitHub Actions: `Error: terraform plan exited with code 1`"

Check `Pre-deploy resource conflict check` step output. If a resource exists outside Terraform state, either:
- `terraform import` it into state, or
- delete the orphan resource via `gcloud`, then re-run.

### "Worker can't reach Temporal"

Temporal is self-hosted on Cloud Run + Cloud SQL. Check, in order:

1. The Cloud Run **Service** `temporal-server-<env>` is healthy: `gcloud run services describe temporal-server-<env> --region=us-central1 --project=dfh-<env>-id`.
2. The Cloud SQL Postgres instance is `RUNNABLE` and has private IP only.
3. The worker Cloud Run **Job** has a VPC connector attached (`vpc_access.connector`) and `TEMPORAL_ADDRESS=temporal-server.<env>.internal:7233` in its env.
4. The private DNS zone resolves `temporal-server.<env>.internal` from inside the VPC — test from a Cloud Shell with VPC peering: `nslookup temporal-server.stage.internal`.

### "Eval score is `null`"

Your dataset has no validation rows (e.g. `validation_split` was effectively 0). Increase `data.validation_split` or supply pre-split data.

---

## Live URLs

| Environment | DNS (reserved for serving) | Branch | GCP Project | Terraform state |
|---|---|---|---|---|
| **Staging** | `gcp-vertex-ai-jobs-template.stage.demo.devops-for-hire.com` | `stage` | `dfh-stage-id` | `gs://dfh-stage-tfstate/gcp-vertex-ai-jobs-template/state` |
| **Production** | `gcp-vertex-ai-jobs-template.demo.devops-for-hire.com` | `main` | `dfh-prod-id` | `gs://dfh-prod-tfstate/gcp-vertex-ai-jobs-template/state` |

> Note: this template **builds and pushes** a serving image to Artifact Registry but **does not deploy** any serving endpoint. The DNS names above are reserved for whatever downstream serving deployment consumes the image (see `gcp-cloudrun-template` or `gcp-clouddeploy-gke-template`).
>
> Training metrics live in **Vertex AI TensorBoard** for the env's project (linked above). No separate metrics endpoint is deployed.

Other operational URLs:

- **Vertex AI Training jobs (stage):** https://console.cloud.google.com/vertex-ai/training/custom-jobs?project=dfh-stage-id
- **Model Registry (stage):** https://console.cloud.google.com/vertex-ai/models?project=dfh-stage-id
- **TensorBoard (stage):** https://console.cloud.google.com/vertex-ai/experiments/tensorboard-instances?project=dfh-stage-id
- **Cloud Build (stage):** https://console.cloud.google.com/cloud-build/builds?project=dfh-stage-id
- **Cloud Logging (stage):** https://console.cloud.google.com/logs/query?project=dfh-stage-id
- **Temporal Web UI:** the self-hosted server's Web UI is **not exposed publicly** (VPC-only). To reach it, either deploy `temporalio/web` as a sidecar Service in the same VPC or `gcloud run services proxy temporal-server-<env> --port=8233` and tunnel to `localhost:8233`.

---

## Repository layout

```
.
├── terraform/                 # IaC (modules/, stage/, prod/)
├── temporal/                  # Workflow + activities + worker entrypoint
├── training/                  # train.py + Hydra configs + callbacks
├── data_prep/                 # Raw docs → instruction JSONL pipeline
├── serving/                   # vLLM Dockerfile + helpers
├── cloudbuild/                # Cloud Build YAML files
├── docker/                    # Training/worker/data-prep Dockerfiles
├── configs/                   # Job YAMLs + JSON Schema
├── scripts/                   # Utility scripts (submit, cost report, etc.)
├── docs/                      # Long-form documentation
├── .github/workflows/         # GitHub Actions (lint, test, deploy, …)
├── ARCHITECTURE.md            # Full technical spec (authoritative)
├── README.md                  # You are here
├── Makefile
└── .env.example
```

For a complete file-by-file description, see `ARCHITECTURE.md §2`.

---

## Authoritative spec

This README is a getting-started guide. The **authoritative technical specification** is `ARCHITECTURE.md` — refer to it for:

- Full workflow input/output contracts
- All retry policies, timeouts, and heartbeat intervals
- Complete IAM bindings and Secret Manager inventory
- Cloud Build YAML structure
- GCS bucket lifecycle rules
- Spot-VM fallback decision flowchart
- Design decisions and rationale

If something in this README conflicts with `ARCHITECTURE.md`, `ARCHITECTURE.md` wins. Open a PR to fix the README.

---

## License

Apache 2.0 — see `LICENSE`.

## Contributing

This is a template — fork it, don't PR domain-specific job configs back here. Architectural improvements (more PEFT methods, additional base models, better cost-reporting) are welcome.
