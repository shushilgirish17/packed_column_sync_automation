"""
App Canvas in the notebook entry: pick Packed Columns (or leave empty = all columns
registered from this entry), click "Sync to UNICORN", see the per-column outcome.

The results block sits above the button (not just appended after a run) so the status
roster is visible as soon as the canvas loads, and stays current after the on-prem side
actually finishes the UNICORN import/edit - see report_column_result().
"""
import logging
import re

from benchling_sdk.models import (AppCanvasCreate, AppCanvasUpdate, ButtonUiBlock, ButtonUiBlockType,
                                  MarkdownUiBlock, MarkdownUiBlockType, SearchInputMultiValueUiBlock,
                                  SearchInputMultiValueUiBlockType, SearchInputUiBlockItemType)

import config
from sync.benchling_reader import BenchlingReader
from sync.models import ColumnOutcome, SyncError
from sync.packed_column_sync import sync_columns

log = logging.getLogger(__name__)

INTRO = ("### Sync Packed Columns to UNICORN\n"
         "Select the Packed Columns to send, or leave the field empty to send every Packed Column "
         "registered in this entry. Each Active column is queued for UNICORN: the Column Type is "
         "imported if it does not exist yet, otherwise its run parameters are updated.")

_NO_RESULTS_YET = "_No columns synced yet._"
_ICON = {"Queued": "⏳", "Synced": "✅", "Skipped": "⏭️", "Failure": "❌", "Failed": "❌"}


def build_blocks(selected_ids=None, results_markdown=None, enabled=True):
    return [
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
        MarkdownUiBlock(id=config.CANVAS_RESULTS_ID, type=MarkdownUiBlockType.MARKDOWN,
                        value=results_markdown or _NO_RESULTS_YET),
        ButtonUiBlock(id=config.CANVAS_SYNC_BUTTON_ID, type=ButtonUiBlockType.BUTTON,
                      text="Sync to UNICORN" if enabled else "Syncing…", enabled=enabled),
    ]


def _column_block(o) -> str:
    """One column's chunk of the results markdown, anchored by a hidden id comment so a later
    completion report can find-and-replace just this column's lines (see _patch_column_block).
    No hidden marker needed: Benchling's markdown renderer doesn't suppress HTML comments (they
    show up as literal text), so the match anchor is the registry id/entity id already bolded
    and visible in the line - real, meaningful content doubling as the key, nothing extra."""
    anchor = str(o.registry_id or o.column_id)
    label = f"{anchor} {o.column_name or ''}".strip()
    lines = [f"- {_ICON.get(o.result, '')} **{label}** — {o.result}: {o.message}"]
    if o.column_type_name:
        lines.append(f"    - Column Type: `{o.column_type_name}` (scope {config.IMPORT_ACCESS_SCOPE})")
    for w in o.warnings:
        lines.append(f"    - ⚠️ {w}")
    return "\n".join(lines)


def results_to_markdown(outcomes):
    if not outcomes:
        return "**No Packed Columns found to sync.**"
    return "\n".join(["#### Sync results"] + [_column_block(o) for o in outcomes])


def _merge_results(existing_markdown: str, outcomes) -> str:
    """Accumulate this run's outcomes into the existing results markdown instead of replacing it
    wholesale - so deselecting a previously-synced column before the next run never drops its
    line, every entity ever put through this canvas keeps its own row. Reuses the same
    patch-or-append logic report_column_result uses for a single completion."""
    if not outcomes:
        return existing_markdown or _NO_RESULTS_YET
    markdown = existing_markdown or ""
    for o in outcomes:
        markdown = _patch_column_block(markdown, str(o.registry_id or o.column_id), _column_block(o))
    return markdown


def _prepend_error(existing_markdown: str, kind: str, message: str) -> str:
    """A run-level failure (auth, IP allowlist, Benchling down) happens before any per-column
    outcome exists, so there's nothing to merge - just surface it above whatever history is
    already there instead of replacing it."""
    base = existing_markdown if existing_markdown and existing_markdown.strip() not in (
        "", _NO_RESULTS_YET, "**No Packed Columns found to sync.**") else "#### Sync results"
    return f"> ❌ **{kind}**: {message}\n\n{base}"


def _patch_column_block(markdown: str, anchor: str, new_block: str) -> str:
    """Replace one column's chunk (bullet + its indented sub-lines) in an existing results
    markdown, matched by the anchor already bolded at the start of its line, or append it if
    this column has no chunk yet. Used by report_column_result to update a single column without
    disturbing the others - the canvas's own current markdown IS the persisted state."""
    pattern = re.compile(rf"^-.*\*\*{re.escape(anchor)}\b.*?(?=\n-\s|\Z)", re.DOTALL | re.MULTILINE)
    if pattern.search(markdown):
        patched = pattern.sub(lambda _m: new_block, markdown, count=1)
    elif not markdown or markdown.strip() in ("", _NO_RESULTS_YET, "**No Packed Columns found to sync.**"):
        patched = "#### Sync results\n" + new_block
    else:
        patched = markdown.rstrip() + "\n" + new_block
    return patched.rstrip()


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
    # Snapshot before this run, so switching which columns are selected between runs never drops
    # a previously synced column's line - see _merge_results.
    existing_markdown = _results_block_value(canvas)

    # No AppSession here (deliberately) - Benchling permanently closes a session once given a
    # terminal status, so a fixed "queued" banner set now would go stale and misleading once the
    # real UNICORN outcome (possibly a failure) is known later. The "Sync results" block below is
    # the only status surface, and it stays truthfully updatable via report_column_result.
    trigger = {"type": "canvas", "user": user, "entry_id": entry_id, "canvas_id": canvas_id}
    benchling.apps.update_canvas(canvas_id=canvas_id, canvas=AppCanvasUpdate(
        blocks=build_blocks(selected, existing_markdown, enabled=False), enabled=False))

    try:
        reader = BenchlingReader(benchling)
        column_ids = selected or reader.list_column_ids_in_entry(entry_id)
        outcomes = sync_columns(reader, column_ids, trigger)
    except SyncError as e:  # run-level failure (auth, IP allowlist, Benchling down)
        log.exception("Sync run failed")
        _finish(benchling, canvas_id, selected, _prepend_error(existing_markdown, e.kind, str(e)))
        return
    except Exception as e:
        log.exception("Sync run failed unexpectedly")
        _finish(benchling, canvas_id, selected, _prepend_error(existing_markdown, "Unexpected Error", str(e)))
        return

    _finish(benchling, canvas_id, selected, _merge_results(existing_markdown, outcomes))


def _finish(benchling, canvas_id, selected, markdown):
    benchling.apps.update_canvas(canvas_id=canvas_id, canvas=AppCanvasUpdate(
        blocks=build_blocks(selected, markdown, enabled=True), enabled=True))


def _selected_column_ids(canvas):
    for block in canvas.blocks or []:
        if getattr(block, "id", None) == config.CANVAS_COLUMNS_INPUT_ID:
            value = getattr(block, "value", None)
            return list(value) if value else []
    return []


def _results_block_value(canvas):
    for block in canvas.blocks or []:
        if getattr(block, "id", None) == config.CANVAS_RESULTS_ID:
            return getattr(block, "value", None) or ""
    return ""


def report_column_result(body: dict, benchling) -> None:
    """Completion callback from the on-prem UNICORN consumer (watch_service.ps1), invoked
    directly as a Lambda action (not a Benchling webhook) once a queued column's real import/edit
    outcome is known. Patches just that column's line in the canvas that originally queued it -
    the canvas's current markdown is the only persisted state this needs, no extra field or store.

    No AppSession involved at all (see handle_canvas_interaction) - an earlier version tried
    creating one for a top status banner, but Benchling permanently closes a session once given a
    terminal status, so a later re-update attempt hit AppSessionClosedError ("Session has been
    closed and cannot be updated") - a hard platform constraint, not a bug. The canvas patch here
    is the only status surface, and unlike a session it can be updated any number of times.
    """
    canvas_id = body["canvas_id"]
    outcome = ColumnOutcome(
        column_id=body["column_id"],
        column_name=body.get("column_name"),
        registry_id=body.get("registry_id"),
        result=body.get("result", "Failure"),
        message=body.get("message", ""),
        column_type_name=body.get("column_type_name"),
        warnings=body.get("warnings") or [],
    )
    canvas = benchling.apps.get_canvas_by_id(canvas_id=canvas_id)
    anchor = str(outcome.registry_id or outcome.column_id)
    patched = _patch_column_block(_results_block_value(canvas), anchor, _column_block(outcome))
    benchling.apps.update_canvas(canvas_id=canvas_id, canvas=AppCanvasUpdate(
        blocks=build_blocks(_selected_column_ids(canvas), patched, enabled=True), enabled=True))
