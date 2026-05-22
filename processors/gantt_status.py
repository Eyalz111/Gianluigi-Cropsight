"""
Gantt status rollup (v3 chunk 2 — curated knowledge-view).

Sets each curated Gantt row's bar COLOR from its topic's brief current_status — a
derived display of already-approved knowledge state (not new content). Principles:
- Color-only: never rewrites Eyal's label text (that's his content).
- Sticky-aware: skips rows he manually set (manual_status).
- Sensitivity-gated: skips CEO-tier topics (the Gantt is team-visible).
- Honors protected + conditional-format rows (color skipped where the sheet owns it).
- Shadow-capable: computes + logs without writing when GANTT_SHADOW_MODE is on.

(The task-rollup "⚠ N overdue" escalation, which would change cell text, is a
deliberate follow-on — kept out of the color-only core to avoid clobbering labels.)
"""

import logging

from config.settings import settings
from guardrails.gantt_guard import _load_schema, _load_schema_metadata, is_protected
from services.gantt_manager import _get_color_map, _hex_to_sheets_color
from services.gantt_rows import resolve_row_by_topic
from services.gantt_weeks import column_to_index, week_to_column
from services.google_sheets import sheets_service
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# TopicStatus -> existing gantt color-map status key
_STATUS_MAP = {
    "active": "active",
    "blocked": "blocked",
    "stale": "planned",
    "pending_decision": "planned",
    "closed": "completed",
}


def _topic_status(topic: dict) -> str | None:
    for src in (topic.get("brief_json"), topic.get("state_json")):
        if isinstance(src, dict) and src.get("current_status"):
            return src["current_status"]
    return None


def _is_ceo_only(topic: dict) -> bool:
    """Sensitivity gate: don't render CEO-tier topics on the team-visible Gantt."""
    for src in (topic.get("brief_json"), topic.get("state_json")):
        if isinstance(src, dict) and (src.get("sensitivity") or "").lower() == "ceo":
            return True
    return (topic.get("sensitivity") or "").lower() == "ceo"


def _cond_format_rows(sheet_name: str) -> set:
    out = set()
    for r in _load_schema():
        if r.get("sheet_name", "").lower() == sheet_name.lower() and "cond_format" in (r.get("notes") or ""):
            out.add(r.get("row_number"))
    return out


async def rollup_gantt_status(sheet_name: str, shadow: bool | None = None) -> dict:
    if shadow is None:
        shadow = getattr(settings, "GANTT_SHADOW_MODE", True)

    rows = supabase_client.get_gantt_rows(sheet_name)
    color_map = _get_color_map()
    meta = _load_schema_metadata()
    week_offset = meta.get("week_offset", 9)
    first_week_col = meta.get("first_week_col", "E")
    cond_rows = _cond_format_rows(sheet_name)
    sid = sheets_service._get_sheet_id_by_name(settings.GANTT_SHEET_ID, sheet_name)

    summary = {"sheet": sheet_name, "rows": len(rows), "recolored": 0,
               "skipped_sticky": 0, "skipped_ceo": 0, "no_timeframe": 0, "shadow": shadow}
    requests = []
    db_updates = []  # (gantt_row_id, gantt_status)

    for row in rows:
        if not row.get("topic_id"):
            continue
        if row.get("manual_status"):
            summary["skipped_sticky"] += 1
            continue
        topic = supabase_client.get_topic_thread(row["topic_id"]) or {}
        if _is_ceo_only(topic):
            summary["skipped_ceo"] += 1
            continue
        gantt_status = _STATUS_MAP.get((_topic_status(topic) or "").lower())
        if not gantt_status or gantt_status not in color_map:
            continue
        db_updates.append((row["id"], gantt_status))

        ws, we = row.get("week_start"), row.get("week_end")
        if not ws or not we:
            summary["no_timeframe"] += 1
            continue
        live_row = await resolve_row_by_topic(sheet_name, row["topic_id"])
        if not live_row or is_protected(sheet_name, live_row) or live_row in cond_rows:
            continue
        color = _hex_to_sheets_color(color_map[gantt_status])
        for wk in range(ws, we + 1):
            col_idx = column_to_index(week_to_column(wk, week_offset, first_week_col))
            requests.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": live_row - 1, "endRowIndex": live_row,
                          "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }})
        summary["recolored"] += 1

    if shadow:
        logger.info(f"[gantt_rollup][shadow] {summary}")
        try:
            supabase_client.log_action("shadow_gantt_rollup", details=summary, triggered_by="auto")
        except Exception:
            pass
        return summary

    for (gid, st) in db_updates:
        try:
            supabase_client.client.table("gantt_rows").update({"status": st}).eq("id", gid).execute()
        except Exception as e:
            logger.warning(f"gantt_rows status update failed {gid}: {e}")
    if requests:
        try:
            sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=settings.GANTT_SHEET_ID, body={"requests": requests}
            ).execute()
        except Exception as e:
            logger.error(f"gantt rollup write failed: {e}")
            return {**summary, "error": "write_failed"}
    try:
        supabase_client.log_action("gantt_rollup_applied", details=summary, triggered_by="auto")
    except Exception:
        pass
    logger.info(f"[gantt_rollup] {summary}")
    return summary
