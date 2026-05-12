"""Tests for the FineTuneRequest Pydantic contracts (§3.2 of ARCHITECTURE.md).

Covers:
- Required-field validation for data.raw_uri and artifacts output/checkpoint URIs
- Empty-string base_model_id raises ValidationError (min_length=1 guard)
- CPU serialisation has no accelerator fields
- GPU + Spot serialisation includes all expected fields
- eval_threshold (min_eval_score) is clamped to [0.0, 1.0]
- retry_lr_multiplier must be > 0
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from temporal.workflows.shared import (
    ArtifactConfig,
    DataConfig,
    EvaluationConfig,
    FineTuneRequest,
    InfraConfig,
    ModelConfig,
    PeftType,
    DataFormat,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _minimal_request(**overrides) -> FineTuneRequest:
    """Build a valid minimal FineTuneRequest, applying keyword overrides."""
    defaults = dict(
        job_name="test-job",
        config_uri="gs://bucket/config.yaml",
        data=DataConfig(raw_uri="gs://bucket/raw/"),
        artifacts=ArtifactConfig(
            output_uri="gs://bucket/out/",
            checkpoint_uri="gs://bucket/ckpt/",
        ),
    )
    defaults.update(overrides)
    return FineTuneRequest(**defaults)


# ---------------------------------------------------------------------------
# Required-field tests
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_missing_data_raises(self):
        """Omitting data (which contains the required raw_uri) must raise."""
        with pytest.raises(ValidationError):
            FineTuneRequest(
                job_name="test-job",
                config_uri="gs://bucket/config.yaml",
                artifacts=ArtifactConfig(
                    output_uri="gs://bucket/out/",
                    checkpoint_uri="gs://bucket/ckpt/",
                ),
            )

    def test_missing_artifacts_raises(self):
        """Omitting artifacts (output_uri, checkpoint_uri) must raise."""
        with pytest.raises(ValidationError):
            FineTuneRequest(
                job_name="test-job",
                config_uri="gs://bucket/config.yaml",
                data=DataConfig(raw_uri="gs://bucket/raw/"),
            )

    def test_missing_output_uri_raises(self):
        """ArtifactConfig.output_uri is required."""
        with pytest.raises((ValidationError, TypeError)):
            ArtifactConfig(checkpoint_uri="gs://bucket/ckpt/")

    def test_missing_checkpoint_uri_raises(self):
        """ArtifactConfig.checkpoint_uri is required."""
        with pytest.raises((ValidationError, TypeError)):
            ArtifactConfig(output_uri="gs://bucket/out/")

    def test_missing_raw_uri_raises(self):
        """DataConfig.raw_uri is required."""
        with pytest.raises((ValidationError, TypeError)):
            DataConfig()


# ---------------------------------------------------------------------------
# job_name validation
# ---------------------------------------------------------------------------


class TestJobName:
    @pytest.mark.parametrize("valid_name", [
        "my-job",
        "a1",
        "invoices-llama3-8b-lora-v1",
        "x" * 60,  # exactly at the 60-char limit
    ])
    def test_valid_job_names(self, valid_name):
        req = _minimal_request(job_name=valid_name)
        assert req.job_name == valid_name

    @pytest.mark.parametrize("bad_name", [
        "",               # empty
        "x" * 61,         # too long (>60 chars)
        "UPPER_CASE",     # uppercase not in [a-z0-9-]
        "has_underscore", # underscore not in [a-z0-9-]
        "has space",      # space not in [a-z0-9-]
    ])
    def test_invalid_job_names_raise(self, bad_name):
        with pytest.raises(ValidationError):
            _minimal_request(job_name=bad_name)


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------


class TestModelConfig:
    def test_default_base_model(self, sample_finetune_request_cpu):
        assert sample_finetune_request_cpu.model.base_model_id == (
            "unsloth/Meta-Llama-3.1-8B-Instruct"
        )

    def test_empty_base_model_raises(self):
        """§3.2: base_model_id is required — empty string must be rejected."""
        with pytest.raises(ValidationError):
            ModelConfig(base_model_id="")

    def test_custom_base_model_accepted(self):
        m = ModelConfig(base_model_id="mistralai/Mistral-7B-Instruct-v0.3")
        assert m.base_model_id == "mistralai/Mistral-7B-Instruct-v0.3"


# ---------------------------------------------------------------------------
# CPU serialisation — no accelerator fields
# ---------------------------------------------------------------------------


class TestCpuSerialisation:
    def test_cpu_has_no_accelerator_fields_in_infra(self, sample_finetune_request_cpu):
        """use_gpu=False: the serialised InfraConfig must have accelerator_type=null
        and accelerator_count=0 (i.e. no GPU resource allocated)."""
        infra = sample_finetune_request_cpu.infrastructure
        assert infra.use_gpu is False
        assert infra.accelerator_type is None
        assert infra.accelerator_count == 0

    def test_cpu_machine_type_is_n1_standard_8(self, sample_finetune_request_cpu):
        assert sample_finetune_request_cpu.infrastructure.machine_type == "n1-standard-8"

    def test_cpu_serialises_to_dict(self, sample_finetune_request_cpu):
        d = sample_finetune_request_cpu.model_dump()
        infra = d["infrastructure"]
        assert infra["use_gpu"] is False
        assert infra["accelerator_type"] is None


# ---------------------------------------------------------------------------
# GPU + Spot serialisation
# ---------------------------------------------------------------------------


class TestGpuSerialisation:
    def test_gpu_spot_fields_set(self, sample_finetune_request_gpu):
        infra = sample_finetune_request_gpu.infrastructure
        assert infra.use_gpu is True
        assert infra.spot is True
        assert infra.machine_type == "a2-highgpu-1g"
        assert infra.accelerator_type == "NVIDIA_TESLA_A100"
        assert infra.accelerator_count == 1

    def test_gpu_serialises_to_dict(self, sample_finetune_request_gpu):
        d = sample_finetune_request_gpu.model_dump()
        infra = d["infrastructure"]
        assert infra["use_gpu"] is True
        assert infra["spot"] is True
        assert infra["machine_type"] == "a2-highgpu-1g"
        assert infra["accelerator_type"] == "NVIDIA_TESLA_A100"
        assert infra["accelerator_count"] == 1

    def test_gpu_json_round_trip(self, sample_finetune_request_gpu):
        """JSON serialisation/deserialisation preserves GPU config."""
        json_str = sample_finetune_request_gpu.model_dump_json()
        restored = FineTuneRequest.model_validate_json(json_str)
        assert restored.infrastructure.use_gpu is True
        assert restored.infrastructure.accelerator_type == "NVIDIA_TESLA_A100"


# ---------------------------------------------------------------------------
# EvaluationConfig constraints
# ---------------------------------------------------------------------------


class TestEvaluationConfig:
    @pytest.mark.parametrize("score", [0.0, 0.5, 1.0, None])
    def test_valid_eval_scores(self, score):
        cfg = EvaluationConfig(min_eval_score=score)
        assert cfg.min_eval_score == score

    @pytest.mark.parametrize("bad_score", [-0.001, 1.001, -1.0, 2.0])
    def test_out_of_range_eval_score_raises(self, bad_score):
        """min_eval_score must be in [0.0, 1.0] when set."""
        with pytest.raises(ValidationError):
            EvaluationConfig(min_eval_score=bad_score)

    @pytest.mark.parametrize("good_mult", [0.001, 0.5, 1.0, 2.0])
    def test_valid_retry_lr_multiplier(self, good_mult):
        cfg = EvaluationConfig(retry_lr_multiplier=good_mult)
        assert cfg.retry_lr_multiplier == good_mult

    @pytest.mark.parametrize("bad_mult", [0.0, -0.1, -1.0])
    def test_zero_or_negative_retry_lr_multiplier_raises(self, bad_mult):
        """retry_lr_multiplier must be > 0."""
        with pytest.raises(ValidationError):
            EvaluationConfig(retry_lr_multiplier=bad_mult)


# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


class TestEnums:
    def test_peft_type_values(self):
        assert PeftType.LORA.value == "lora"
        assert PeftType.QLORA.value == "qlora"
        assert PeftType.NONE.value == "none"

    def test_data_format_values(self):
        assert DataFormat.CHAT.value == "chat"
        assert DataFormat.INSTRUCT.value == "instruct"

    def test_invalid_peft_type_raises(self):
        from temporal.workflows.shared import PeftConfig
        with pytest.raises(ValidationError):
            PeftConfig(type="fft")

    def test_invalid_data_format_raises(self):
        with pytest.raises(ValidationError):
            DataConfig(raw_uri="gs://b/r/", format="unknown")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_train_split(self):
        cfg = DataConfig(raw_uri="gs://b/raw/")
        assert cfg.train_split == 0.95
        assert cfg.validation_split == 0.05

    def test_default_use_gpu_is_false(self):
        cfg = InfraConfig()
        assert cfg.use_gpu is False

    def test_default_region(self):
        cfg = InfraConfig()
        assert cfg.region == "us-central1"
