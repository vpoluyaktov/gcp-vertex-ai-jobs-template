"""Shared pytest fixtures for the gcp-vertex-ai-jobs-template test suite.

All fixtures here are available to every test module without explicit imports.
"""

from __future__ import annotations

import pytest

from temporal.workflows.shared import (
    ArtifactConfig,
    DataConfig,
    EvaluationConfig,
    FineTuneRequest,
    InfraConfig,
)


# ---------------------------------------------------------------------------
# FineTuneRequest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_finetune_request_cpu() -> FineTuneRequest:
    """Minimal CPU-only FineTuneRequest (use_gpu=False, default machine)."""
    return FineTuneRequest(
        job_name="test-cpu-job",
        config_uri="gs://dfh-stage-id-configs/jobs/test_cpu.yaml",
        data=DataConfig(
            raw_uri="gs://dfh-stage-id-raw-documents/test/",
            processed_uri=None,
        ),
        artifacts=ArtifactConfig(
            output_uri="gs://dfh-stage-id-final-models/test-cpu-job/",
            checkpoint_uri="gs://dfh-stage-id-checkpoints/test-cpu-job/",
        ),
        infrastructure=InfraConfig(
            use_gpu=False,
            machine_type="n1-standard-8",
            spot=True,  # ignored when use_gpu=False per §4.3
        ),
    )


@pytest.fixture
def sample_finetune_request_gpu() -> FineTuneRequest:
    """GPU FineTuneRequest with Spot enabled (use_gpu=True, spot=True).

    machine_type is set explicitly to "a2-highgpu-1g" so _build_machine_spec
    returns the correct GPU spec per §4.1.b.
    """
    return FineTuneRequest(
        job_name="test-gpu-job",
        config_uri="gs://dfh-stage-id-configs/jobs/test_gpu.yaml",
        data=DataConfig(
            raw_uri="gs://dfh-stage-id-raw-documents/test/",
        ),
        artifacts=ArtifactConfig(
            output_uri="gs://dfh-stage-id-final-models/test-gpu-job/",
            checkpoint_uri="gs://dfh-stage-id-checkpoints/test-gpu-job/",
        ),
        infrastructure=InfraConfig(
            use_gpu=True,
            machine_type="a2-highgpu-1g",
            accelerator_type="NVIDIA_TESLA_A100",
            accelerator_count=1,
            spot=True,
            fallback_on_demand=True,
        ),
        evaluation=EvaluationConfig(
            min_eval_score=0.65,
            retry_on_low_score=True,
            retry_lr_multiplier=0.5,
        ),
    )


# ---------------------------------------------------------------------------
# JSONL row fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_chat_jsonl_rows() -> list[dict]:
    """Five valid chat-format JSONL rows per §5.2 / §5.1 stage 5."""
    system_content = "You are an accounts-payable assistant."
    rows = []
    for i in range(1, 6):
        rows.append(
            {
                "messages": [
                    {"role": "system", "content": system_content},
                    {
                        "role": "user",
                        "content": (
                            f"Extract vendor and total from invoice #{i}:\n"
                            f"ACME Corp\nInvoice #{i}\nTotal: ${i * 100}.00"
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": (
                            f'{{"vendor":"ACME Corp","total":{i * 100}.0}}'
                        ),
                    },
                ]
            }
        )
    return rows


@pytest.fixture
def sample_instruct_jsonl_rows() -> list[dict]:
    """Five valid instruct-format JSONL rows per §5.3 / §5.1 stage 5."""
    rows = []
    for i in range(1, 6):
        rows.append(
            {
                "instruction": "Extract invoice fields as JSON.",
                "input": (
                    f"ACME Corp\nInvoice #{i}\nDue: 2026-06-0{i}\nTotal: ${i * 500}.00"
                ),
                "output": (
                    f'{{"vendor":"ACME Corp","total":{i * 500}.0,"due_date":"2026-06-0{i}"}}'
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# GCS URI fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_gcs_prefix() -> str:
    """A fake GCS URI used to stand-in for a real processed_uri."""
    return "gs://test-bucket/processed-datasets/test-job"
