"""
Entry point. Accepted event shapes:

1. Benchling webhook over HTTP (Lambda Function URL / API Gateway): {"headers", "body", ...}
   Signature verified, then canvas interactions are handed to an async self-invoke so the
   webhook is acknowledged immediately.
2. Async self-invoke:            {"_webhook": <benchling webhook body>}
3. Pre-parsed webhook (as in the App Canvas framework): {"message": {...}, "app": {...}}
4. Manual/test invoke:          {"action": "sync", "column_ids": ["bfi_..."]}
5. On-prem completion callback: {"action": "report", "canvas_id": ..., "column_id": ...,
   "result": "Synced"|"Failed", ...} - see canvas_handler.report_column_result. Invoked directly
   (aws lambda invoke), not via a webhook, by watch_service.ps1 once UNICORN import/edit finishes.
"""
import base64
import json
import logging
import os

import boto3
from benchling_sdk.apps.helpers.webhook_helpers import verify

import config
from auth_utils.authentication import initialize_benchling
from canvas_handler.app_canvas_handler import (handle_canvas_created, handle_canvas_initialized,
                                               handle_canvas_interaction, report_column_result)
from sync.benchling_reader import BenchlingReader
from sync.models import SyncError, WebhookError
from sync.packed_column_sync import sync_columns

logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

CANVAS_HANDLERS = {
    "v2.canvas.initialized": handle_canvas_initialized,
    "v2-beta.canvas.created": handle_canvas_created,
    "v2.canvas.created": handle_canvas_created,
    "v2.canvas.userInteracted": handle_canvas_interaction,
}


def _response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def lambda_handler(event, context):
    try:
        # "action" is checked first and is an unambiguous discriminator - a direct invoke (sync,
        # report) - so it can never collide with a Benchling webhook shape, which never sets it.
        # Checking this after "message" in event previously misrouted {"action": "report", ...}
        # into _dispatch_webhook, because a report's own "message" (str) collided with the
        # webhook shape's "message" (dict) and crashed on body.get("message").get("type").
        if event.get("action") == "sync":
            return _response(200, _run_batch(event.get("column_ids") or [],
                                             {"type": "manual", "user": event.get("user", "manual")}))
        if event.get("action") == "report":
            return _response(200, _report(event))
        if "headers" in event and "body" in event:
            return _handle_http(event, context)
        if "_webhook" in event:
            _dispatch_webhook(event["_webhook"])
            return _response(200, {"status": "processed"})
        if "message" in event:
            _dispatch_webhook(event)
            return _response(200, {"status": "processed"})
        raise WebhookError(f"Unrecognised event shape: {list(event)[:10]}")
    except WebhookError as e:
        log.warning("%s", e)
        return _response(400, {"error": str(e)})


def _handle_http(event, context):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if not config.SKIP_WEBHOOK_VERIFICATION:
        if not config.APP_DEFINITION_ID:
            raise WebhookError("APP_DEFINITION_ID not configured; cannot verify webhook")
        try:
            verify(config.APP_DEFINITION_ID, raw, headers)
        except Exception as e:
            raise WebhookError(f"Webhook signature verification failed: {e}") from e

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise WebhookError(f"Invalid webhook payload: {e}") from e

    msg_type = (body.get("message") or {}).get("type")
    if msg_type == "v2.canvas.userInteracted" and config.ASYNC_WEBHOOK and context is not None:
        boto3.client("lambda").invoke(FunctionName=context.invoked_function_arn, InvocationType="Event",
                                      Payload=json.dumps({"_webhook": body}).encode("utf-8"))
        return _response(200, {"status": "accepted"})

    _dispatch_webhook(body)
    return _response(200, {"status": "processed"})


def _dispatch_webhook(body):
    msg_type = (body.get("message") or {}).get("type")
    handler = CANVAS_HANDLERS.get(msg_type)
    if handler is None:
        log.info("Ignoring webhook type %s", msg_type)
        return
    handler(body, initialize_benchling())


def _report(body):
    try:
        report_column_result(body, initialize_benchling())
        return {"status": "reported"}
    except Exception as e:
        log.exception("Completion report failed for canvas %s", body.get("canvas_id"))
        return {"error": f"{type(e).__name__}: {e}"}


def _run_batch(column_ids, trigger):
    try:
        outcomes = sync_columns(BenchlingReader(initialize_benchling()), column_ids, trigger)
    except SyncError as e:
        log.exception("Sync run failed")
        return {"error": f"{e.kind}: {e}"}
    summary = {}
    for o in outcomes:
        summary[o.result] = summary.get(o.result, 0) + 1
    return {"summary": summary, "outcomes": [o.__dict__ for o in outcomes]}
