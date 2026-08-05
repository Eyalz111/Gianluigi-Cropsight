"""
Writes the weekly Project Status pack into a Google Sheets workbook — one tab
per area, laid out like "PROJECTS STATUS TEMPLATE 05082026.xlsx".

Kept out of services/google_sheets.py (already ~4k lines) so this feature stays
readable and independently testable; it reuses that module's authenticated
service and retry wrapper rather than building its own.

The row content comes from processors/project_status.py — this module is purely
the Sheets I/O half.
"""

import logging

from config.settings import settings
from services.google_sheets import sheets_service, _hex_color, _column_width_request

logger = logging.getLogger(__name__)

# Template fidelity — colours/widths read out of the supplied .xlsx.
_HEADER_BG = _hex_color("#0070C0")   # blue band, row 3
_TITLE_BG = _hex_color("#C00000")    # red title cell, A1
_WHITE = _hex_color("#FFFFFF")
# Excel char widths (4.9/28/49/48.1/13.9/10.9/30.6) -> px (px ≈ chars*7 + 5)
_COL_PX = [39, 201, 348, 342, 102, 81, 219]

WORKBOOK_TITLE = "CropSight — Projects Status"


def _tab_format_requests(sheet_id: int) -> list[dict]:
    """Formatting for one tab, mirroring the template."""
    reqs: list[dict] = [
        {"repeatCell": {  # A1 title: bold 20pt white on red
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _TITLE_BG,
                "verticalAlignment": "MIDDLE",
                "textFormat": {"bold": True, "fontSize": 20, "foregroundColor": _WHITE}}},
            "fields": "userEnteredFormat(backgroundColor,verticalAlignment,textFormat)"}},
        {"repeatCell": {  # row 3 header band: bold 18pt white on blue, centred
            "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3,
                      "startColumnIndex": 0, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_BG,
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
                "textFormat": {"bold": True, "fontSize": 18, "foregroundColor": _WHITE}}},
            "fields": ("userEnteredFormat(backgroundColor,horizontalAlignment,"
                       "verticalAlignment,wrapStrategy,textFormat)")}},
        {"repeatCell": {  # body: wrap so 'To do' is readable without resizing
            "range": {"sheetId": sheet_id, "startRowIndex": 3,
                      "startColumnIndex": 1, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                           "verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        {"repeatCell": {  # '#' column centred
            "range": {"sheetId": sheet_id, "startRowIndex": 3,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat(horizontalAlignment)"}},
        {"updateSheetProperties": {  # template freezes at A4
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": 3}},
            "fields": "gridProperties.frozenRowCount"}},
        {"updateBorders": {
            "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3,
                      "startColumnIndex": 0, "endColumnIndex": 7},
            "bottom": {"style": "SOLID_MEDIUM",
                       "color": {"red": 0, "green": 0, "blue": 0}}}},
    ]
    for idx, px in enumerate(_COL_PX):
        reqs.append(_column_width_request(sheet_id, idx, px))
    return reqs


async def write_project_status(pack: dict, title_blocks: dict,
                               spreadsheet_id: str = "") -> dict:
    """Write {area -> rows} to a workbook, one tab per area.

    Creates the workbook when no id is configured and returns it so the id can be
    pinned into PROJECT_STATUS_SHEET_ID.

    Each tab is CLEARED then rewritten: this is a generated snapshot, so a row
    left over from a previous run would read as a live open item. Anything typed
    during the meeting belongs in the meeting's own copy, not here.
    """
    from processors.project_status import HEADERS, tab_name_for

    svc = sheets_service
    result = {"spreadsheet_id": spreadsheet_id or settings.PROJECT_STATUS_SHEET_ID,
              "created": False, "tabs": [], "rows": 0, "url": "", "error": None}
    try:
        if not result["spreadsheet_id"]:
            created = svc._execute_with_retry(
                lambda: svc.service.spreadsheets().create(
                    body={"properties": {"title": WORKBOOK_TITLE}},
                    fields="spreadsheetId")
            )
            result["spreadsheet_id"] = created["spreadsheetId"]
            result["created"] = True
            logger.info(f"[project-status] created workbook {result['spreadsheet_id']}")

        ssid = result["spreadsheet_id"]

        def _tabs() -> dict:
            meta = svc._execute_with_retry(
                lambda: svc.service.spreadsheets().get(
                    spreadsheetId=ssid, fields="sheets.properties")
            )
            return {s["properties"]["title"]: s["properties"]["sheetId"]
                    for s in meta.get("sheets", [])}

        existing = _tabs()
        add = [{"addSheet": {"properties": {"title": tab_name_for(a)}}}
               for a in pack if tab_name_for(a) not in existing]
        if add:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().batchUpdate(
                    spreadsheetId=ssid, body={"requests": add})
            )
            existing = _tabs()

        fmt_reqs: list[dict] = []
        data: list[dict] = []
        for area, rows in pack.items():
            tab = tab_name_for(area)
            sheet_id = existing.get(tab)
            if sheet_id is None:
                logger.warning(f"[project-status] tab missing after create: {tab}")
                continue
            b = title_blocks.get(area, {})
            values = [
                [b.get("title", area), "", "", b.get("confidentiality", ""), "", "", ""],
                [b.get("distribution", ""), "", "", b.get("generated", ""), "", "", ""],
                list(HEADERS),
            ]
            values.extend(rows)
            svc._execute_with_retry(
                lambda t=tab: svc.service.spreadsheets().values().clear(
                    spreadsheetId=ssid, range=t, body={})
            )
            data.append({"range": f"'{tab}'!A1", "values": values})
            fmt_reqs.extend(_tab_format_requests(sheet_id))
            result["tabs"].append(tab)
            result["rows"] += len(rows)

        if data:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=ssid,
                    body={"valueInputOption": "RAW", "data": data})
            )
        if fmt_reqs:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().batchUpdate(
                    spreadsheetId=ssid, body={"requests": fmt_reqs})
            )

        # A freshly created workbook carries a default tab whose name is
        # LOCALE-DEPENDENT — on this org account it is 'גיליון1', not 'Sheet1',
        # so matching the English name silently leaves it behind. Drop whatever
        # isn't one of ours, but only on the run that created the workbook, so a
        # tab someone added by hand to a pinned sheet is never destroyed.
        if result["created"]:
            ours = {tab_name_for(a) for a in pack}
            stale = [sid for title, sid in _tabs().items() if title not in ours]
            if stale:
                svc._execute_with_retry(
                    lambda: svc.service.spreadsheets().batchUpdate(
                        spreadsheetId=ssid,
                        body={"requests": [{"deleteSheet": {"sheetId": s}} for s in stale]})
                )
                logger.info(f"[project-status] removed {len(stale)} default tab(s)")

        result["url"] = f"https://docs.google.com/spreadsheets/d/{ssid}/edit"
        logger.info(
            f"[project-status] wrote {result['rows']} row(s) across "
            f"{len(result['tabs'])} tab(s) -> {ssid}"
        )
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"[project-status] write failed: {e}")
    return result
