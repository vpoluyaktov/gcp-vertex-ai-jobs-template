# Cost Optimization

This guide covers the main cost drivers for the `gcp-vertex-ai-jobs-template` stack,
provides worked estimates, and lists concrete actions to reduce spend.

> **Pricing disclaimer:** All figures are approximate and based on GCP published rates
> as of 2026-Q2 for `us-central1`.  Run `scripts/cost_report.py` for actuals from your
> billing export.  Use the [GCP Pricing Calculator](https://cloud.google.com/products/calculator)
> to model different configurations before committing.

---

## 1. Cost Component Breakdown

### 1.1 Vertex AI Training (dominant cost)

| Configuration | Machine | Accelerator | Rate (approx.) | 3-hour job |
|---|---|---|---|---|
| **CPU — default template** | `n1-standard-8` | none | $0.38/hr | ~$1.15 |
| **GPU Spot (recommended)** | `a2-highgpu-1g` | A100 40 GB | ~$1.10/hr | ~$3.30 |
| **GPU On-demand (fallback)** | `a2-highgpu-1g` | A100 40 GB | ~$3.67/hr | ~$11.00 |
| GPU Spot — T4 (budget option) | `n1-standard-4` + T4 | T4 16 GB | ~$0.35/hr | ~$1.05 |

**Spot savings:** GPU Spot is typically 60–70% cheaper than On-demand.
With `fallback_on_demand: true` (the default), you pay Spot price most of the time
and absorb On-demand cost only on preemption fallback.

**LoRA vs full fine-tune cost comparison:**

| Approach | Trainable params | Typical job time (8B model) | Training cost |
|---|---|---|---|
| LoRA (r=16) | ~20M / 8B total | 2–4 h GPU | ~$3–7 |
| QLoRA (4-bit + r=16) | ~20M / 8B total | 3–6 h GPU | ~$5–11 |
| Full fine-tune (all params) | 8B | 12–24 h GPU | ~$20–50 |
| CPU-only (dev/template test) | any | hours–days | ~$1–4 (impractical >1B params) |

**Recommendation:** Use LoRA with `use_gpu: true` and `spot: true` for all production
training runs.  Reserve CPU-only mode for CI smoke tests and template validation.

---

### 1.2 Always-On Infrastructure (monthly, per environment)

| Component | Resource | Estimated monthly cost |
|---|---|---|
| **Cloud SQL** (Temporal DB) | `db-custom-2-7680` (2 vCPU, 7.5 GB) | ~$85–95 |
| **Cloud Run — Temporal Server** | `min-instances=1`, small container | ~$10–15 |
| **Cloud Run — Worker Jobs** | Pay per execution; ~seconds per dispatch | ~$0.50–2 |
| **Serverless VPC Connector** | Throughput-based; light RPC load | ~$5–10 |
| **Cloud Build** | First 120 min/day free; standard tier after | ~$0–10 |
| **Artifact Registry** | ~$0.10/GB/month for image storage | ~$2–8 |
| **Secret Manager** | $0.06 per 10K access operations | ~$0.50–2 |

**Cloud SQL is the largest fixed cost.**  A single `db-custom-2-7680` instance running
24/7 costs ~$85–95/month.  For development environments, consider:

- Scaling down to `db-g1-small` (~$25/month) — sufficient for low-concurrency workloads.
- Stopping the instance when not in use:
  ```bash
  gcloud sql instances patch temporal-<env> --activation-policy NEVER --project=<project>
  ```
- Sharing one staging Cloud SQL instance across multiple dev projects.

---

### 1.3 GCS Storage Costs

| Bucket | Default class | Lifecycle transition | Lifecycle delete | Est. cost (100 GB) |
|---|---|---|---|---|
| `<project>-raw-documents` | Standard | — | — | ~$2.00/month |
| `<project>-processed-datasets` | Standard → Nearline after 7 d | after 7 d | after 30 d | ~$0.50–1.00/month |
| `<project>-checkpoints` | Standard | — | after **14 d** | ~$0.50/month (transient) |
| `<project>-final-models` | Standard → Nearline after 30 d | after 30 d | after 365 d | ~$1.00–2.00/month |
| `<project>-build-artifacts` | Standard | — | — | ~$0.50/month |

GCS storage tiers (us-central1):

| Class | Cost/GB/month | Min storage duration | Best for |
|---|---|---|---|
| **Standard** | $0.020 | none | Hot data — active training inputs |
| **Nearline** | $0.010 | 30 days | Data accessed < once/month — models, processed datasets after training |
| **Coldline** | $0.004 | 90 days | Long-term archival (> 90-day retention) |

The lifecycle rules in `terraform/modules/gcs_buckets/` are pre-configured to
implement the transitions shown above.  Verify they are active:
```bash
gcloud storage buckets describe gs://<project>-checkpoints --format="value(lifecycle)"
```

---

## 2. Spot VM Savings Estimate

For a team running **10 training jobs/month** averaging 3 hours each on an A100:

| Scenario | Effective rate | Monthly training cost |
|---|---|---|
| All On-demand | $3.67/hr | **$110** |
| All Spot, no preemptions | $1.10/hr | **$33** |
| Spot + 20% preemptions → On-demand fallback | ~$1.60/hr (blended) | **$48** |

**Typical saving: 55–70% off the GPU line-item.**

Preemption rate in `us-central1` for A100s is typically 10–30% depending on time of day.
Configure `max_preemptions: 2` (the default) to allow up to 2 automatic checkpoint-resume
retries before the workflow falls back to On-demand.  The `restart_job_on_worker_restart`
flag ensures Vertex AI resumes from the latest checkpoint on each preemption.

---

## 3. Cost Reduction Recommendations

### 3.1 Training job configuration

- **Always use LoRA/QLoRA** for domain adaptation.  Full fine-tuning an 8B model
  is rarely justified on financial extraction tasks — LoRA at r=16 achieves near-equivalent
  performance at a fraction of the time and cost.
- **Reduce `num_train_epochs`** for initial experiments.  Start with 1–2 epochs,
  evaluate `eval_score`, then extend only if metrics are still improving.
- **Try smaller base models first.**  Mistral 7B and Qwen 2.5 7B often converge faster
  than Llama 3.1 8B for structured extraction tasks.  Try them before scaling up.
- **Set `max_seq_length` to the minimum that covers your data.**  Shorter sequences
  allow larger effective batch sizes and faster training.  Check p95 token length
  in the data prep `manifest.json` before defaulting to 4096.
- **Enable `use_gpu: true` + `spot: true`** and let `fallback_on_demand: true` handle
  preemptions automatically.  Do not leave the default `use_gpu: false` in production.

### 3.2 Always-on infrastructure

- **Scale down Cloud SQL in staging.**  Use `db-g1-small` in `terraform/stage/terraform.tfvars`
  instead of `db-custom-2-7680`.  Training jobs don't talk to Cloud SQL; only the
  Temporal server does, and only for workflow history (low throughput).
- **Allow Temporal Server to scale to zero in staging** by setting `min_instance_count = 0`
  in the `temporal_server` module variables for staging.  Worker jobs will cold-start
  (~10 s extra), which is acceptable in non-production.
- **Use a single staging environment** per team, not per developer.  Multiple stage
  environments multiply the fixed Cloud SQL cost linearly.

### 3.3 GCS storage

- **Lifecycle rules are on by default** (Terraform provisions them).  Do not disable them.
- **Delete processed datasets immediately after a successful training run** if raw documents
  are still in GCS and the pipeline is idempotent (it is — SHA-256 based).  Don't wait
  for the 30-day lifecycle trigger.
- **Final models auto-transition to Nearline after 30 days** per the lifecycle rule.
  If you are done with a model version, delete it explicitly to avoid accumulating
  adapter artifacts over the 365-day default retention.

### 3.4 Cloud Build

- The first 120 build-minutes/day are free on the default pool.
  Use `paths:` filters in `.github/workflows/` to skip image rebuilds when only
  docs or config files change.
- Training image builds (CUDA + Unsloth + PyTorch) are slow and expensive.  Tag images
  with the source digest and skip rebuilds when `training/requirements.txt` and
  `docker/Dockerfile.training` haven't changed.

### 3.5 Monitoring cost anomalies

Add a billing budget alert so runaway training jobs don't surprise you at month-end:
```bash
gcloud billing budgets create \
  --billing-account=<BILLING_ACCOUNT_ID> \
  --display-name="vertex-finetune-stage-alert" \
  --budget-amount=200USD \
  --threshold-rules=percent=0.5,percent=0.9,percent=1.0
```

The `finetune_cost_estimate_usd` Cloud Monitoring metric (emitted per workflow run via
`WorkflowResult.cost_estimate_usd`) provides per-job attribution for chargeback.

---

## 4. Example Monthly Bill Estimate

**Active staging environment, 10 training jobs/month (LoRA 8B, 3 h each, A100 Spot):**

| Line item | Details | Cost |
|---|---|---|
| Vertex AI GPU training (Spot + 20% fallback) | 10 jobs × 3 h × $1.60/hr | ~$48 |
| Cloud SQL `db-custom-2-7680` | always-on | ~$90 |
| Cloud Run — Temporal Server | min-instances=1 | ~$12 |
| Cloud Run — Worker Jobs | 10 jobs × ~$0.01 | ~$0 |
| GCS storage (raw docs 50 GB, models 100 GB) | Standard + Nearline mix | ~$5 |
| Artifact Registry (10 GB images) | 4 image types | ~$1 |
| Cloud Build (image builds, < 120 min/day) | within free tier | ~$0 |
| Misc (Logging, Monitoring, Secrets) | | ~$3 |
| **Total** | | **~$159/month** |

**Production environment (same load, all On-demand fallback):** ~$240/month.

Switching staging GPU training from On-demand to Spot + reducing Cloud SQL tier
to `db-g1-small` saves approximately **$125/month** on this profile.
