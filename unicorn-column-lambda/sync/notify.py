"""notify-failure: email the operations list (via an SNS topic) when a sync fails.

Only failures are sent - never Queued / Skipped / Synced. Called from:
  - packed_column_sync.sync_columns    a column failed validation / mapping / queueing
  - canvas_handler (run-level)         auth, IP allowlist, Benchling unavailable
  - canvas_handler.report_column_result the on-prem UNICORN import/edit failed
Disabled when FAILURE_TOPIC_ARN is unset. A notification problem is logged and swallowed -
it must never fail the sync itself.
"""
import logging
from datetime import datetime, timezone

import boto3

import config

log = logging.getLogger(__name__)


def notify_failure(stage: str, message: str, registry_id: str = None, column_name: str = None,
                   column_id: str = None, error_kind: str = None, user: str = None,
                   entry_id: str = None) -> None:
    if not config.FAILURE_TOPIC_ARN:
        return
    label = " ".join(x for x in (registry_id, column_name) if x) or column_id or "sync run"
    lines = [
        "A Packed Column could not be synced to UNICORN.",
        "",
        f"Column:     {label}",
        f"Failed at:  {stage}" + (f" ({error_kind})" if error_kind else ""),
        f"Error:      {message}",
    ]
    if user:
        lines.append(f"Started by: {user}")
    if column_id:
        lines.append(f"Entity ID:  {column_id}")
    if entry_id:
        lines.append(f"Entry:      {entry_id}")
    lines += [f"Time (UTC): {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}", "",
              "Details: the Sync results on the App Canvas, and the audit log in CloudWatch."]
    try:
        boto3.client("sns").publish(
            TopicArn=config.FAILURE_TOPIC_ARN,
            Subject=f"UNICORN sync failed: {label}"[:100],   # SNS subject limit
            Message="\n".join(lines),
        )
    except Exception:
        log.exception("Failure notification could not be sent for %s", label)
