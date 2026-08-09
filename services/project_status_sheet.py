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
from services.project_status_rows import (
    ALL_HEADERS, VISIBLE_HEADERS, COL_ACTION, COL_COMMENTS, COL_DATE, COL_KIND,
    COL_NUM, COL_PRIORITY, COL_PROJECT, COL_RESP, COL_TODO, COL_TOPIC,
    FIRST_BODY_ROW, PRIORITIES,
)

logger = logging.getLogger(__name__)

# EVERY POSITION IS DERIVED FROM THE HEADER LIST. This module used to write
# column numbers out by hand — `endColumnIndex: 7`, `$H4="A"`, `$E4` for the
# date — which is fine until the layout gains a column. Adding `Project` and
# `Priority` on 2026-08-09 moved five of the nine visible columns and both
# hidden blocks, so every one of those literals would have painted, protected
# and tested the WRONG cell, silently. Deriving them means the header list is
# the single declaration of the layout.
N_VISIBLE = len(VISIBLE_HEADERS)
N_ALL = len(ALL_HEADERS)
IDX = {name: i for i, name in enumerate(ALL_HEADERS)}
KIND_IDX = IDX[COL_KIND]


def _a1(col_index: int) -> str:
    """0-based column index -> its spreadsheet letter ('A', 'J', 'AA')."""
    letters, n = "", col_index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            return letters


def _kind_of(row) -> str:
    """The `_kind` cell of a built row, tolerant of a short row."""
    return row[KIND_IDX] if len(row) > KIND_IDX else ""

# Template fidelity — colours/widths read out of the supplied .xlsx.
_HEADER_BG = _hex_color("#0070C0")   # blue band, row 3
_TITLE_BG = _hex_color("#C00000")    # red title cell, A1
_WHITE = _hex_color("#FFFFFF")
# Excel char widths from the template (4.9/28/49/48.1/13.9/10.9/30.6) -> px
# (px ≈ chars*7 + 5), re-apportioned for the nine-column layout. Project and
# Topic split the old Subject width; Priority is a narrow dropdown.
_COL_PX = [39, 190, 150, 330, 300, 102, 90, 78, 200]

WORKBOOK_TITLE = "CropSight — Projects Status"

# v2 block layout ------------------------------------------------------------
_GREY = _hex_color("#EFEFEF")        # missing date / missing owner
_RED = _hex_color("#F4C7C3")         # past due
_AMBER = _hex_color("#FCE8B2")       # due soon
_BAD_DATE = _hex_color("#E5D0F0")    # typed a date nothing can read
_PROJECT_BG = _hex_color("#EAF1F8")  # project row band
_MARKER_GREY = _hex_color("#6B6B6B")  # the [auto · …] provenance chip
_INK = _hex_color("#212121")         # ordinary body text, asserted not assumed
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

    Every rule is scoped to action rows via the hidden `_kind` column, so a
    project row is never coloured. Because they are sheet-level rules over
    an open-ended range they survive inserts, deletes and re-ordering — no
    per-row API calls, and nothing to re-apply when Nechama moves a line.
    """
    # The letters come from the header list, so inserting a column re-derives
    # them instead of silently colouring the wrong cells.
    num_a1 = _a1(IDX[COL_NUM])
    date_a1 = _a1(IDX[COL_DATE])
    resp_a1 = _a1(IDX[COL_RESP])
    kind_a1 = _a1(KIND_IDX)
    date_expr = _ddmmyyyy(date_a1)
    is_action = f'${kind_a1}4="A"'
    not_done = f"NOT(${num_a1}4=TRUE)"

    def rule(col_start, col_end, formula, colour, index, fmt=None):
        return {"addConditionalFormatRule": {"index": index, "rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": 3,
                        "startColumnIndex": col_start, "endColumnIndex": col_end}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": formula}]},
                "format": fmt or {"backgroundColor": colour}}}}}

    return [
        # THE PROJECT BAND IS A RULE, NOT PER-ROW PAINT. Eyal: "I want the new
        # project distinctions to also work once we add manually or through
        # meeting transcriptions — every time content is added there."
        # A conditional rule keyed on the hidden _kind column styles ANY row
        # that is a project, the instant it becomes one, forever — a row she
        # types at 10am looks like a project block head immediately, with no
        # cycle, no API call and nothing to re-apply. Per-row painting could
        # only ever catch up later. The heavy top rule still comes from
        # _project_row_requests, because conditional formatting cannot set
        # borders; the band is what carries the distinction. [2026-08-07]
        rule(0, N_VISIBLE, f'=${kind_a1}4="P"', None, 0,
             fmt={"backgroundColor": _PROJECT_BG, "textFormat": {"bold": True}}),
        # Order matters: the first matching rule wins.
        #
        # UNREADABLE DATE GOES FIRST. A cell the system cannot parse is never
        # pulled — which is what stops a typo nulling a real deadline — but that
        # silence was indistinguishable from success: Eyal typed a bad date and
        # nothing told him. It cannot be flagged by the other rules either,
        # because they IFERROR to FALSE and simply leave it uncoloured. Its own
        # colour, distinct from past-due and due-soon, is the only feedback the
        # sheet can give at the moment of typing. [2026-08-07]
        # Purple = "nothing could read this". It must agree with what the SYSTEM
        # can read, or it lies. The explicit DD/MM/YYYY slice only understands
        # the canonical form, so "12 Aug" — which the parser now accepts — was
        # being painted as unreadable until the next cycle canonicalised it.
        # Telling someone their correct input is wrong, then silently fixing it,
        # is worse than saying nothing. DATEVALUE covers everything Sheets
        # itself understands in en_GB (12 Aug, 12 August, 12/8), so a cell is
        # only flagged when BOTH fail. [2026-08-07]
        rule(IDX[COL_DATE], IDX[COL_DATE] + 1,
             f'=AND({is_action},${date_a1}4<>"",ISERROR({date_expr}),'
             f'ISERROR(DATEVALUE(${date_a1}4)))', _BAD_DATE, 1),
        rule(IDX[COL_DATE], IDX[COL_DATE] + 1,
             f'=AND({is_action},{not_done},IFERROR({date_expr}<TODAY(),FALSE))',
             _RED, 2),
        rule(IDX[COL_DATE], IDX[COL_DATE] + 1,
             f'=AND({is_action},{not_done},IFERROR(AND({date_expr}>=TODAY(),'
             f'{date_expr}<=TODAY()+{due_soon_days}),FALSE))', _AMBER, 3),
        rule(IDX[COL_DATE], IDX[COL_DATE] + 1,
             f'=AND({is_action},${date_a1}4="")', _GREY, 4),
        rule(IDX[COL_RESP], IDX[COL_RESP] + 1,
             f'=AND({is_action},${resp_a1}4="")', _GREY, 5),
        # Urgent stands out on sight. Scoped to the Priority cell only, so it
        # tints the chip rather than shouting across the whole row.
        rule(IDX[COL_PRIORITY], IDX[COL_PRIORITY] + 1,
             f'=AND({is_action},{not_done},'
             f'${_a1(IDX[COL_PRIORITY])}4="Urgent")', _RED, 6),
    ]


def _project_row_requests(sheet_id: int, rows: list) -> list[dict]:
    """Make a project row look like the head of a block, not another action.

    Eyal, first look at the live file: "we don't have a clear distinction
    between subjects and action rows". The template gives both the same
    formatting, so a 28-row tab reads as one flat list and the block structure —
    the whole point of v2 — is invisible.

    ONLY THE BORDER IS PAINTED HERE. The band and the bold come from the
    conditional rule keyed on `_kind = "P"`, and deliberately so: a STATIC
    background is inherited by any row inserted beneath it, whether the system
    inserts with inheritFromBefore or Nechama presses "insert row" in the UI. So
    the first action row under every project arrived tinted and bold, looking
    like a second heading — Eyal hit it in his first session and fixed it by
    hand.

    Patching the format reset would only have covered the system's own inserts;
    hers would still inherit. A conditional rule is evaluated per row against
    that row's own kind, so nothing can inherit it and there is no reset to
    forget. The border stays static only because conditional formatting cannot
    set borders. [2026-08-08]
    """
    from services.project_status_rows import FIRST_BODY_ROW, KIND_PROJECT

    reqs: list[dict] = []
    for idx, row in enumerate(rows):
        if _kind_of(row) != KIND_PROJECT:
            continue
        r = FIRST_BODY_ROW - 1 + idx
        reqs.append({"updateBorders": {
            "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": 0, "endColumnIndex": N_VISIBLE},
            "top": {"style": "SOLID_THICK",
                    "color": {"red": 0.1, "green": 0.3, "blue": 0.5}}}})
    return reqs


def _marker_bold_requests(sheet_id: int, rows: list) -> list[dict]:
    """Bold the `[auto · meeting · date]` chip inside an action's text.

    Eyal asked for the provenance to stand out. It cannot be done with cell
    formatting — that applies to the whole cell — so this uses textFormatRuns,
    which style RANGES OF CHARACTERS within one cell.

    `fields: "textFormatRuns"` means the value itself is untouched, so this can
    run after the values pass without rewriting a single word. The marker is
    bold AND grey: bold to find it, grey so it still reads as metadata rather
    than shouting over the action text.
    """
    from services.project_status_rows import FIRST_BODY_ROW, KIND_ACTION

    col = IDX[COL_ACTION]
    reqs: list[dict] = []
    for idx, row in enumerate(rows):
        if _kind_of(row) != KIND_ACTION:
            continue
        text = str(row[col] or "")
        start = text.rfind("[")
        if start <= 0:
            continue
        r = FIRST_BODY_ROW - 1 + idx
        reqs.append({"updateCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "rows": [{"values": [{"textFormatRuns": [
                {"startIndex": 0, "format": {"bold": False}},
                {"startIndex": start,
                 "format": {"bold": True, "foregroundColor": _MARKER_GREY}},
            ]}]}],
            "fields": "textFormatRuns"}})
    return reqs


def _date_validation_requests(sheet_id: int, rows: list) -> list[dict]:
    """Warn on a Date cell nothing can read, at the moment it is typed.

    Colour tells you afterwards; the warning triangle tells you now. strict is
    False throughout (house convention) so a system-written value can never hard
    -error a cell — this warns, it does not block.
    """
    from services.project_status_rows import FIRST_BODY_ROW, KIND_ACTION

    col = IDX[COL_DATE]
    reqs, start = [], None
    for idx, row in enumerate(rows + [[]]):
        kind = _kind_of(row)
        if kind == KIND_ACTION:
            start = idx if start is None else start
            continue
        if start is not None:
            reqs.append({"setDataValidation": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": FIRST_BODY_ROW - 1 + start,
                          "endRowIndex": FIRST_BODY_ROW - 1 + idx,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rule": {"condition": {"type": "DATE_IS_VALID"},
                         "inputMessage": ("Any format works — 12/8, 12 Aug, "
                                          "next Tuesday. It is rewritten to "
                                          "DD/MM/YYYY. Purple means nothing "
                                          "could read it."),
                         "strict": False}}})
            start = None
    return reqs


def _v2_structure_requests(sheet_id: int, due_soon_days: int) -> list[dict]:
    """Hide/tint/protect the system columns and attach the colour rules.

    Hidden + white-on-white + a WARNING, deliberately not a hard lock.

    Eyal asked to lock these columns properly — they carry row identity, and
    overwriting `_uid` makes the engine stop recognising the row. So they were
    locked, with the bot as sole editor. That broke the file: dragging a row and
    deleting a row BOTH touch the row's hidden cells, and Google Sheets cannot
    tell "typing into a protected column" apart from "moving a row that happens
    to contain one". Eyal hit it immediately on checks 15 and 16 — the two most
    important gestures in a review, reorder and remove, were refused outright.

    So: warningOnly. A deliberate edit gets an "are you sure" it can accept, and
    row operations keep working. The columns are hidden and white-on-white
    anyway, so reaching them at all takes intent, and the engine defends itself
    against the damage a broken identity could do: a uid it does not recognise
    is a GHOST and is left alone, a duplicated one keeps the topmost row, and a
    create re-reads its row before adopting it. Protection here is a seatbelt,
    not a lock — the same choice already made for Tasks column J. [2026-08-07]
    """
    protection = {"warningOnly": True}
    reqs: list[dict] = [
        # ASSERT THE WHOLE STATE, DON'T ADD TO IT. These three requests used to
        # say only "hide/tint/protect the system block" — nothing ever said what
        # should be VISIBLE. That is fine until the block moves, and on
        # 2026-08-09 it moved: adding Project and Priority pushed the hidden
        # block from columns 8-12 to 10-14, so the two columns it vacated kept
        # the old hidden + white-on-white-8pt styling and Priority and Comments
        # were simply not there. Every value was correct in the sheet and in the
        # database; you just could not see either column.
        #
        # An operation that should set a state must describe the whole state, or
        # it silently inherits whatever the last version left behind. Written
        # this way it is idempotent and self-correcting: run it on any older
        # layout and the sheet ends up right.
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": N_VISIBLE},
            "properties": {"hiddenByUser": False},
            "fields": "hiddenByUser"}},
        # The body starts PLAIN — not bold, no band, readable ink. The project
        # band and its bold come back on top from the conditional rule, which
        # asks each row what kind it is.
        #
        # This also clears paint left by an OLDER layout. `values().clear()`
        # removes values and nothing else, so when the rebuild rewrote the tabs
        # the static band that used to mark project rows stayed at those row
        # NUMBERS — and a task now sitting there rendered bold and tinted, which
        # is exactly the "no clear distinction between subjects and actions"
        # problem the band exists to solve, inverted. Eyal spotted four of them
        # on one tab. [2026-08-09]
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": FIRST_BODY_ROW - 1,
                      "startColumnIndex": 0, "endColumnIndex": N_VISIBLE},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _WHITE,
                "textFormat": {"bold": False, "foregroundColor": _INK,
                               "fontSize": 11}}},
            "fields": ("userEnteredFormat(backgroundColor,"
                       "textFormat(bold,foregroundColor,fontSize))")}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": N_VISIBLE, "endIndex": N_ALL},
            "properties": {"pixelSize": _HIDDEN_PX, "hiddenByUser": True},
            "fields": "pixelSize,hiddenByUser"}},
        {"repeatCell": {   # white on white — unhiding still reveals nothing useful
            "range": {"sheetId": sheet_id, "startRowIndex": 0,
                      "startColumnIndex": N_VISIBLE, "endColumnIndex": N_ALL},
            "cell": {"userEnteredFormat": {
                "textFormat": {"foregroundColor": _WHITE, "fontSize": 8}}},
            "fields": "userEnteredFormat.textFormat"}},
        {"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": sheet_id, "startColumnIndex": N_VISIBLE,
                      "endColumnIndex": N_ALL},
            "description": _PROTECT_DESC, **protection}}},
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
        kind = _kind_of(row)
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


def _priority_requests(sheet_id: int, rows: list[list]) -> list[dict]:
    """A Priority dropdown over each contiguous run of action rows.

    Same shape as the checkbox: not the whole column, because a project row has
    no priority and an empty cell under a strict-looking dropdown reads as a
    field somebody forgot to fill.

    strict=False by house convention — this offers the four values and shows a
    warning triangle on anything else, rather than refusing the keystroke. The
    reconcile then declines to pull an off-vocabulary cell, so a typo cannot
    reach the database either way.
    """
    from services.project_status_rows import FIRST_BODY_ROW, KIND_ACTION

    col = IDX[COL_PRIORITY]
    reqs, start = [], None
    for idx, row in enumerate(list(rows) + [[]]):     # sentinel flushes the tail
        if _kind_of(row) == KIND_ACTION:
            start = idx if start is None else start
            continue
        if start is not None:
            reqs.append({"setDataValidation": {
                "range": {"sheetId": sheet_id,
                          "startRowIndex": FIRST_BODY_ROW - 1 + start,
                          "endRowIndex": FIRST_BODY_ROW - 1 + idx,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rule": {"condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in PRIORITIES]},
                    "showCustomUi": True, "strict": False}}})
            start = None
    return reqs


def _howto_format_requests(sheet_id: int) -> list[dict]:
    """Make the guidance tab look like a reference card, not a text file."""
    def band(row, colour, bold, size, col_end=2):
        return {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": row,
                      "endRowIndex": row + 1, "startColumnIndex": 0,
                      "endColumnIndex": col_end},
            "cell": {"userEnteredFormat": {
                "backgroundColor": colour,
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
                "textFormat": {"bold": bold, "fontSize": size,
                               "foregroundColor": _WHITE if bold and size >= 13
                               else {"red": 0.13, "green": 0.13, "blue": 0.13}}}},
            "fields": ("userEnteredFormat(backgroundColor,verticalAlignment,"
                       "wrapStrategy,textFormat)")}}

    reqs = [
        _column_width_request(sheet_id, 0, 250),
        _column_width_request(sheet_id, 1, 620),
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
    ]
    for idx, (kind, _a, _b) in enumerate(HOWTO_BLOCK):
        if kind == "title":
            reqs.append(band(idx, _TITLE_BG, True, 20))
        elif kind == "lede":
            reqs.append(band(idx, _WHITE, False, 12))
        elif kind == "h2":
            reqs.append(band(idx, _HEADER_BG, True, 13))
        elif kind == "row":
            # Only the left column is bold — it reads as a term/definition pair.
            reqs.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": idx,
                          "endRowIndex": idx + 1, "startColumnIndex": 0,
                          "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(textFormat,verticalAlignment)"}})
            reqs.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": idx,
                          "endRowIndex": idx + 1, "startColumnIndex": 1,
                          "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                               "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})
        elif kind == "note":
            reqs.append(band(idx, _AMBER, False, 11))
    return reqs


def new_row_format_requests(sheet_id: int, row_number: int, kind: str,
                            action_text: str = "") -> list[dict]:
    """Everything a row needs the moment it becomes ours, at a known row number.

    The conditional rules already handle colour and the project band without
    being asked. What they cannot do is a checkbox, a date validation or a bold
    provenance chip — those are per-cell and have to be applied to the specific
    row. Called right after a create or an injection so a line typed at 10am is
    tickable at 10:30 rather than at the next structural pass. [2026-08-07]
    """
    from services.project_status_rows import KIND_ACTION, KIND_PROJECT

    r = row_number - 1
    if kind == KIND_PROJECT:
        return [{"updateBorders": {
            "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": 0, "endColumnIndex": N_VISIBLE},
            "top": {"style": "SOLID_THICK",
                    "color": {"red": 0.1, "green": 0.3, "blue": 0.5}}}}]
    if kind != KIND_ACTION:
        return []

    date_col = IDX[COL_DATE]
    act_col = IDX[COL_ACTION]
    prio_col = IDX[COL_PRIORITY]
    reqs = [
        {"setDataValidation": {
            "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": prio_col, "endColumnIndex": prio_col + 1},
            "rule": {"condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": v} for v in PRIORITIES]},
                "showCustomUi": True, "strict": False}}},
        {"setDataValidation": {
            "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": False}}},
        {"setDataValidation": {
            "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": date_col, "endColumnIndex": date_col + 1},
            "rule": {"condition": {"type": "DATE_IS_VALID"},
                     "inputMessage": ("Any format works — 12/8, 12 Aug, next "
                                      "Tuesday. It is rewritten to DD/MM/YYYY. "
                                      "Purple means nothing could read it."),
                     "strict": False}}},
        # A row she typed carries the project row's band until told otherwise,
        # because an insert inherits from above.
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": 0, "endColumnIndex": N_VISIBLE},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": False},
                "wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
            "fields": ("userEnteredFormat(textFormat.bold,wrapStrategy,"
                       "verticalAlignment)")}},
    ]
    start = str(action_text or "").rfind("[")
    if start > 0:
        reqs.append({"updateCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": act_col, "endColumnIndex": act_col + 1},
            "rows": [{"values": [{"textFormatRuns": [
                {"startIndex": 0, "format": {"bold": False}},
                {"startIndex": start,
                 "format": {"bold": True, "foregroundColor": _MARKER_GREY}},
            ]}]}],
            "fields": "textFormatRuns"}})
    return reqs


def _header_note_requests(sheet_id: int) -> list[dict]:
    """Hover notes on the visible headers — guidance that costs no space.

    Deliberately on the header cells rather than a banner row: the working range
    stays clean, and the explanation is one hover away at the moment it is
    needed.

    KEYED BY COLUMN NAME, not written out in order. A positional list would put
    the wrong note on the wrong header the first time the layout changed, which
    is the one thing a hover note must never do.
    """
    notes_by_col = {
        COL_NUM: "Project rows are numbered 1, 2, 3… down the tab. On an ACTION "
                 "row this is a TICK BOX — tick it when the work is done, and "
                 "the row leaves the sheet overnight.",
        COL_PROJECT: "Fill this in and the row IS a project. Leave it empty on "
                     "action rows. Renaming here is undone — rename on the "
                     "Projects tab, which updates everything else too.",
        COL_TOPIC: "Optional tag for an action row — what this step is about "
                   "(a client, a region, a workstream).",
        COL_ACTION: "Fill this in and the row IS an action. The nearest "
                    "concrete step. Lines marked [auto · …] were added by "
                    "Gianluigi; yours are unmarked and never overwritten.",
        COL_TODO: "Where we want to take this project — the eventual "
                  "objective. Belongs to the project row.",
        COL_DATE: "Target date, DD/MM/YYYY. Type it any way you like (12/8, "
                  "12 Aug, next Tuesday) and it is rewritten to the standard "
                  "form. Purple means nothing could read it.",
        COL_RESP: "Who is responsible. A project row's owner owns the project; "
                  "an action row's owner does that step.",
        COL_PRIORITY: "Urgent · H · M · L. Pick from the list — anything else "
                      "is left in the cell but not saved.",
        COL_COMMENTS: "Free notes. Yours — the system does not write here.",
    }
    notes = [notes_by_col.get(name, "") for name in VISIBLE_HEADERS]
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
                      "startColumnIndex": 0, "endColumnIndex": N_VISIBLE},
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
                      "startColumnIndex": 1, "endColumnIndex": N_VISIBLE},
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
                      "startColumnIndex": 0, "endColumnIndex": N_VISIBLE},
            "bottom": {"style": "SOLID_MEDIUM",
                       "color": {"red": 0, "green": 0, "blue": 0}}}},
    ]
    for idx, px in enumerate(_COL_PX):
        reqs.append(_column_width_request(sheet_id, idx, px))
    return reqs


# (kind, column A, column B). `kind` drives the formatting below:
#   title / lede / h2 / row / note / blank
#
# Two columns, not one wall of prose. Eyal's review of the first version: "the
# instructions are clear but it looks too simple — people will avoid reading it
# properly." A page that looks like a plain-text file gets skimmed and then
# ignored, so this is laid out as a reference table with a coloured header
# band per section, matching the area tabs it explains. [2026-08-07]
HOWTO_BLOCK = [
    ("title", "How to use this file", ""),
    ("lede", "The working document for the project review. Gianluigi keeps it in "
             "step with the database — and what you type here goes back INTO the "
             "database.", ""),
    ("blank", "", ""),

    ("h2", "THE LAYOUT", ""),
    ("row", "Each tab is one area", "Inside it: a PROJECT row, then its ACTION "
                                    "rows, then the next project."),
    ("row", "Project", "Fill it in and the row IS a project. Empty on action "
                       "rows."),
    ("row", "Action", "Fill it in and the row IS an action — the nearest "
                      "concrete step. Empty on project rows."),
    ("row", "Topic", "Optional tag on an action row: what the step is about."),
    ("row", "To do", "Where we want to take this project — the eventual aim."),
    ("row", "Date · Resp. · Priority", "When that step is due, who owns it, and "
                                       "how it ranks (Urgent · H · M · L)."),
    ("blank", "", ""),

    ("h2", "WHAT THE SYSTEM WILL AND WILL NOT TOUCH", ""),
    ("row", "Lines it added", "Marked [auto · meeting · date] in bold."),
    ("row", "Lines you typed", "No marker. It never edits, moves or deletes them."),
    ("row", "The one exception", "It rewrites a date you typed into DD/MM/YYYY. "
                                 "The format only, never the meaning — and every "
                                 "one is listed in the weekly summary."),
    ("blank", "", ""),

    ("h2", "FINISHING WORK  vs  HIDING IT", ""),
    ("row", "Tick the box", "Marks the task done EVERYWHERE — here, the Tasks "
                            "sheet, and anything Gianluigi tells you."),
    ("row", "Delete the row", "Removes the line from THIS review only. The task "
                              "stays live and still gets chased."),
    ("note", "Two different intentions, two different gestures. Deleting is never "
             "how you finish something.", ""),
    ("blank", "", ""),

    ("h2", "ADDING THINGS", ""),
    ("row", "A new action", "Type it on a blank row under a project."),
    ("row", "A new project", "Type the name in the PROJECT column. That is what "
                             "makes the row a project — nothing is guessed."),
    ("row", "A topic tag", "Type it in TOPIC on an action row."),
    ("row", "A priority", "Pick Urgent · H · M · L from the dropdown."),
    ("row", "A new person", "Type the name in Resp. Eyal is asked to confirm them "
                            "before they join the team."),
    ("blank", "", ""),

    ("h2", "THE COLOURS", ""),
    ("row", "Red", "Past its date."),
    ("row", "Amber", "Due within a week."),
    ("row", "Grey", "No date, or nobody responsible."),
    ("row", "Purple", "The date could not be read — retype it."),
    ("blank", "", ""),

    ("h2", "DATES", ""),
    ("row", "Type them any way", "12/8 · 12 Aug · next Tuesday · 2026-08-12. All "
                                 "are understood and rewritten to DD/MM/YYYY."),
    ("row", "05/08 is 5 August", "Day first, not month. The whole file is "
                                 "day-first."),
    ("blank", "", ""),

    ("note", "Nothing here is ever lost — every change is reversible, and the "
             "system keeps its own copy. If something looks wrong, tell Eyal "
             "rather than repairing the file by hand.", ""),
]

HOWTO_TEXT = [[a, b] for _kind, a, b in HOWTO_BLOCK]


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
            # The two banner rows are as wide as the sheet; the second cell of
            # each carries the right-hand text. Both offsets are derived so a
            # column change cannot leave the confidentiality line hanging in the
            # middle of the table.
            def banner(left, right):
                cells = [""] * len(ALL_HEADERS)
                cells[0] = left
                cells[IDX[COL_ACTION]] = right
                return cells

            values = [
                banner(b.get("title", area), b.get("confidentiality", "")),
                banner(b.get("distribution", ""), b.get("generated", "")),
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
            struct.extend(_priority_requests(sheet_id, rows))
            struct.extend(_header_note_requests(sheet_id))
            # AFTER the base formatting: the project band and the bold marker
            # both override what _tab_format_requests paints across the body,
            # and requests apply in order.
            struct.extend(_project_row_requests(sheet_id, rows))
            struct.extend(_marker_bold_requests(sheet_id, rows))
            struct.extend(_date_validation_requests(sheet_id, rows))
            result["tabs"].append(tab)
            result["rows"] += len(rows)
            result["projects"] += sum(1 for r in rows if _kind_of(r) == "P")
            result["actions"] += sum(1 for r in rows if _kind_of(r) == "A")

        howto_id = existing.get(HOWTO_TAB)
        if howto_id is not None:
            svc._execute_with_retry(
                lambda: svc.service.spreadsheets().values().clear(
                    spreadsheetId=ssid, range=HOWTO_TAB, body={}))
            data.append({"range": f"'{HOWTO_TAB}'!A1", "values": HOWTO_TEXT})
            struct.extend(_howto_format_requests(howto_id))
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
        # NON_AREA_TABS, not just the How-to. This set is what a tab is measured
        # against before being DELETED, and the meetings pool moved into this
        # workbook on 2026-08-09 — 123 rows that a re-run of the cutover script
        # would have removed without a word, because they are not area tabs and
        # not the How-to. A delete list defined by exclusion has to name every
        # legitimate resident.
        from processors.project_status_reconcile import NON_AREA_TABS
        keep = {*NON_AREA_TABS, *result["tabs"]}
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
