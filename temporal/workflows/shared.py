"""Shared Pydantic models, result dataclasses, and exceptions for FineTuneWorkflow.

All models here are the authoritative workflow contract (§3.2/§3.3 of ARCHITECTURE.md).
Both the Temporal client (submit_job.py) and the worker import from this module.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PeftType(str, Enum):
    LORA = "lora"
    QLORA = "qlora"
    NONE = "none"


class DataFormat(str, Enum):
    CHAT = "chat"
    INSTRUCT = "instruct"


# ---------------------------------------------------------------------------
# FineTuneRequest — nested sub-models (§3.2)
# ---------------------------------------------------------------------------


class ModelConfig(BaseModel):
    base_model_id: str = "unsloth/Meta-Llama-3.1-8B-Instruct"
    revision: Optional[str] = "main"


class PeftConfig(BaseModel):
    type: PeftType = PeftType.LORA
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    target_modules: Optional[List[str]] = None
    bias: str = "none"
    merge_after_training: bool = False


class DataConfig(BaseModel):
    raw_uri: str
    processed_uri: Optional[str] = None
    format: DataFormat = DataFormat.CHAT
    train_split: float = 0.95
    validation_split: float = 0.05


class TrainingConfig(BaseModel):
    epochs: int = 3
    per_device_batch_size: int = 4
    gradient_accumulation_steps: int = 4
    learning_rate: float = 2e-4
    warmup_ratio: float = 0.03
    max_seq_length: int = 4096
    bf16: bool = True
    gradient_checkpointing: bool = True


class InfraConfig(BaseModel):
    use_gpu: bool = False
    machine_type: str = "n1-standard-8"
    accelerator_type: Optional[str] = None
    accelerator_count: int = 0
    spot: bool = True
    fallback_on_demand: bool = True
    max_preemptions: int = 2
    region: str = "us-central1"


class ArtifactConfig(BaseModel):
    output_uri: str
    checkpoint_uri: str
    upload_hf_hub: bool = False
    hf_repo_id: Optional[str] = None
    hf_private: bool = True
    register_in_vertex: bool = True
    prepare_serving_image: bool = True


class EvaluationConfig(BaseModel):
    min_eval_score: Optional[float] = None
    retry_on_low_score: bool = False
    retry_lr_multiplier: float = 0.5


class NotificationConfig(BaseModel):
    slack_webhook_secret: Optional[str] = None
    email_to: Optional[str] = None


class FineTuneRequest(BaseModel):
    """Top-level workflow input.  Clients serialise this to JSON and pass it as
    the Temporal workflow argument."""

    job_name: str = Field(..., max_length=60, pattern=r"^[a-z0-9-]+$")
    config_uri: str
    model: ModelConfig = Field(default_factory=ModelConfig)
    peft: PeftConfig = Field(default_factory=PeftConfig)
    data: DataConfig
    training: TrainingConfig = Field(default_factory=TrainingConfig)
    infrastructure: InfraConfig = Field(default_factory=InfraConfig)
    artifacts: ArtifactConfig
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    # resolved by build_training_image_if_needed before submit_training
    image_uri: Optional[str] = None


# ---------------------------------------------------------------------------
# Activity result models
# ---------------------------------------------------------------------------


class DataPrepResult(BaseModel):
    processed_uri: str
    train_rows: int
    val_rows: int
    sample_uri: str


class BuildResult(BaseModel):
    image_uri: str
    build_id: str
    cached: bool = False


class SubmitResult(BaseModel):
    vertex_job_id: str
    scheduling_strategy: str  # "SPOT" or "STANDARD"


class MonitorResult(BaseModel):
    final_state: str
    final_step: int
    train_loss: Optional[float] = None
    eval_loss: Optional[float] = None
    eval_score: Optional[float] = None
    preemption_count: int = 0
    preempted: bool = False
    wall_clock_seconds: int = 0


class SaveResult(BaseModel):
    adapter_uri: str
    merged_uri: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)


class RegisterResult(BaseModel):
    model_resource_name: str
    version_id: str
    aliases: List[str] = Field(default_factory=list)


class HFResult(BaseModel):
    repo_url: str
    commit_sha: str


class ServingResult(BaseModel):
    image_uri: str


class WorkflowResult(BaseModel):
    """Top-level workflow output (§3.3)."""

    status: str  # "succeeded" | "failed" | "cancelled"
    workflow_id: str
    vertex_job_id: Optional[str] = None
    model: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None
    cost_estimate_usd: Optional[float] = None
    preemption_count: int = 0
    fallback_to_on_demand: bool = False
    serving_image: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    # present only on failure
    failure_step: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Progress payload written by the Temporal query handler
# ---------------------------------------------------------------------------


class WorkflowProgress(BaseModel):
    current_step: str
    vertex_state: str
    step_count: int
    preemptions: int


# ---------------------------------------------------------------------------
# Custom exceptions
# Non-retryable types must match the string names in temporal/policies.py
# ---------------------------------------------------------------------------


class DataValidationError(Exception):
    """Input data is invalid or empty.  Non-retryable."""


class SpotUnavailable(Exception):
    """Spot capacity rejected and fallback_on_demand=false."""


class QuotaExceeded(Exception):
    """GPU quota exceeded for the requested region.  Non-retryable."""


class AmbiguousVertexJob(Exception):
    """Dedup listing returned >1 active job for the same display_name.  Non-retryable."""


class ArtifactsMissing(Exception):
    """Expected adapter files not found under output_uri.  Non-retryable."""


class VersionLimitReached(Exception):
    """Vertex AI Model Registry version cap (100) reached.  Non-retryable."""


class HFAuthError(Exception):
    """Hugging Face token is invalid.  Non-retryable."""


class VertexJobFailed(Exception):
    """Training job completed with JOB_STATE_FAILED (not a preemption)."""


class BuildFailed(Exception):
    """Cloud Build step OOM or otherwise failed."""
