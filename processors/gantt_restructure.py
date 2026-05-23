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


def _write_labels(spreadsheet_id: str, tab: str) -> int:
    """Label the original + newly-inserted lanes in col B: Planning #1/#2, Execution #1/2/3.

    Walks col B; each 'Planning' becomes #1 and the next ADD_PLANNING (blank, inserted) rows
    become #2..; each 'Execution' becomes #1 and the next ADD_EXECUTION rows #2,#3. The new
    rows are the inserts placed directly after the existing lane — works for every section
    (incl. LEGAL) regardless of the section-header column."""
    from services.google_sheets import sheets_service
    svc = sheets_service.service
    rows = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{tab}'!A6:B70").execute().get("values", [])
    updates, i, n = [], 0, len(rows)
    while i < n:
        b = ((rows[i] + ["", ""])[1] or "").strip().lower() if rows[i] else ""
        rn = 6 + i
        if b == "planning":
            updates.append((rn, "Planning #1"))
            for k in range(1, ADD_PLANNING + 1):
                updates.append((rn + k, f"Planning #{1 + k}"))
            i += 1 + ADD_PLANNING
        elif b == "execution":
            updates.append((rn, "Execution #1"))
            for k in range(1, ADD_EXECUTION + 1):
                updates.append((rn + k, f"Execution #{1 + k}"))
            i += 1 + ADD_EXECUTION
        else:
            i += 1
    data = [{"range": f"'{tab}'!B{rn}", "values": [[lab]]} for rn, lab in updates]
    if data:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
    return len(updates)


async def _verify(spreadsheet_id: str, tab: str) -> dict:
    """Count Planning/Execution lanes from col B + conditional-format rule count (coverage sanity)."""
    from services.google_sheets import sheets_service
    svc = sheets_service.service
    rows = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"'{tab}'!A6:B70").execute().get("values", [])
    planning = sum(1 for r in rows if ((r + ["", ""])[1] or "").strip().lower().startswith("planning"))
    execution = sum(1 for r in rows if ((r + ["", ""])[1] or "").strip().lower().startswith("execution"))
    cf = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets(conditionalFormats)").execute()
    cf_count = sum(len(s.get("conditionalFormats", []) or []) for s in cf.get("sheets", []))
    return {"planning_lanes": planning, "execution_lanes": execution, "cond_format_rules": cf_count}


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
    labeled = _write_labels(copy_id, tab)             # label the new lanes (COPY ONLY)
    verify = await _verify(copy_id, tab)
    preview = {
        "status": "preview", "working_copy_id": copy_id,
        "link": f"https://docs.google.com/spreadsheets/d/{copy_id}",
        "inserts_applied": applied, "rows_added": sum(c for _, c in inserts),
        "labels_written": labeled, "verify": verify,
    }
    try:
        supabase_client.upsert_pending_approval(
            approval_id=f"grestructure-{copy_id[:8]}", content_type="gantt_restructure_preview", content=preview)
    except Exception as e:
        logger.warning(f"restructure preview persist failed: {e}")
    logger.info(f"[gantt_restructure] proposed on copy {copy_id}: {verify}")
    return preview


def _reapply_legal_fix(tab: str) -> int:
    """Restore the LEGAL section after a re-parse. Its header text sits in col B, so the
    parser mis-files it as a Fundraising subsection; re-mark the header + re-attribute its
    rows (up to the next real section header). Idempotent."""
    sc = supabase_client.client
    rows = [r for r in (sc.table("gantt_schema").select("row_number,section,subsection,notes")
            .eq("sheet_name", tab).execute().data or []) if r.get("row_number")]
    hdr = next((r for r in rows if "legal" in (r.get("subsection") or "").lower()
                and "finance" in (r.get("subsection") or "").lower()), None)
    if not hdr:
        return 0
    R = hdr["row_number"]
    next_hdr = min((r["row_number"] for r in rows
                    if r["row_number"] > R and (r.get("notes") or "").startswith("section_header")),
                   default=10 ** 9)
    sc.table("gantt_schema").update(
        {"section": "LEGAL, CORPORATE & FINANCE", "subsection": None,
         "notes": "section_header", "protected": True}
    ).eq("sheet_name", tab).eq("row_number", R).execute()
    moved = 1
    for r in rows:
        if R < r["row_number"] < next_hdr:
            sc.table("gantt_schema").update({"section": "LEGAL, CORPORATE & FINANCE"}).eq(
                "sheet_name", tab).eq("row_number", r["row_number"]).execute()
            moved += 1
    logger.info(f"[gantt_restructure] re-applied LEGAL schema fix at row {R} ({moved} rows)")
    return moved


async def apply_restructure_to_live(working_copy_id: str, confirm: bool = False) -> dict:
    """Cutover: apply the SAME inserts to the LIVE board. Gated, backup-first, idempotent."""
    if not confirm or not getattr(settings, "GANTT_RESTRUCTURE_ENABLED", False):
        return {"status": "blocked", "hint": "requires confirm=True AND GANTT_RESTRUCTURE_ENABLED"}
    tab = settings.GANTT_MAIN_TAB
    # idempotency: abort if the board already carries the restructure signature ('Planning #2'
    # labels). NB: pre-existing area extras (Marketing, Finance & Admin) are NOT the signature.
    from services.google_sheets import sheets_service
    bvals = sheets_service.service.spreadsheets().values().get(
        spreadsheetId=settings.GANTT_SHEET_ID, range=f"'{tab}'!B6:B70").execute().get("values", [])
    if any("planning #2" in ((r or [""])[0] or "").lower() for r in bvals):
        return {"status": "already_cut_over", "hint": "live board already has 'Planning #2' lanes"}

    from services.gantt_manager import gantt_manager
    backup = await gantt_manager.backup_full_gantt()
    if backup.get("status") != "success":
        return {"status": "error", "error": f"backup failed: {backup.get('error')}", "aborted": True}

    inserts = _compute_inserts(tab)
    gid = _tab_gid(settings.GANTT_SHEET_ID, tab)
    applied = _apply_inserts(settings.GANTT_SHEET_ID, gid, inserts)   # LIVE
    _write_labels(settings.GANTT_SHEET_ID, tab)   # label the new lanes
    from scripts.parse_gantt_schema import parse_gantt_schema
    await parse_gantt_schema(persist=True)   # re-parse live (row numbers shifted)
    _reapply_legal_fix(tab)                  # re-parse re-mis-files LEGAL (col-B header) -> restore it
    verify = await _verify(settings.GANTT_SHEET_ID, tab)
    result = {"status": "applied", "inserts_applied": applied, "backup_file_id": backup.get("file_id"),
              "verify": verify, "reseed_hint": "run scripts/seed_gantt_lanes.py --apply to refresh the lane mirror"}
    supabase_client.log_action("gantt_restructured_live", details=result, triggered_by="eyal")
    logger.warning(f"[gantt_restructure] LIVE cutover applied; backup={backup.get('file_id')}")
    return result
