"""Writes the Focus tab and its hidden data tab.

Split from `processors/focus_view.py` on the house rule: the processor decides
WHAT the view contains and holds no I/O; this module only talks to Sheets.
"""

import logging

from config.settings import settings
from processors.focus_view import (
    DATA_HEADERS, DCOL_AREA, DCOL_WHO, FCOL_DONE, FCOL_DUE, FCOL_PRIORITY,
    FCOL_WHEN, FOCUS_DATA_TAB, FOCUS_HIDDEN_HEADERS, FOCUS_TAB, GROUP_CHOICES,
    HEADERS, N_FOCUS_HIDDEN, SHOW_CHOICES, build_rows, display_rows,
    focus_layout, valid_control,
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
# A PALER TINT OF TODAY, not a fourth colour. Eyal: *"i like it that today is
# colored in yellow… lets think if we should color also this week as the
# meetings with nechama will probably be weekly ones."* The weekly review is the
# operative rhythm, so THIS WEEK is the actionable set and TODAY is a subset of
# it — which is exactly what a lighter shade of the same hue says. A different
# colour would read as a different KIND of urgency and compete with it.
_WEEK_BG = {"red": 0.996, "green": 0.965, "blue": 0.878}

_PROTECT_DESC = "Gianluigi: Focus is generated — change the dropdowns, not the rows"
# ✓, When, Due, What, Who, Priority, Project, Area, Kind
_COL_PX = [34, 100, 90, 400, 140, 80, 180, 200, 70]


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


def _when_col() -> str:
    """The A1 letter of the When column, which the row-colour rules key on.

    Derived, never spelled. It moved from A to B when the Done tick took the
    first column, and a hardcoded `$A` would have coloured every row by its
    checkbox — silently, because a formula that matches nothing simply paints
    nothing. [2026-08-13]
    """
    return chr(65 + FCOL_WHEN)


def _bucket_colour_rules(sheet_id: int) -> list[dict]:
    """Colour the whole row by its When cell.

    FOUR of the six buckets, not all six. The original rule was three — late,
    today, forgotten — on the reasoning that colouring everything makes nothing
    stand out. That still holds, and THIS WEEK earns the fourth slot because the
    working rhythm is weekly: the Nechama review covers the week, so the week is
    the actionable set and TODAY is a subset of it. It is drawn as a lighter
    shade of the TODAY yellow rather than a new colour, so it reads as "the same
    thing, less imminent" instead of a competing kind of urgency.

    THIS MONTH and LATER stay white. Those are the two that would flatten the
    contrast, and neither is something you act on in a weekly meeting.
    """
    rules = []
    for idx, (text, bg) in enumerate((
        ("OVERDUE", _OVERDUE_BG), ("TODAY", _TODAY_BG), ("THIS WEEK", _WEEK_BG),
        ("NO DATE", _NODATE_BG),
    )):
        rules.append({"addConditionalFormatRule": {"index": idx, "rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": 4,
                        "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [
                    {"userEnteredValue": f'=${_when_col()}5="{text}"'}]},
                "format": {"backgroundColor": bg},
            }}}})
    return rules


def _a1(idx: int) -> str:
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _read_controls(ssid: str, owners, areas) -> dict:
    """The four dropdowns, as the server will filter on them.

    An unreadable tab yields the DEFAULTS rather than an error: the controls are
    a preference, and losing a filter setting is a far smaller harm than
    skipping the refresh and leaving a stale list on screen.
    """
    got = {}
    try:
        resp = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=ssid, range=f"'{FOCUS_TAB}'!A2:H2"))
        row = list((resp.get("values") or [[]])[0]) + [""] * 8
        got = {"group": row[1], "owner": row[3], "area": row[5], "show": row[7]}
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[focus] could not read the controls ({e}) — defaulting")
    return {k: valid_control(k, got.get(k), owners, areas)
            for k in ("group", "owner", "area", "show")}


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

    owners = sorted({r[DCOL_WHO] for r in rows if r[DCOL_WHO]})
    areas = sorted({r[DCOL_AREA] for r in rows if r[DCOL_AREA]})

    sid = await sheets_service._ensure_tab(FOCUS_TAB, HEADERS, spreadsheet_id=ssid)
    data_sid = await sheets_service._ensure_tab(
        FOCUS_DATA_TAB, DATA_HEADERS, spreadsheet_id=ssid)
    if sid is None:
        return {"error": "could not create the Focus tab"}

    # READ THE CONTROLS BEFORE OVERWRITING THEM. The server does the filtering
    # now, so these four cells are INPUTS — and rewriting the defaults every
    # refresh would drop Nechama back to "Everything open" every thirty minutes,
    # mid-meeting, while she was looking at it.
    controls = _read_controls(ssid, owners, areas)
    display = display_rows(rows, **controls)

    n_vis = len(HEADERS)
    n_cols = n_vis + N_FOCUS_HIDDEN
    grid = [(r + [""] * n_cols)[:n_cols] for r in focus_layout(controls)]
    grid.extend((list(r) + [""] * n_cols)[:n_cols] for r in display)

    # Wipe the body first. The row count changes with every filter change, and
    # values().update only overwrites what it covers — a shorter result would
    # leave the tail of the previous, longer one behind, reading as live work
    # that no longer matches the filter.
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().clear(
            spreadsheetId=ssid,
            range=f"'{FOCUS_TAB}'!A5:{_a1(n_cols - 1)}", body={}))
    # USER_ENTERED, not RAW: the header block holds formulas, and RAW would
    # store them as text that looks right and computes nothing.
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().update(
            spreadsheetId=ssid,
            range=f"'{FOCUS_TAB}'!A1:{_a1(n_cols - 1)}{len(grid)}",
            valueInputOption="USER_ENTERED", body={"values": grid}))

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

    # ---- the Done tick ---------------------------------------------------
    # A real checkbox, not the word TRUE: it is the gesture of the whole tab, and
    # a cell you have to type into is not one you use in a meeting. Wiped first
    # for the same reason every other validation is — it survives values().clear()
    # and would otherwise stay on rows that are now shorter than the last render.
    n_body = max(len(grid), 5) + 200
    reqs.append({"setDataValidation": {
        "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": n_body,
                  "startColumnIndex": 0, "endColumnIndex": n_vis}}})
    reqs.append({"setDataValidation": {
        "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": len(grid),
                  "startColumnIndex": FCOL_DONE, "endColumnIndex": FCOL_DONE + 1},
        "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True}}})

    # ---- the identity columns --------------------------------------------
    # Hidden and masked white-on-white, the way every other editable tab masks
    # its own: unhiding them still shows nothing useful, because the readback
    # depends on those cells being exactly what the renderer wrote.
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": n_vis, "endIndex": n_vis + N_FOCUS_HIDDEN},
        "properties": {"pixelSize": 120, "hiddenByUser": True},
        "fields": "pixelSize,hiddenByUser"}})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": n_body,
                  "startColumnIndex": n_vis, "endColumnIndex": n_vis + N_FOCUS_HIDDEN},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _WHITE,
            "textFormat": {"fontSize": 8, "foregroundColor": _WHITE}}},
        "fields": "userEnteredFormat(backgroundColor,textFormat)"}})

    # ---- protection: the GENERATED columns only --------------------------
    # Done, Due and Priority are the point of Phase C and must stay open. The
    # rest is rendered from the database and would be silently overwritten on
    # the next refresh, so it is protected — warningOnly, because this is a tab
    # people work in and a hard block on a stray click is its own kind of
    # unusable. The identity columns are inside the protected block too: an edit
    # there would ask the readback to adopt whatever landed. [2026-08-13]
    _READ_ONLY = ((FCOL_WHEN, FCOL_DUE),                       # When
                  (FCOL_DUE + 1, FCOL_PRIORITY),               # What, Who
                  (FCOL_PRIORITY + 1, n_vis + N_FOCUS_HIDDEN))  # Project..hidden
    for a, b in _READ_ONLY:
        reqs.append({"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": sid, "startRowIndex": 4,
                      "startColumnIndex": a, "endColumnIndex": b},
            "description": _PROTECT_DESC,
            "warningOnly": True,
        }}})

    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=ssid, body={"requests": reqs}))

    logger.info(f"[focus] rebuilt: {len(rows)} open item(s), "
                f"{len(owners)} owner(s), {len(areas)} area(s)")
    return {"rows": len(rows), "owners": len(owners), "areas": len(areas)}
