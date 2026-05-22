"""
PR-B: copy + add-rows engine (the FRONT change). Copy-first, reversible.

Adds +1 Planning and +2 Execution rows per Area (→ 2 Planning + 3 Execution end
state) so the board has the lanes Eyal wants. The ONLY structural change, and it
happens on a fresh Drive COPY first — verified, Eyal reviews — then a separate,
explicit, backup-first cutover applies the SAME inserts to the live board.

Safety: propose_restructure() touches ONLY the working copy (never GANTT_SHEET_ID).
Insertion uses InsertDimension(inheritFromBefore=true) bottom-to-top so formatting
carries and earlier inserts don't shift later indices. Gated GANTT_RESTRUCTURE_ENABLED.
"""

import logging
from datetime import date

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

ADD_PLANNING = 1   # 1 existing + 1 = 2 Planning
ADD_EXECUTION = 2  # 1 existing + 2 = 3 Execution


def _tab_gid(spreadsheet_id: str, tab: str) -> int | None:
    from services.google_sheets import sheets_service
    meta = sheets_service.service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab:
            return s["properties"]["sheetId"]
    return None


def _compute_inserts(sheet: str) -> list[tuple[int, int]]:
    """(startIndex, count) per Area's Planning(+1)/Execution(+2), bottom-to-top.

    startIndex = the 1-based row of the existing lane (insert AFTER it; inheritFromBefore
    copies that row's formatting). Sorted DESC so earlier inserts don't shift later rows.
    """
    lanes = [r for r in supabase_client.get_gantt_rows(sheet)
             if r.get("lane_type") in ("planning", "execution") and r.get("lane_index") == 1
             and r.get("display_order")]
    inserts = []
    for ln in lanes:
        count = ADD_PLANNING if ln["lane_type"] == "planning" else ADD_EXECUTION
        inserts.append((ln["display_order"], count))
    return sorted(inserts, key=lambda x: -x[0])


def _make_copy(name: str) -> str:
    from services.google_drive import drive_service
    body = {"name": name}
    if settings.GANTT_BACKUP_FOLDER_ID:
        body["parents"] = [settings.GANTT_BACKUP_FOLDER_ID]
    f = drive_service.service.files().copy(fileId=settings.GANTT_SHEET_ID, body=body).execute()
    return f.get("id")


def _apply_inserts(spreadsheet_id: str, gid: int, inserts: list[tuple[int, int]]) -> int:
    """Apply InsertDimension requests to the TARGET spreadsheet (caller guarantees it's the copy)."""
    from services.google_sheets import sheets_service
    reqs = [{"insertDimension": {
        "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": start, "endIndex": start + count},
        "inheritFromBefore": True}} for (start, count) in inserts]
    if reqs:
        sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": reqs}).execute()
    return len(reqs)


async def _verify(spreadsheet_id: str, tab: str) -> dict:
    """Re-parse the copy (no persist) + count per-section Planning/Execution + cond-format rules."""
    from scripts.parse_gantt_schema import parse_gantt_schema
    from services.google_sheets import sheets_service
    parsed = await parse_gantt_schema(spreadsheet_id=spreadsheet_id, sheet_name=tab, persist=False)
    rows = parsed.get("schema_rows", [])
    counts = {}
    for r in rows:
        sub = (r.get("subsection") or "").strip().lower()
        sec = r.get("section") or ""
        if sub in ("planning", "execution"):
            counts.setdefault(sec, {}).setdefault(sub, 0)
            counts[sec][sub] += 1
    # conditional-format rule count (coverage sanity)
    cf = sheets_service.service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets(conditionalFormats)").execute()
    cf_count = sum(len(s.get("conditionalFormats", []) or []) for s in cf.get("sheets", []))
    ok = all(c.get("planning", 0) >= 2 and c.get("execution", 0) >= 3 for c in counts.values())
    return {"per_section": counts, "cond_format_rules": cf_count, "all_areas_2p3e": ok}


async def propose_restructure() -> dict:
    """Make a working COPY, add the rows there, verify, and surface a preview. NEVER touches live."""
    if not getattr(settings, "GANTT_RESTRUCTURE_ENABLED", False):
        return {"status": "disabled", "hint": "set GANTT_RESTRUCTURE_ENABLED=true to use"}
    tab = settings.GANTT_MAIN_TAB
    inserts = _compute_inserts(tab)
    name = f"Gantt RESTRUCTURE WORKING {date.today().isoformat()}"
    copy_id = _make_copy(name)
    gid = _tab_gid(copy_id, tab)
    if gid is None:
        return {"status": "error", "error": f"tab '{tab}' not found in copy"}
    applied = _apply_inserts(copy_id, gid, inserts)   # COPY ONLY
    verify = await _verify(copy_id, tab)
    preview = {
        "status": "preview", "working_copy_id": copy_id,
        "link": f"https://docs.google.com/spreadsheets/d/{copy_id}",
        "inserts_applied": applied, "rows_added": sum(c for _, c in inserts),
        "verify": verify,
    }
    try:
        supabase_client.upsert_pending_approval(
            approval_id=f"grestructure-{copy_id[:8]}", content_type="gantt_restructure_preview", content=preview)
    except Exception as e:
        logger.warning(f"restructure preview persist failed: {e}")
    logger.info(f"[gantt_restructure] proposed on copy {copy_id}: {verify}")
    return preview


async def apply_restructure_to_live(working_copy_id: str, confirm: bool = False) -> dict:
    """Cutover: apply the SAME inserts to the LIVE board. Gated, backup-first, idempotent."""
    if not confirm or not getattr(settings, "GANTT_RESTRUCTURE_ENABLED", False):
        return {"status": "blocked", "hint": "requires confirm=True AND GANTT_RESTRUCTURE_ENABLED"}
    tab = settings.GANTT_MAIN_TAB
    # idempotency: if any area already has a 2nd planning/execution lane, abort
    lanes = supabase_client.get_gantt_rows(tab)
    if any(r.get("lane_type") in ("planning", "execution") and (r.get("lane_index") or 0) >= 2 for r in lanes):
        return {"status": "already_cut_over", "hint": "live board already has extra lanes"}

    from services.gantt_manager import gantt_manager
    backup = await gantt_manager.backup_full_gantt()
    if backup.get("status") != "success":
        return {"status": "error", "error": f"backup failed: {backup.get('error')}", "aborted": True}

    inserts = _compute_inserts(tab)
    gid = _tab_gid(settings.GANTT_SHEET_ID, tab)
    applied = _apply_inserts(settings.GANTT_SHEET_ID, gid, inserts)   # LIVE
    from scripts.parse_gantt_schema import parse_gantt_schema
    await parse_gantt_schema(persist=True)   # re-parse live (row numbers shifted)
    verify = await _verify(settings.GANTT_SHEET_ID, tab)
    result = {"status": "applied", "inserts_applied": applied, "backup_file_id": backup.get("file_id"),
              "verify": verify, "reseed_hint": "run scripts/seed_gantt_lanes.py --apply to refresh the lane mirror"}
    supabase_client.log_action("gantt_restructured_live", details=result, triggered_by="eyal")
    logger.warning(f"[gantt_restructure] LIVE cutover applied; backup={backup.get('file_id')}")
    return result
