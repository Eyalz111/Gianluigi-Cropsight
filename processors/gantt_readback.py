"""
PR-D: weekly Gantt read-back (board → knowledge). DB-only, never writes the board.

For each lane (gantt_rows, resolved by its schema row = display_order), reads the
authored bar off the live board (filled = non-empty text AND a known status color),
computes the lane's span + status-from-bar, and updates gantt_rows (manual-wins via
snapshot) so the knowledge layer knows the plan. Never mutates brief_json, never
writes the board. Multi-gap lanes are flagged (feed a nudge), not guessed.
"""

import logging

from config.settings import settings
from guardrails.gantt_guard import _load_schema_metadata
from services.gantt_manager import _get_color_map, _sheets_color_to_hex
from services.gantt_weeks import week_to_column
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


def _norm(v):
    return str(v or "").strip().lower()


async def reconcile_gantt_lanes(sheet=None, shadow: bool | None = None) -> dict:
    from services.google_sheets import sheets_service

    sheet = sheet or settings.GANTT_MAIN_TAB
    if shadow is None:
        shadow = getattr(settings, "GANTT_SHADOW_MODE", True)
    write_allowed = not shadow

    lanes = [r for r in supabase_client.get_gantt_rows(sheet)
             if r.get("lane_type") and r.get("display_order")]
    if not lanes:
        return {"sheet": sheet, "lanes": 0, "shadow": shadow}

    cmap = _get_color_map()
    status_by_hex = {_norm(v): k for k, v in cmap.items() if v}
    meta = _load_schema_metadata()
    week_offset = meta.get("week_offset", 9)
    first_col = meta.get("first_week_col", "E")
    max_week = meta.get("max_week", 104)
    last_col = week_to_column(max_week, week_offset, first_col)

    try:
        resp = sheets_service.service.spreadsheets().get(
            spreadsheetId=settings.GANTT_SHEET_ID,
            ranges=[f"'{sheet}'!{first_col}1:{last_col}"], includeGridData=True).execute()
        rowdata = resp["sheets"][0]["data"][0].get("rowData", [])
    except Exception as e:
        logger.error(f"[gantt_readback] grid read failed: {e}")
        return {"sheet": sheet, "error": str(e)}

    snaps = supabase_client.get_gantt_row_snapshots(sheet)
    summary = {"sheet": sheet, "lanes": len(lanes), "pulled": 0,
               "flagged_multigap": 0, "empty": 0, "shadow": shadow}

    for ln in lanes:
        row = ln["display_order"]
        cells = rowdata[row - 1].get("values", []) if (row - 1) < len(rowdata) else []
        filled, statuses = [], []
        for ci, c in enumerate(cells):
            txt = (c.get("formattedValue", "") or "").strip()
            bg = c.get("effectiveFormat", {}).get("backgroundColor")
            hexv = _norm(_sheets_color_to_hex(bg)) if bg else ""
            if txt and hexv in status_by_hex:        # filled = text AND known status color
                filled.append(week_offset + ci)
                statuses.append(status_by_hex[hexv])
        if not filled:
            summary["empty"] += 1
            continue
        ws, we = min(filled), max(filled)
        if set(range(ws, we + 1)) - set(filled):
            summary["flagged_multigap"] += 1
            continue
        bar_status = max(set(statuses), key=statuses.count) if statuses else None
        gid = ln["id"]
        snap = snaps.get(gid) or {}
        if snap.get("week_start") == ws and snap.get("week_end") == we:
            continue  # unchanged since last read-back
        summary["pulled"] += 1
        if write_allowed:
            try:
                upd = {"week_start": ws, "week_end": we}
                if bar_status:
                    upd["status"] = bar_status
                supabase_client.client.table("gantt_rows").update(upd).eq("id", gid).execute()
                supabase_client.mark_gantt_field_manual(gid, "timeframe", "sheet_edit")
                supabase_client.upsert_gantt_snapshot(gid, row, ws, we)
            except Exception as e:
                logger.warning(f"[gantt_readback] pull {gid} failed: {e}")

    try:
        supabase_client.log_action(
            "gantt_readback_shadow" if shadow else "gantt_readback_applied",
            details=summary, triggered_by="auto")
    except Exception:
        pass
    logger.info(f"[gantt_readback]{'[shadow]' if shadow else ''} {summary}")
    return summary
