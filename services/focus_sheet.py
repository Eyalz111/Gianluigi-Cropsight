"""Writes the Focus tab and its hidden data tab.

Split from `processors/focus_view.py` on the house rule: the processor decides
WHAT the view contains and holds no I/O; this module only talks to Sheets.
"""

import logging

from config.settings import settings
from processors.focus_view import (
    CTL_AREA, CTL_GROUP, CTL_OWNER, CTL_SHOW, DATA_HEADERS, FOCUS_DATA_TAB,
    FOCUS_TAB, GROUP_CHOICES, HEADERS, SHOW_CHOICES, build_rows, focus_layout,
)
from services.google_sheets import sheets_service

logger = logging.getLogger(__name__)

_TITLE_BG = {"red": 0.75, "green": 0.0, "blue": 0.0}
_HEADER_BG = {"red": 0.0, "green": 0.44, "blue": 0.75}
_CTL_BG = {"red": 0.93, "green": 0.95, "blue": 0.98}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_OVERDUE_BG = {"red": 0.957, "green": 0.78, "blue": 0.765}
_TODAY_BG = {"red": 0.988, "green": 0.91, "blue": 0.698}
_NODATE_BG = {"red": 0.937, "green": 0.937, "blue": 0.937}

_PROTECT_DESC = "Gianluigi: Focus is generated — change the dropdowns, not the rows"
_COL_PX = [110, 90, 420, 150, 80, 190, 210, 80]


def _cell(value: str) -> dict:
    return {"userEnteredValue": {"stringValue": value}}


def _dropdown(sheet_id: int, a1_row: int, a1_col: int, choices: list[str]) -> dict:
    """A ONE_OF_LIST validation. `strict` so a typo cannot silently break the
    QUERY that reads this cell — an unmatched value would return zero rows and
    look like "nothing is open"."""
    return {"setDataValidation": {
        "range": {"sheetId": sheet_id,
                  "startRowIndex": a1_row, "endRowIndex": a1_row + 1,
                  "startColumnIndex": a1_col, "endColumnIndex": a1_col + 1},
        "rule": {
            "condition": {"type": "ONE_OF_LIST",
                          "values": [{"userEnteredValue": c} for c in choices]},
            "showCustomUi": True,
            "strict": True,
        }}}


def _bucket_colour_rules(sheet_id: int) -> list[dict]:
    """Colour the whole row by its When cell. Three states only — the eye needs
    late, today, and forgotten; colouring all six buckets makes none of them
    stand out."""
    rules = []
    for idx, (text, bg) in enumerate((
        ("OVERDUE", _OVERDUE_BG), ("TODAY", _TODAY_BG), ("NO DATE", _NODATE_BG),
    )):
        rules.append({"addConditionalFormatRule": {"index": idx, "rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": 4,
                        "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [
                    {"userEnteredValue": f'=$A5="{text}"'}]},
                "format": {"backgroundColor": bg},
            }}}})
    return rules


async def refresh_focus(spreadsheet_id: str | None = None) -> dict:
    """Rebuild both tabs. Returns a small summary for the caller to log."""
    ssid = spreadsheet_id or settings.PROJECT_STATUS_SHEET_ID
    if not ssid:
        return {"skipped": "no PROJECT_STATUS_SHEET_ID"}

    rows = build_rows()
    if not rows:
        # Same reasoning as _rebuild_readonly_tab's force_empty guard: a failed
        # read and a genuinely empty backlog are indistinguishable here, and
        # blanking the view is the more expensive mistake.
        logger.warning("[focus] 0 open items — leaving the tab as it stands")
        return {"skipped": "no rows"}

    ok = await sheets_service._rebuild_readonly_tab(
        FOCUS_DATA_TAB, DATA_HEADERS, rows, spreadsheet_id=ssid)
    if not ok:
        return {"error": "data tab write failed"}

    owners = sorted({r[3] for r in rows if r[3]})
    areas = sorted({r[7] for r in rows if r[7]})

    sid = await sheets_service._ensure_tab(FOCUS_TAB, HEADERS, spreadsheet_id=ssid)
    data_sid = await sheets_service._ensure_tab(
        FOCUS_DATA_TAB, DATA_HEADERS, spreadsheet_id=ssid)
    if sid is None:
        return {"error": "could not create the Focus tab"}

    # USER_ENTERED, not RAW: every cell written here is a formula, and RAW would
    # store them as text that looks right and computes nothing.
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().update(
            spreadsheetId=ssid, range=f"'{FOCUS_TAB}'!A1:H5",
            valueInputOption="USER_ENTERED",
            body={"values": focus_layout()}))

    reqs: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 4}},
            "fields": "gridProperties.frozenRowCount"}},
        # Hide the data tab. It is not secret — it is noise, and a tab nobody
        # should read sitting next to one they must read is how the wrong tab
        # gets edited.
        {"updateSheetProperties": {
            "properties": {"sheetId": data_sid, "hidden": True},
            "fields": "hidden"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _TITLE_BG,
                "textFormat": {"bold": True, "fontSize": 12,
                               "foregroundColor": _WHITE}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2,
                      "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
            "cell": {"userEnteredFormat": {"backgroundColor": _CTL_BG,
                                           "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 3, "endRowIndex": 4,
                      "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": _WHITE}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        _dropdown(sid, 1, 1, GROUP_CHOICES),
        _dropdown(sid, 1, 3, ["All"] + owners),
        _dropdown(sid, 1, 5, ["All"] + areas),
        _dropdown(sid, 1, 7, SHOW_CHOICES),
    ]
    for i, px in enumerate(_COL_PX):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}})

    # ASSERT, don't add: clear our own colour rules and protection before
    # re-adding them. Conditional formats and protected ranges survive a values
    # clear, so re-running this without the delete stacks a duplicate rule every
    # cycle — five of the fifteen defects in the 2026-08-09 review were exactly
    # this. [2026-08-11]
    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=ssid,
            fields="sheets(properties.sheetId,conditionalFormats,protectedRanges)"))
    for sheet in meta.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != sid:
            continue
        for idx in reversed(range(len(sheet.get("conditionalFormats") or []))):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": idx}})
        for pr in sheet.get("protectedRanges") or []:
            if pr.get("description") == _PROTECT_DESC:
                reqs.append({"deleteProtectedRange":
                             {"protectedRangeId": pr["protectedRangeId"]}})

    reqs.extend(_bucket_colour_rules(sid))
    # Row 5 down only. Row 2 holds the dropdowns and MUST stay editable — the
    # tab is useless if the controls are locked with everything else.
    reqs.append({"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": sid, "startRowIndex": 4},
        "description": _PROTECT_DESC,
        "warningOnly": True,
    }}})

    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=ssid, body={"requests": reqs}))

    logger.info(f"[focus] rebuilt: {len(rows)} open item(s), "
                f"{len(owners)} owner(s), {len(areas)} area(s)")
    return {"rows": len(rows), "owners": len(owners), "areas": len(areas)}
