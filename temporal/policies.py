"""Temporal RetryPolicy definitions for the FineTuneWorkflow.

Each activity selects the appropriate policy from this module.  Policies are
grouped by activity risk profile — see §3.6 of ARCHITECTURE.md.
"""

from datetime import timedelta

from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# §3.6 — canonical policy constants
# ---------------------------------------------------------------------------

#: General-purpose policy: 3 attempts, exponential to 5 min.
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
        "AmbiguousVertexJob",
    ],
)

#: Long-polling policy used by monitor_training.
#: maximum_attempts=0 means infinite — the *workflow* controls termination via
#: preemption counting and the 24-hour execution timeout.
LONG_POLL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,  # infinite
)

#: Data validation / prep step (Step 1) — same non-retryables as DEFAULT but
#: slightly longer back-off because data-prep jobs can be slow to start.
DATA_PREP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
    non_retryable_error_types=["DataValidationError"],
)

#: Artifact verification / merge (Step 5) — up to 5 retries to tolerate GCS
#: eventual consistency.
ARTIFACT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=5,
    non_retryable_error_types=["ArtifactsMissing"],
)

#: HF Hub (Step 7) — aggressive rate-limiting requires longer back-off.
HF_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=["HFAuthError"],
)

#: Notifications (Step 9) — best-effort; failures are swallowed after 3 tries.
NOTIFY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
)

#: Cloud Build trigger (Step 2) — 3 attempts, longer intervals.
BUILD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
    non_retryable_error_types=["BuildFailed"],
)
