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

# v2 block layout ------------------------------------------------------------
_GREY = _hex_color("#EFEFEF")        # missing date / missing owner
_RED = _hex_color("#F4C7C3")         # past due
_AMBER = _hex_color("#FCE8B2")       # due soon
_PROTECT_DESC = "Gianluigi system columns — do not edit"
HOWTO_TAB = "How to use"

# Widths for the 5 hidden columns. They are hidden, but a width keeps the sheet
# sane if anyone unhides them.
_HIDDEN_PX = 120


def _ddmmyyyy(col: str, row: int = 4) -> str:
    """A locale-proof DATE() over a DD/MM/YYYY cell.

    DATEVALUE() reads the SPREADSHEET's locale, so "05/08/2026" is 5 August in
    en_GB and 8 May in en_US — the one genuinely ambiguous case, and the whole
    file is Israel/Europe. Slicing the string and feeding DATE() explicitly
    removes the locale from the question. A hand-typed "12 Aug" fails the
    VALUE() calls, IFERROR swallows it, and the row simply isn't highlighted —
    which is the correct degradation: no highlight beats a wrong one.
    """
    c = f"${col}{row}"
    return (f"DATE(VALUE(RIGHT({c},4)),VALUE(MID({c},4,2)),VALUE(LEFT({c},2)))")


def _conditional_format_rules(sheet_id: int, due_soon_days: int) -> list[dict]:
    """Colour rules for a tab, attached once and independent of row count.

    Every rule is scoped to action rows via the hidden `_kind` column ($H="A"),
    so a project row is never coloured. Because they are sheet-level rules over
    an open-ended range they survive inserts, deletes and re-ordering — no
    per-row API calls, and nothing to re-apply when Nechama moves a line.
    """
    date_expr = _ddmmyyyy("E")
    is_action = '$H4="A"'
    not_done = "NOT($A4=TRUE)"

    def rule(col_start, col_end, formula, colour, index):
        return {"addConditionalFormatRule": {"index": index, "rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": 3,
                        "startColumnIndex": col_start, "endColumnIndex": col_end}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": formula}]},
                "format": {"backgroundColor": colour}}}}}

    return [
        # Order matters: the first matching rule wins, so overdue beats due-soon.
        rule(4, 5, f'=AND({is_action},{not_done},IFERROR({date_expr}<TODAY(),FALSE))',
             _RED, 0),
        rule(4, 5,
             f'=AND({is_action},{not_done},IFERROR(AND({date_expr}>=TODAY(),'
             f'{date_expr}<=TODAY()+{due_soon_days}),FALSE))', _AMBER, 1),
        rule(4, 5, f'=AND({is_action},$E4="")', _GREY, 2),
        rule(5, 6, f'=AND({is_action},$F4="")', _GREY, 3),
    ]


def _v2_structure_requests(sheet_id: int, due_soon_days: int) -> list[dict]:
    """Hide/tint/protect the system columns and attach the colour rules.

    The triple on the hidden columns (hide + white-on-white + warningOnly
    protection) is the same one already used on Tasks column J and the Gantt's
    tag column. Protection is warningOnly on purpose: a hard lock would also
    block the system's own writes through a user-authorised token, and the
    point is to make an accidental edit ANNOUNCE itself, not to be
    unbypassable.
    """
    reqs: list[dict] = [
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 7, "endIndex": 12},
            "properties": {"pixelSize": _HIDDEN_PX, "hiddenByUser": True},
            "fields": "pixelSize,hiddenByUser"}},
        {"repeatCell": {   # white on white — unhiding still reveals nothing useful
            "range": {"sheetId": sheet_id, "startRowIndex": 0,
                      "startColumnIndex": 7, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {
                "textFormat": {"foregroundColor": _WHITE, "fontSize": 8}}},
            "fields": "userEnteredFormat.textFormat"}},
        {"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": sheet_id, "startColumnIndex": 7,
                      "endColumnIndex": 12},
            "description": _PROTECT_DESC, "warningOnly": True}}},
    ]
    reqs.extend(_conditional_format_rules(sheet_id, due_soon_days))
    return reqs


def _checkbox_requests(sheet_id: int, rows: list[list]) -> list[dict]:
    """BOOLEAN validation on column A for CONTIGUOUS RUNS of action rows.

    Not the whole column: column A holds the '#' on a project row, and a
    numeral sitting in a checkbox cell would raise a warning triangle on every
    single project row. Blocks are contiguous, so one run per block covers it.
    """
    from services.project_status_rows import FIRST_BODY_ROW, KIND_ACTION

    reqs, start = [], None
    for idx, row in enumerate(rows + [[]]):          # sentinel flushes the tail
        kind = row[7] if len(row) > 7 else ""
        if kind == KIND_ACTION:
            start = idx if start is None else start
            continue
        if start is not None:
            reqs.append({"setDataValidation": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": FIRST_BODY_ROW - 1 + start,
                          "endRowIndex": FIRST_BODY_ROW - 1 + idx,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "rule": {"condition": {"type": "BOOLEAN"}, "strict": False}}})
            start = None
    return reqs


def _header_note_requests(sheet_id: int) -> list[dict]:
    """Hover notes on the 7 visible headers — guidance that costs no space.

    Deliberately on the header cells rather than a banner row: the working range
    stays clean, and the explanation is one hover away at the moment it is
    needed.
    """
    notes = [
        "Project rows are numbered. On an action row this is a TICK BOX — "
        "tick it when the work is done.",
        "The project name. On an action row, use this to tag the topic.",
        "The nearest concrete step. Lines marked [auto · …] were added by "
        "Gianluigi; yours are unmarked and are never overwritten.",
        "Where we want to take this project — the eventual objective. "
        "Belongs to the project row.",
        "Target date, DD/MM/YYYY. Type it any way you like (12/8, 12 Aug, "
        "next Tuesday) and it is rewritten to the standard form.",
        "Who is responsible. A project row's owner is the account owner; an "
        "action row's is whoever does that step.",
        "Free notes. Yours — the system does not write here.",
    ]
    return [{"updateCells": {
        "range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3,
                  "startColumnIndex": 0, "endColumnIndex": len(notes)},
        "rows": [{"values": [{"note": n} for n in notes]}],
        "fields": "note"}}]


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


HOWTO_TEXT = [
    ["How to use this file"],
    [""],
    ["This is the working document for the project review. Gianluigi keeps it "
     "in step with the database; what you type here goes back INTO the database."],
    [""],
    ["Layout"],
    ["Each tab is one area. Inside a tab, a PROJECT row is followed by its "
     "ACTION rows, then the next project."],
    ["  Subject   the project name          To do   where we want to take it"],
    ["  Action    the nearest concrete step  Date    when that step is due"],
    [""],
    ["What the system will and will not touch"],
    ["Lines it added are marked [auto · meeting · date]. Lines you type carry "
     "no marker, and it never edits, moves or deletes them."],
    ["It only rewrites a date you typed into the standard DD/MM/YYYY form — "
     "never the meaning, and every change is listed in the weekly summary."],
    [""],
    ["Marking work done"],
    ["Tick the box in the first column of an action row. That sets the task to "
     "done everywhere — this file, the Tasks sheet, and anything Gianluigi says."],
    ["DELETING a row is different: it only removes the line from THIS review. "
     "The task stays live and still gets chased. Tick to finish it, delete to "
     "hide it."],
    [""],
    ["Adding things"],
    ["  A new action     type it on a blank row under a project"],
    ["  A new project    type a name in Subject with the Action cell empty"],
    ["  A topic tag      type it in Subject on an action row"],
    ["  A new person     type the name in Resp. — Eyal is asked to confirm "
     "them before they are added to the team"],
    [""],
    ["Colours"],
    ["  red     past its date        amber   due within a week"],
    ["  grey    no date, or nobody responsible"],
    [""],
    ["If something looks wrong, nothing is lost — every change is reversible. "
     "Tell Eyal rather than trying to repair the file by hand."],
]


async def write_project_status_blocks(pack: dict, title_blocks: dict,
                                      spreadsheet_id: str = "",
                                      due_soon_days: int = 7) -> dict:
    """Rebuild the workbook in the v2 BLOCK layout. THE CUTOVER PATH ONLY.

    This clears each tab, which is exactly what v2 exists to stop doing. It is
    correct here and nowhere else: at cutover every row is still derived data,
    re-derivable from tasks/canonical_projects, and there is nothing human in
    the file to lose (verified against the workbook's revision history before
    running). From the moment this returns, the sheet is Nechama's and only the
    reconcile engine may touch it — incrementally, and never by clearing.

    The caller MUST seed sheet_snapshots from what this actually wrote.
    Skipping that is the expensive mistake: with no merge base the first
    reconcile sees every cell as divergent, mass-pulls the whole sheet into the
    database and marks every field sticky.
    """
    from processors.project_status import tab_name_for
    from services.project_status_rows import ALL_HEADERS

    svc = sheets_service
    ssid = spreadsheet_id or settings.PROJECT_STATUS_SHEET_ID
    result = {"spreadsheet_id": ssid, "tabs": [], "rows": 0, "projects": 0,
              "actions": 0, "url": "", "error": None}
    if not ssid:
        result["error"] = "PROJECT_STATUS_SHEET_ID not configured"
        return result

    try:
        def _meta() -> list[dict]:
            return svc._execute_with_retry(
                lambda: svc.service.spreadsheets().get(
                    spreadsheetId=ssid,
                    fields=("sheets(properties(sheetId,title),conditionalFormats,"
                            "protectedRanges(protectedRangeId,description))"))
            ).get("sheets", [])

        def _tabs() -> dict:
            return {s["properties"]["title"]: s["properties"]["sheetId"]
                    for s in _meta()}

        existing = _tabs()
        wanted = [tab_name_for(a) for a in pack]
        add = [{"addSheet": {"properties": {"title": t}}}
               for t in [HOWTO_TAB, *wanted] if t not in existing]
        if add:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().batchUpdate(
                    spreadsheetId=ssid, body={"requests": add}))
            existing = _tabs()

        # DD/MM/YYYY natively, so a date the sheet formats itself agrees with
        # what we write and with what the colour rules parse.
        struct: list[dict] = [{"updateSpreadsheetProperties": {
            "properties": {"locale": "en_GB"}, "fields": "locale"}}]
        data: list[dict] = []

        # Clear what a previous run of THIS function added, so re-running after a
        # hiccup doesn't stack four more colour rules and a second protected
        # range on every tab. Same delete-then-re-add idempotence the Tasks sheet
        # uses, keyed on our own description. Deletes go first in the batch, and
        # rule indices descend so earlier deletions don't shift later ones.
        for sheet in _meta():
            sid_ = sheet["properties"]["sheetId"]
            for idx in reversed(range(len(sheet.get("conditionalFormats") or []))):
                struct.append({"deleteConditionalFormatRule":
                               {"sheetId": sid_, "index": idx}})
            for pr in sheet.get("protectedRanges") or []:
                if pr.get("description") == _PROTECT_DESC:
                    struct.append({"deleteProtectedRange":
                                   {"protectedRangeId": pr["protectedRangeId"]}})

        for area, rows in pack.items():
            tab = tab_name_for(area)
            sheet_id = existing.get(tab)
            if sheet_id is None:
                logger.warning(f"[project-status] tab missing after create: {tab}")
                continue
            b = title_blocks.get(area, {})
            pad = [""] * (len(ALL_HEADERS) - 7)
            values = [
                [b.get("title", area), "", "", b.get("confidentiality", ""), "", "", "", *pad],
                [b.get("distribution", ""), "", "", b.get("generated", ""), "", "", "", *pad],
                list(ALL_HEADERS),
                *rows,
            ]
            svc._execute_with_retry(
                lambda t=tab: svc.service.spreadsheets().values().clear(
                    spreadsheetId=ssid, range=t, body={}))
            data.append({"range": f"'{tab}'!A1", "values": values})

            struct.extend(_tab_format_requests(sheet_id))
            struct.extend(_v2_structure_requests(sheet_id, due_soon_days))
            struct.extend(_checkbox_requests(sheet_id, rows))
            struct.extend(_header_note_requests(sheet_id))
            result["tabs"].append(tab)
            result["rows"] += len(rows)
            result["projects"] += sum(1 for r in rows if len(r) > 7 and r[7] == "P")
            result["actions"] += sum(1 for r in rows if len(r) > 7 and r[7] == "A")

        howto_id = existing.get(HOWTO_TAB)
        if howto_id is not None:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().values().clear(
                    spreadsheetId=ssid, range=HOWTO_TAB, body={}))
            data.append({"range": f"'{HOWTO_TAB}'!A1", "values": HOWTO_TEXT})
            struct.append(_column_width_request(howto_id, 0, 760))
            struct.append({"updateSheetProperties": {
                "properties": {"sheetId": howto_id, "index": 0},
                "fields": "index"}})

        if data:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=ssid,
                    body={"valueInputOption": "RAW", "data": data}))
        if struct:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().batchUpdate(
                    spreadsheetId=ssid, body={"requests": struct}))

        # Tabs v1 left behind: the 'General' bucket (never an area — v2 files
        # every task under a real project) and a locale-named default like
        # 'גיליון2'. Leaving them looks like live content that nothing updates,
        # which is worse than removing them. Safe only because the caller has
        # already taken the Drive backup — hence cutover-only.
        keep = {HOWTO_TAB, *result["tabs"]}
        stale = {t: s for t, s in _tabs().items() if t not in keep}
        if stale:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().batchUpdate(
                    spreadsheetId=ssid,
                    body={"requests": [{"deleteSheet": {"sheetId": s}}
                                       for s in stale.values()]}))
            result["removed_tabs"] = sorted(stale)
            logger.info(f"[project-status] removed stale tab(s): {sorted(stale)}")

        result["url"] = f"https://docs.google.com/spreadsheets/d/{ssid}/edit"
        logger.info(
            f"[project-status] v2 cutover wrote {result['projects']} project(s) "
            f"and {result['actions']} action(s) across {len(result['tabs'])} tab(s)")
    except Exception as e:                                  # noqa: BLE001
        result["error"] = str(e)
        logger.error(f"[project-status] v2 cutover failed: {e}")
    return result


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
