"""
Gantt row-tag plumbing (v3 chunk 2 — curated knowledge-view).

Robust Sheet<->topic identity for the Gantt: a hidden tag column (default DZ,
past the week grid) holds each row's topic UUID — the analog of the Tasks col-J
UUID. Rows are resolved LIVE by tag, never by absolute row number (which shifts
when rows are inserted/removed). Writes touch only the tag column (separate from
the labels A-D and the week bars E..max_week), so they can't disturb the grid,
formulas, or conditional formatting.
"""

import logging

from config.settings import settings
from guardrails.gantt_guard import _load_schema_metadata
from services.gantt_weeks import week_to_column
from services.google_sheets import sheets_service

logger = logging.getLogger(__name__)


def _col_to_index(col: str) -> int:
    """1-based column index for a letter ref (A=1, Z=26, AA=27, DZ=130)."""
    idx = 0
    for ch in col.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx


def tag_column() -> str:
    return (getattr(settings, "GANTT_TAG_COLUMN", "DZ") or "DZ").upper()


def verify_tag_column_safe() -> tuple[bool, str]:
    """
    The tag column MUST sit past the last week column for every sheet, else a
    tag write could land on a week-bar cell. Fail loud rather than guess.
    """
    meta = _load_schema_metadata()
    max_week = meta.get("max_week", 104)
    week_offset = meta.get("week_offset", 9)
    first_week_col = meta.get("first_week_col", "E")
    last_week_col = week_to_column(max_week, week_offset, first_week_col)
    tag_idx, last_idx = _col_to_index(tag_column()), _col_to_index(last_week_col)
    if tag_idx <= last_idx:
        return False, (
            f"Tag column {tag_column()} (idx {tag_idx}) is NOT past the last week "
            f"column {last_week_col} (idx {last_idx}) — pick a column further right."
        )
    return True, f"Tag column {tag_column()} is safe (past last week col {last_week_col})."


async def read_row_tags(sheet_name: str) -> dict[int, str]:
    """Map row_number -> topic_id from the tag column (blank cells skipped)."""
    col = tag_column()
    try:
        # Route through _execute_with_retry so an idle-wake broken pipe rebuilds
        # the transport instead of failing the read (audit RG-01 / June P3-07).
        resp = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.GANTT_SHEET_ID, range=f"'{sheet_name}'!{col}1:{col}")
        )
        out: dict[int, str] = {}
        for i, row in enumerate(resp.get("values", []), start=1):
            v = (row[0].strip() if row and row[0] else "")
            if v:
                out[i] = v
        return out
    except Exception as e:
        logger.error(f"read_row_tags({sheet_name}) failed: {e}")
        return {}


async def resolve_row_by_topic(sheet_name: str, topic_id: str) -> int | None:
    """Live row number for a topic id, by scanning the tag column (never a stored row)."""
    for row, tid in (await read_row_tags(sheet_name)).items():
        if tid == topic_id:
            return row
    return None


async def write_row_tag(sheet_name: str, row: int, topic_id: str) -> bool:
    """Write a topic id into the tag column for one row."""
    col = tag_column()
    try:
        # Retry-wrapped so a stale idle-wake socket rebuilds rather than silently
        # dropping the tag write (audit RG-01 / June P3-07).
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().update(
                spreadsheetId=settings.GANTT_SHEET_ID,
                range=f"'{sheet_name}'!{col}{row}",
                valueInputOption="RAW",
                body={"values": [[topic_id]]},
            )
        )
        return True
    except Exception as e:
        logger.error(f"write_row_tag({sheet_name},{row}) failed: {e}")
        return False


async def format_tag_column(sheet_name: str) -> bool:
    """
    Make the tag column visually invisible (white text on white background) so it
    stays hidden even if the sheet is accidentally unhidden. Idempotent.
    """
    try:
        sid = sheets_service._get_sheet_id_by_name(settings.GANTT_SHEET_ID, sheet_name)
        if sid is None:
            return False
        col_idx0 = _col_to_index(tag_column()) - 1  # 0-based for the API
        white = {"red": 1, "green": 1, "blue": 1}
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=settings.GANTT_SHEET_ID,
                body={"requests": [{
                    "repeatCell": {
                        "range": {
                            "sheetId": sid,
                            "startColumnIndex": col_idx0,
                            "endColumnIndex": col_idx0 + 1,
                        },
                        "cell": {"userEnteredFormat": {
                            "backgroundColor": white,
                            "textFormat": {"foregroundColor": white},
                        }},
                        "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.foregroundColor",
                    }
                }]},
            )
        )
        return True
    except Exception as e:
        logger.error(f"format_tag_column({sheet_name}) failed: {e}")
        return False
