"""Notification activity (Step 9 of FineTuneWorkflow).

Sends a Slack webhook and/or email notification on workflow completion or failure.
Implements §3.4 Step 9 of ARCHITECTURE.md.

This is a best-effort activity: after NOTIFY_RETRY exhaustion the exception is
swallowed by the *workflow* so that a notification failure never marks the
workflow itself as failed.

Supports:
  - Slack via an HTTP POST to a webhook URL stored in Secret Manager
  - Email via SendGrid (if sendgrid-api-key secret is present)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import urllib.request
import urllib.error

from google.cloud import secretmanager
from temporalio import activity

from temporal.workflows.shared import NotificationConfig, WorkflowResult

logger = logging.getLogger(__name__)


def _fetch_secret(secret_resource_name: str) -> str:
    """Fetch a secret version value from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(name=secret_resource_name)
    return response.payload.data.decode("utf-8").strip()


def _post_webhook(url: str, payload: Dict[str, Any]) -> None:
    """HTTP POST a JSON payload to a webhook URL."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        status = resp.getcode()
        if status not in (200, 201, 202, 204):
            raise RuntimeError(f"Webhook returned HTTP {status}")


def _build_slack_message(result: WorkflowResult) -> Dict[str, Any]:
    """Build a Slack Block Kit message from the workflow result."""
    icon = ":white_check_mark:" if result.status == "succeeded" else ":x:"
    title = f"{icon} Fine-tune job *{result.workflow_id}*: {result.status.upper()}"

    fields = [{"type": "mrkdwn", "text": f"*Status:* {result.status}"}]

    if result.vertex_job_id:
        fields.append(
            {"type": "mrkdwn", "text": f"*Vertex Job:* `{result.vertex_job_id}`"}
        )

    if result.metrics:
        loss_str = (
            f"{result.metrics.get('train_loss_final', 'n/a'):.4f}"
            if isinstance(result.metrics.get("train_loss_final"), float)
            else "n/a"
        )
        fields.append({"type": "mrkdwn", "text": f"*Train loss:* {loss_str}"})
        if result.metrics.get("eval_score") is not None:
            fields.append(
                {
                    "type": "mrkdwn",
                    "text": f"*Eval score:* {result.metrics['eval_score']:.4f}",
                }
            )

    if result.cost_estimate_usd is not None:
        fields.append(
            {
                "type": "mrkdwn",
                "text": f"*Est. cost:* ${result.cost_estimate_usd:.2f} USD",
            }
        )

    if result.failure_step:
        fields.append(
            {
                "type": "mrkdwn",
                "text": f"*Failed at:* {result.failure_step} — {result.message}",
            }
        )

    return {
        "text": title,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": title}},
            {"type": "section", "fields": fields},
        ],
    }


@activity.defn
async def send_notification(
    result: WorkflowResult,
    notifications: NotificationConfig,
) -> None:
    """Send Slack and/or email notifications for the workflow outcome.

    Retry policy: NOTIFY_RETRY (3 attempts, then caller swallows the exception).
    Best-effort: failures here must NOT propagate to mark the workflow failed.
    """
    if not notifications.slack_webhook_secret and not notifications.email_to:
        logger.debug("No notification channels configured — skipping.")
        return

    errors: list[str] = []

    # --- Slack ---
    if notifications.slack_webhook_secret:
        try:
            webhook_url = _fetch_secret(notifications.slack_webhook_secret)
            payload = _build_slack_message(result)
            _post_webhook(webhook_url, payload)
            logger.info(
                "Slack notification sent for workflow '%s'", result.workflow_id
            )
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)
            errors.append(f"slack: {exc}")

    # --- Email (SendGrid) ---
    if notifications.email_to:
        try:
            _send_email_notification(result, notifications.email_to)
        except Exception as exc:
            logger.warning("Email notification failed: %s", exc)
            errors.append(f"email: {exc}")

    if errors:
        raise RuntimeError(
            f"Notification errors for workflow '{result.workflow_id}': {'; '.join(errors)}"
        )


def _send_email_notification(result: WorkflowResult, to_email: str) -> None:
    """Send an email via SendGrid.  Skipped if SENDGRID_API_KEY is absent."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        # Try Secret Manager as fallback
        try:
            project = os.environ.get("GCP_PROJECT_ID", "")
            if project:
                secret_name = f"projects/{project}/secrets/sendgrid-api-key/versions/latest"
                api_key = _fetch_secret(secret_name)
        except Exception:
            pass

    if not api_key:
        logger.debug("SENDGRID_API_KEY not found — skipping email notification")
        return

    subject = (
        f"Fine-tune {result.workflow_id}: {result.status.upper()}"
    )
    body = (
        f"Workflow: {result.workflow_id}\n"
        f"Status: {result.status}\n"
        f"Vertex Job: {result.vertex_job_id or 'n/a'}\n"
        f"Metrics: {json.dumps(result.metrics or {}, indent=2)}\n"
    )
    if result.failure_step:
        body += f"\nFailed at step: {result.failure_step}\nError: {result.message}\n"

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": "noreply@devops-for-hire.com"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.getcode() not in (200, 202):
            raise RuntimeError(f"SendGrid returned HTTP {resp.getcode()}")
    logger.info("Email notification sent to '%s'", to_email)
