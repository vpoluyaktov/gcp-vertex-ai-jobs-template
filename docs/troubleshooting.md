# Troubleshooting

This guide covers the most common operational issues encountered when running
`gcp-vertex-ai-jobs-template`.  Issues are grouped by component.

---

## 1. Temporal Worker Can't Connect (Port 443 / TLS)

### Symptom

```
temporalio.exceptions.RPCError: failed to connect to Temporal server at
temporal-server.stage.internal:7233: connection refused
```
or
```
grpc._channel._InactiveRpcError: StatusCode.UNAVAILABLE
deadline exceeded
```

### Cause

The most common cause is the wrong port.  Cloud Run terminates TLS at its edge
on **port 443** — the container's internal port 7233 is not reachable from other
services.  Workers must connect to `temporal-server.<env>.internal:443` with TLS.

### Fix

1. **Check `TEMPORAL_ADDRESS` in your environment:**
   ```bash
   # Correct:
   TEMPORAL_ADDRESS=temporal-server.stage.internal:443

   # Wrong — will be refused:
   TEMPORAL_ADDRESS=temporal-server.stage.internal:7233
   ```

2. **Verify `TLSConfig()` is used in the Python client:**
   ```python
   from temporalio.client import Client, TLSConfig
   client = await Client.connect(address, namespace=ns, tls=TLSConfig())
   ```
   Both `temporal/worker/worker.py` and `temporal/client/trigger_workflow.py` already
   do this.  If you've forked the code, check that TLS wasn't accidentally removed.

3. **Confirm the worker is running inside the VPC:**
   The Temporal server's Cloud Run Service uses INTERNAL ingress — it is not
   reachable from the public internet.  Workers must run inside the VPC (i.e. as
   Cloud Run Jobs with the VPC connector attached) or via the Cloud Build Private
   Worker Pool.  Local `python -m temporal.worker.worker` will time out unless
   you have VPN access to the VPC.

4. **Check Temporal Server logs:**
   ```bash
   gcloud run services logs read temporal-server-stage \
     --project=<project> --region=us-central1 --limit=50
   ```

---

## 2. Cloud SQL Private IP Not Reachable

### Symptom

```
temporalio/auto-setup container exits with:
  dial tcp <private-ip>:5432: connect: connection refused
```
or the Temporal Server Cloud Run Service fails health checks immediately after deploy.

### Cause

The Temporal Server reads `POSTGRES_SEEDS` from its environment, which is set to the
Cloud SQL private IP by Terraform.  If Cloud SQL isn't running, or the VPC peering
isn't set up correctly, the connection fails.

### Fix

1. **Confirm Cloud SQL instance is running:**
   ```bash
   gcloud sql instances describe temporal-stage \
     --project=<project> --format="value(state)"
   # Expected: RUNNABLE
   ```
   If it shows `STOPPED`, start it:
   ```bash
   gcloud sql instances patch temporal-stage \
     --activation-policy ALWAYS --project=<project>
   ```

2. **Verify the private IP is allocated:**
   ```bash
   gcloud sql instances describe temporal-stage \
     --project=<project> --format="value(ipAddresses)"
   # Should include an entry with type=PRIVATE
   ```
   If there is no PRIVATE IP, the `private_network` VPC peering wasn't applied.
   Re-run `terraform apply` for the `cloud_sql` module.

3. **Check VPC Service Networking peering:**
   ```bash
   gcloud services vpc-peerings list \
     --network=vpc-stage --project=<project> --service=servicenetworking.googleapis.com
   ```
   If empty, the `servicenetworking.googleapis.com` peering connection hasn't been
   created.  This is Terraform-managed; re-run `terraform apply` for the `networking`
   module first, then `cloud_sql`.

4. **Verify the Cloud Run Service is attached to the VPC connector:**
   ```bash
   gcloud run services describe temporal-server-stage \
     --project=<project> --region=us-central1 \
     --format="value(spec.template.metadata.annotations)"
   # Look for: run.googleapis.com/vpc-access-connector=vpcconn-stage
   ```

---

## 3. Vertex AI Job Quota Exceeded

### Symptom

Temporal workflow fails at Step 3 (`submit_training_job`) with:
```
QuotaExceeded: Quota 'NVIDIA_A100_GPUS' exceeded. Limit: 0.0 in region us-central1.
```
or the workflow raises `SpotUnavailable` (when `fallback_on_demand: false`).

### Fix

1. **Check current GPU quota:**
   ```bash
   gcloud compute regions describe us-central1 --project=<project> \
     --format="table(quotas.metric, quotas.limit, quotas.usage)" | grep -i gpu
   ```

2. **Request a quota increase** via the GCP Console:
   Navigation: IAM & Admin → Quotas → filter for `NVIDIA_A100_GPUS` → Edit Quotas.
   Initial quota for new projects is 0; request 1–4 A100s for `us-central1`.
   Approval typically takes 1–3 business days.

3. **Use a different region** while waiting for quota:
   ```yaml
   # In your job YAML:
   infrastructure:
     region: us-east1   # or europe-west4
   ```
   Different regions have different A100 availability.  `us-east1` and `europe-west4`
   often have faster approval for initial quotas.

4. **Use CPU training for development** (no quota required):
   ```yaml
   infrastructure:
     use_gpu: false
     machine_type: n1-standard-8
   ```
   This is the default template configuration and works without any GPU quota.

5. **Use T4 GPUs** as a cheaper alternative that typically has higher quota:
   ```yaml
   infrastructure:
     use_gpu: true
     machine_type: n1-standard-4
     accelerator_type: NVIDIA_TESLA_T4
     accelerator_count: 1
   ```

---

## 4. Unsloth Installation Errors

### Symptom

`training/requirements.txt` installation fails with:
```
ERROR: Could not find a version that satisfies the requirement unsloth[colab-new]>=2024.11.0
```
or training container build fails with CUDA version mismatch.

### Fix

1. **The training container requires CUDA 12.**  The `docker/Dockerfile.training.gpu`
   base image (`nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04`) provides this.
   Do not try to install `unsloth` in a CPU-only environment via `pip install` —
   it requires CUDA at install time.

2. **For local development on a non-GPU machine**, use the CPU Dockerfile
   (`docker/Dockerfile.training`) which installs `unsloth[colab-new]` in CPU mode:
   ```bash
   docker build -f docker/Dockerfile.training -t train-cpu .
   docker run train-cpu python training/train.py --config configs/training-job-llama3-8b.yaml
   ```

3. **If pip fails on `bitsandbytes`**, this is usually a CUDA/GCC version mismatch.
   The Dockerfile pins `bitsandbytes>=0.44.0,<1` which supports CUDA 12.4.
   Ensure the base image hasn't been upgraded without updating the pin:
   ```bash
   # In Dockerfile.training.gpu, verify the base image:
   FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
   ```

4. **For Vertex AI CustomJob failures** during the Unsloth import, check the training
   container logs in Cloud Logging:
   ```bash
   gcloud logging read \
     'resource.type="aiplatform.googleapis.com/CustomJob"
      labels."ml.googleapis.com/job_id"="<job_id>"
      severity>=ERROR' \
     --project=<project> --limit=30
   ```

---

## 5. GCS Permission Denied

### Symptom

```
google.api_core.exceptions.Forbidden: 403 GET https://storage.googleapis.com/...:
  Caller does not have storage.objects.get access to the Google Cloud Storage object.
```

### Cause

The service account running the job lacks the required IAM role on the bucket.

### Fix

1. **Identify which service account is running the job:**
   - Temporal worker → `worker-<env>@<project>.iam.gserviceaccount.com`
   - Vertex AI training job → `training-<env>@<project>.iam.gserviceaccount.com`
   - Cloud Build → `cloudbuild-<env>@<project>.iam.gserviceaccount.com`

2. **Check current IAM bindings on the bucket:**
   ```bash
   gcloud storage buckets get-iam-policy gs://<project>-<bucket-name>
   ```

3. **Re-apply Terraform** to restore the expected bindings (Terraform manages
   all bucket IAM via `google_storage_bucket_iam_member`):
   ```bash
   terraform -chdir=terraform/stage apply -target=module.iam
   ```

4. **Common mis-configuration:** the IAM module grants bucket-scoped
   `roles/storage.objectAdmin` (not project-level).  If someone manually added
   a project-level IAM binding and Terraform removed it during apply, the binding
   disappears.  All bucket access must go through `terraform/modules/iam/main.tf`.

5. **Verify ADC for local scripts:**
   When running `scripts/` locally, the user's ADC must have access to the buckets.
   If using a personal account:
   ```bash
   gcloud auth application-default login
   gcloud projects add-iam-policy-binding <project> \
     --member="user:<your-email>" \
     --role="roles/storage.objectAdmin"
   ```

---

## 6. Out of Memory (OOM) on CPU Training

### Symptom

Training container exits with:
```
RuntimeError: [Errno 12] Cannot allocate memory
```
or the Vertex AI CustomJob fails with status `JOB_STATE_FAILED` and no error code
(process killed by OOM killer).

### Cause

CPU training of 7B+ parameter models requires 28–56 GB of RAM just to load the
model weights, before any gradient accumulation or batch processing.

### Fix

1. **Use GPU training for 7B+ models** (`use_gpu: true`).  This is the intended
   path for production.  CPU training is documented as suitable only for smoke tests
   and template development (`use_gpu: false` default is intentional for CI).

2. **For genuine CPU testing**, increase the machine memory:
   ```yaml
   infrastructure:
     use_gpu: false
     machine_type: n1-highmem-16  # 16 vCPU, 104 GB RAM
   ```

3. **Reduce model size** for CPU tests.  Use a smaller model (e.g. Qwen 2.5 1.5B
   for smoke tests) rather than Llama 3.1 8B:
   ```yaml
   model:
     base_model_id: unsloth/Qwen2.5-1.5B-Instruct
   ```
   Training code emits a prominent warning when `use_gpu=false` with a >1B parameter
   model — this is by design.

4. **Reduce `per_device_train_batch_size`** and `max_seq_length`:
   ```yaml
   training:
     per_device_train_batch_size: 1
     max_seq_length: 512
   ```

5. **Enable gradient checkpointing** (already on by default in `training/config/default.yaml`).
   If you've overridden it, restore: `training.gradient_checkpointing: true`.

---

## 7. Additional Diagnostics

### View workflow progress

```bash
./scripts/check_job_status.sh --workflow-id finetune-<job-name>-<uuid8> --env stage
```

### Query workflow history via Temporal CLI

```bash
temporal workflow describe \
  --workflow-id finetune-<job-name>-<uuid8> \
  --address temporal-server.stage.internal:443 \
  --namespace default \
  --tls
```

### Tail Vertex AI training logs

```bash
gcloud ai custom-jobs stream-logs <JOB_ID> \
  --project=<project> --region=us-central1
```

### List running Cloud Run Jobs (workers)

```bash
gcloud run jobs executions list \
  --job=temporal-worker-stage \
  --project=<project> --region=us-central1
```
