"""
App Canvas in the notebook entry: pick Packed Columns (or leave empty = all columns
registered from this entry), click "Sync to UNICORN", see the per-column outcome.
"""
import logging

from benchling_sdk.models import (AppCanvasCreate, AppCanvasUpdate, AppSessionCreate, AppSessionMessageCreate,
                                  AppSessionMessageStyle, AppSessionUpdate, AppSessionUpdateStatus, ButtonUiBlock,
                                  ButtonUiBlockType, MarkdownUiBlock, MarkdownUiBlockType,
                                  SearchInputMultiValueUiBlock, SearchInputMultiValueUiBlockType,
                                  SearchInputUiBlockItemType)

import config
from sync.benchling_reader import BenchlingReader
from sync.models import SyncError
from sync.packed_column_sync import sync_columns

log = logging.getLogger(__name__)

INTRO = ("### Sync Packed Columns to UNICORN\n"
         "Select the Packed Columns to send, or leave the field empty to send every Packed Column "
         "registered in this entry. Each Active column is queued for UNICORN: the Column Type is "
         "imported if it does not exist yet, otherwise its run parameters are updated.")

_ICON = {"Queued": "✅", "Skipped": "⏭️", "Failure": "❌"}


def build_blocks(selected_ids=None, results_markdown=None, enabled=True):
    blocks = [
        MarkdownUiBlock(id="intro", type=MarkdownUiBlockType.MARKDOWN, value=INTRO),
        SearchInputMultiValueUiBlock(
            id=config.CANVAS_COLUMNS_INPUT_ID,
            type=SearchInputMultiValueUiBlockType.SEARCH_INPUT_MULTIVALUE,
            item_type=SearchInputUiBlockItemType.CUSTOM_ENTITY,
            schema_id=config.PACKED_COLUMN_SCHEMA_ID,
            label="Packed Columns",
            value=selected_ids or None,
            enabled=enabled,
        ),
        ButtonUiBlock(id=config.CANVAS_SYNC_BUTTON_ID, type=ButtonUiBlockType.BUTTON,
                      text="Sync to UNICORN" if enabled else "Syncing…", enabled=enabled),
    ]
    if results_markdown:
        blocks.append(MarkdownUiBlock(id=config.CANVAS_RESULTS_ID, type=MarkdownUiBlockType.MARKDOWN,
                                      value=results_markdown))
    return blocks


def results_to_markdown(outcomes):
    if not outcomes:
        return "**No Packed Columns found to sync.**"
    lines = ["#### Sync results"]
    for o in outcomes:
        label = f"{o.registry_id or o.column_id} {o.column_name or ''}".strip()
        lines.append(f"- {_ICON.get(o.result, '')} **{label}** — {o.result}: {o.message}")
        if o.column_type_name:
            lines.append(f"    - Column Type: `{o.column_type_name}` (scope {config.IMPORT_ACCESS_SCOPE})")
        for w in o.warnings:
            lines.append(f"    - ⚠️ {w}")
    return "\n".join(lines)


def _message(content, style=AppSessionMessageStyle.INFO):
    return AppSessionMessageCreate(content=content[:200], style=style)


# --------------------------------------------------------------------- events
def handle_canvas_initialized(body, benchling):
    msg = body["message"]
    benchling.apps.create_canvas(AppCanvasCreate(
        app_id=body["app"]["id"],
        feature_id=msg["featureId"],
        resource_id=msg["resourceId"],
        blocks=build_blocks(),
        enabled=True,
    ))


def handle_canvas_created(body, benchling):
    benchling.apps.update_canvas(canvas_id=body["message"]["canvasId"],
                                 canvas=AppCanvasUpdate(blocks=build_blocks(), enabled=True))


def handle_canvas_interaction(body, benchling):
    msg = body["message"]
    if msg.get("buttonId") != config.CANVAS_SYNC_BUTTON_ID:
        log.info("Ignoring button %s", msg.get("buttonId"))
        return

    canvas_id = msg["canvasId"]
    canvas = benchling.apps.get_canvas_by_id(canvas_id=canvas_id)
    selected = _selected_column_ids(canvas)
    entry_id = canvas.resource_id
    user = (msg.get("user") or {}).get("handle") or (msg.get("user") or {}).get("id")
    trigger = {"type": "canvas", "user": user, "entry_id": entry_id, "canvas_id": canvas_id}

    session_id = benchling.apps.create_session(AppSessionCreate(
        app_id=body["app"]["id"], name="Sync to UNICORN", timeout_seconds=3600,
        messages=[_message("Synchronisation started")])).id
    benchling.apps.update_canvas(canvas_id=canvas_id, canvas=AppCanvasUpdate(
        blocks=build_blocks(selected, "_Synchronising…_", enabled=False), enabled=False, session_id=session_id))

    try:
        reader = BenchlingReader(benchling)
        column_ids = selected or reader.list_column_ids_in_entry(entry_id)
        outcomes = sync_columns(reader, column_ids, trigger)
    except SyncError as e:  # run-level failure (auth, IP allowlist, Benchling down)
        log.exception("Sync run failed")
        _finish(benchling, canvas_id, session_id, selected, f"❌ **{e.kind}**: {e}",
                AppSessionUpdateStatus.FAILED, [_message(str(e), AppSessionMessageStyle.ERROR)])
        return
    except Exception as e:
        log.exception("Sync run failed unexpectedly")
        _finish(benchling, canvas_id, session_id, selected, f"❌ **Unexpected error**: {e}",
                AppSessionUpdateStatus.FAILED, [_message("Unexpected error - see logs", AppSessionMessageStyle.ERROR)])
        return

    counts = {r: sum(1 for o in outcomes if o.result == r) for r in ("Queued", "Skipped", "Failure")}
    summary = f"{counts['Queued']} queued, {counts['Skipped']} skipped, {counts['Failure']} failed"
    if counts["Failure"] and counts["Failure"] == len(outcomes):
        status, style = AppSessionUpdateStatus.FAILED, AppSessionMessageStyle.ERROR
    elif counts["Failure"]:
        status, style = AppSessionUpdateStatus.COMPLETED_WITH_WARNINGS, AppSessionMessageStyle.WARNING
    else:
        status, style = AppSessionUpdateStatus.SUCCEEDED, AppSessionMessageStyle.SUCCESS
    _finish(benchling, canvas_id, session_id, selected, results_to_markdown(outcomes), status,
            [_message(summary, style)])


def _finish(benchling, canvas_id, session_id, selected, markdown, status, messages):
    benchling.apps.update_canvas(canvas_id=canvas_id, canvas=AppCanvasUpdate(
        blocks=build_blocks(selected, markdown, enabled=True), enabled=True, session_id=session_id))
    benchling.apps.update_session(session_id=session_id, session=AppSessionUpdate(messages=messages, status=status))


def _selected_column_ids(canvas):
    for block in canvas.blocks or []:
        if getattr(block, "id", None) == config.CANVAS_COLUMNS_INPUT_ID:
            value = getattr(block, "value", None)
            return list(value) if value else []
    return []
