"""Tests for Vertex AI activities: machine_spec construction and Spot fallback.

Covers §4.1, §4.3, and §4.4 of ARCHITECTURE.md:
- CPU-only machine_spec contains ONLY machine_type (no accelerator fields)
- GPU machine_spec contains machine_type, accelerator_type, accelerator_count
- CPU jobs always use STANDARD scheduling (Spot is GPU-only)
- GPU+Spot jobs use SPOT scheduling
- ResourceExhausted on Spot triggers fallback to STANDARD (when fallback_on_demand=True)

All GCP API calls are mocked — no real Vertex AI calls are made.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, call, patch

import pytest
from google.api_core import exceptions as gapi_exc

from temporal.activities.vertex_ai_activity import (
    _build_machine_spec,
    _scheduling_strategy,
    submit_training_job,
)
from temporal.workflows.shared import (
    ArtifactConfig,
    DataConfig,
    FineTuneRequest,
    InfraConfig,
    SpotUnavailable,
)

_IMAGE_URI = "us-central1-docker.pkg.dev/dfh-stage-id/training/train:abc1234"
_PROJECT = "dfh-stage-id"
_LOCATION = "us-central1"
_DISPLAY_NAME = "ft-test-job-20260512-140000"
_VERTEX_JOB_ID = "projects/123/locations/us-central1/customJobs/999"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously for testing."""
    return asyncio.run(coro)


def _patch_submit(side_effect=None, return_value=_VERTEX_JOB_ID):
    """Patch _submit_job and _find_active_job with sensible defaults."""
    patches = [
        patch(
            "temporal.activities.vertex_ai_activity._find_active_job",
            return_value=None,
        ),
        patch(
            "temporal.activities.vertex_ai_activity._submit_job",
            side_effect=side_effect,
            return_value=None if side_effect else return_value,
        ),
    ]
    return patches


# ---------------------------------------------------------------------------
# _build_machine_spec — pure function tests (§4.1)
# ---------------------------------------------------------------------------


class TestBuildMachineSpec:
    def test_cpu_contains_only_machine_type(self, sample_finetune_request_cpu):
        """§4.1.a: CPU-only spec has exactly one key: machine_type."""
        spec = _build_machine_spec(sample_finetune_request_cpu)
        assert set(spec.keys()) == {"machine_type"}
        assert spec["machine_type"] == "n1-standard-8"

    def test_cpu_has_no_accelerator_type(self, sample_finetune_request_cpu):
        spec = _build_machine_spec(sample_finetune_request_cpu)
        assert "accelerator_type" not in spec

    def test_cpu_has_no_accelerator_count(self, sample_finetune_request_cpu):
        spec = _build_machine_spec(sample_finetune_request_cpu)
        assert "accelerator_count" not in spec

    def test_gpu_machine_type_is_a2_highgpu(self, sample_finetune_request_gpu):
        """§4.1.b: GPU spec uses a2-highgpu-1g."""
        spec = _build_machine_spec(sample_finetune_request_gpu)
        assert spec["machine_type"] == "a2-highgpu-1g"

    def test_gpu_accelerator_type_is_a100(self, sample_finetune_request_gpu):
        spec = _build_machine_spec(sample_finetune_request_gpu)
        assert spec["accelerator_type"] == "NVIDIA_TESLA_A100"

    def test_gpu_accelerator_count_is_1(self, sample_finetune_request_gpu):
        spec = _build_machine_spec(sample_finetune_request_gpu)
        assert spec["accelerator_count"] == 1

    def test_gpu_spec_contains_all_three_keys(self, sample_finetune_request_gpu):
        spec = _build_machine_spec(sample_finetune_request_gpu)
        assert set(spec.keys()) == {"machine_type", "accelerator_type", "accelerator_count"}

    def test_gpu_fallback_defaults_when_fields_empty(self):
        """_build_machine_spec uses hardcoded defaults when infra fields are falsy."""
        req = FineTuneRequest(
            job_name="test-defaults",
            config_uri="gs://b/c.yaml",
            data=DataConfig(raw_uri="gs://b/raw/"),
            artifacts=ArtifactConfig(
                output_uri="gs://b/out/",
                checkpoint_uri="gs://b/ckpt/",
            ),
            infrastructure=InfraConfig(
                use_gpu=True,
                machine_type="",          # falsy → defaults to "a2-highgpu-1g"
                accelerator_type=None,    # falsy → defaults to "NVIDIA_TESLA_A100"
                accelerator_count=0,      # falsy → defaults to 1
            ),
        )
        spec = _build_machine_spec(req)
        assert spec["machine_type"] == "a2-highgpu-1g"
        assert spec["accelerator_type"] == "NVIDIA_TESLA_A100"
        assert spec["accelerator_count"] == 1


# ---------------------------------------------------------------------------
# _scheduling_strategy — trivial helper
# ---------------------------------------------------------------------------


class TestSchedulingStrategy:
    def test_spot_true_returns_spot(self):
        assert _scheduling_strategy(True) == "SPOT"

    def test_spot_false_returns_standard(self):
        assert _scheduling_strategy(False) == "STANDARD"


# ---------------------------------------------------------------------------
# submit_training_job — scheduling strategy (§4.3 / §4.4)
# ---------------------------------------------------------------------------


class TestSubmitTrainingJobScheduling:
    def test_cpu_uses_standard_strategy(self, sample_finetune_request_cpu):
        """§4.4: CPU-only jobs always use STANDARD — no Spot discount available."""
        with patch(
            "temporal.activities.vertex_ai_activity._find_active_job", return_value=None
        ), patch(
            "temporal.activities.vertex_ai_activity._submit_job",
            return_value=_VERTEX_JOB_ID,
        ) as mock_submit:
            result = _run(
                submit_training_job(
                    sample_finetune_request_cpu,
                    _IMAGE_URI,
                    _DISPLAY_NAME,
                    _PROJECT,
                    _LOCATION,
                )
            )

        assert result.scheduling_strategy == "STANDARD"
        # _submit_job must have been called with use_spot=False
        _, submit_kwargs = mock_submit.call_args
        assert submit_kwargs["use_spot"] is False

    def test_cpu_spot_flag_is_ignored(self):
        """§4.3: Even when spot=True, CPU job must use STANDARD."""
        req = FineTuneRequest(
            job_name="cpu-spot-ignored",
            config_uri="gs://b/c.yaml",
            data=DataConfig(raw_uri="gs://b/raw/"),
            artifacts=ArtifactConfig(
                output_uri="gs://b/out/",
                checkpoint_uri="gs://b/ckpt/",
            ),
            infrastructure=InfraConfig(use_gpu=False, spot=True),
        )
        with patch(
            "temporal.activities.vertex_ai_activity._find_active_job", return_value=None
        ), patch(
            "temporal.activities.vertex_ai_activity._submit_job",
            return_value=_VERTEX_JOB_ID,
        ) as mock_submit:
            result = _run(
                submit_training_job(req, _IMAGE_URI, _DISPLAY_NAME, _PROJECT, _LOCATION)
            )

        assert result.scheduling_strategy == "STANDARD"
        _, submit_kwargs = mock_submit.call_args
        assert submit_kwargs["use_spot"] is False

    def test_gpu_spot_uses_spot_strategy(self, sample_finetune_request_gpu):
        """§4.3: GPU + spot=True → scheduling.strategy=SPOT."""
        with patch(
            "temporal.activities.vertex_ai_activity._find_active_job", return_value=None
        ), patch(
            "temporal.activities.vertex_ai_activity._submit_job",
            return_value=_VERTEX_JOB_ID,
        ) as mock_submit:
            result = _run(
                submit_training_job(
                    sample_finetune_request_gpu,
                    _IMAGE_URI,
                    _DISPLAY_NAME,
                    _PROJECT,
                    _LOCATION,
                )
            )

        assert result.scheduling_strategy == "SPOT"
        _, submit_kwargs = mock_submit.call_args
        assert submit_kwargs["use_spot"] is True


# ---------------------------------------------------------------------------
# submit_training_job — Spot → STANDARD fallback (§4.3)
# ---------------------------------------------------------------------------


class TestSpotFallback:
    def test_resource_exhausted_triggers_standard_fallback(
        self, sample_finetune_request_gpu
    ):
        """ResourceExhausted on Spot submission → re-submit with STANDARD (fallback_on_demand=True)."""
        with patch(
            "temporal.activities.vertex_ai_activity._find_active_job", return_value=None
        ), patch(
            "temporal.activities.vertex_ai_activity._submit_job"
        ) as mock_submit:
            mock_submit.side_effect = [
                gapi_exc.ResourceExhausted("No Spot capacity available"),
                _VERTEX_JOB_ID,
            ]
            result = _run(
                submit_training_job(
                    sample_finetune_request_gpu,
                    _IMAGE_URI,
                    _DISPLAY_NAME,
                    _PROJECT,
                    _LOCATION,
                )
            )

        assert result.scheduling_strategy == "STANDARD"
        assert result.vertex_job_id == _VERTEX_JOB_ID
        assert mock_submit.call_count == 2

        # Second call must use use_spot=False
        second_call_kwargs = mock_submit.call_args_list[1].kwargs
        assert second_call_kwargs["use_spot"] is False

    def test_resource_exhausted_no_fallback_raises_spot_unavailable(self):
        """When fallback_on_demand=False, SpotUnavailable must be raised."""
        req = FineTuneRequest(
            job_name="test-no-fallback",
            config_uri="gs://b/c.yaml",
            data=DataConfig(raw_uri="gs://b/raw/"),
            artifacts=ArtifactConfig(
                output_uri="gs://b/out/",
                checkpoint_uri="gs://b/ckpt/",
            ),
            infrastructure=InfraConfig(
                use_gpu=True,
                machine_type="a2-highgpu-1g",
                accelerator_type="NVIDIA_TESLA_A100",
                accelerator_count=1,
                spot=True,
                fallback_on_demand=False,
            ),
        )
        with patch(
            "temporal.activities.vertex_ai_activity._find_active_job", return_value=None
        ), patch(
            "temporal.activities.vertex_ai_activity._submit_job",
            side_effect=gapi_exc.ResourceExhausted("No Spot capacity"),
        ):
            with pytest.raises(SpotUnavailable):
                _run(
                    submit_training_job(
                        req, _IMAGE_URI, _DISPLAY_NAME, _PROJECT, _LOCATION
                    )
                )

    def test_fallback_submits_exactly_twice(self, sample_finetune_request_gpu):
        """Spot → STANDARD fallback must not retry more than once (single fallback)."""
        with patch(
            "temporal.activities.vertex_ai_activity._find_active_job", return_value=None
        ), patch(
            "temporal.activities.vertex_ai_activity._submit_job"
        ) as mock_submit:
            mock_submit.side_effect = [
                gapi_exc.ResourceExhausted("No Spot"),
                _VERTEX_JOB_ID,
            ]
            _run(
                submit_training_job(
                    sample_finetune_request_gpu,
                    _IMAGE_URI,
                    _DISPLAY_NAME,
                    _PROJECT,
                    _LOCATION,
                )
            )

        assert mock_submit.call_count == 2

    def test_idempotency_returns_existing_job(self, sample_finetune_request_gpu):
        """If an active job already exists, submit must return it without a new submission."""
        with patch(
            "temporal.activities.vertex_ai_activity._find_active_job",
            return_value=_VERTEX_JOB_ID,
        ), patch(
            "temporal.activities.vertex_ai_activity._submit_job"
        ) as mock_submit:
            result = _run(
                submit_training_job(
                    sample_finetune_request_gpu,
                    _IMAGE_URI,
                    _DISPLAY_NAME,
                    _PROJECT,
                    _LOCATION,
                )
            )

        assert result.vertex_job_id == _VERTEX_JOB_ID
        mock_submit.assert_not_called()
