"""Writes the Timeline tab into the Project Status workbook.

Phase 2 of docs/GANTT_V2_PLAN.md. RENDER ONLY — nothing is read back from this
tab yet. Bidirectional editing is Phase 4 and ships in shadow mode first, the
way Project Status v2 did.

Split from `processors/timeline_view.py` on the house rule: the processor
decides what the timeline contains and holds no I/O; this module only talks to
Sheets.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from config.settings import settings
from processors.sheet_format import centre_and_wrap, display_date, left_align
from processors.timeline_view import (
    GHOST_BG, HEADERS, HIDDEN_HEADERS, N_HIDDEN, N_LABEL_COLS, OPEN_END_BG,
    ROW_ARCHIVE, ROW_AREA, ROW_CHROME, ROW_PROJECT, ROW_TASK, STATUS_BG,
    TIMELINE_TAB, build_timeline,
)
from services.google_sheets import sheets_service

logger = logging.getLogger(__name__)

ISRAEL_TZ = timezone(timedelta(hours=3))

TITLE_ROW, LEGEND_ROW, MONTH_ROW, WEEK_ROW = 0, 1, 2, 3
FIRST_BODY_ROW = 4

_TITLE_BG = {"red": 0.75, "green": 0.0, "blue": 0.0}
_HEADER_BG = {"red": 0.0, "green": 0.44, "blue": 0.75}
_AREA_BG = {"red": 0.918, "green": 0.945, "blue": 0.973}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_INK = {"red": 0.13, "green": 0.13, "blue": 0.13}
_TODAY = {"red": 0.85, "green": 0.33, "blue": 0.31}

_PROTECT_DESC = "Gianluigi: Timeline is generated — edit projects on the area tabs"

# Order matters: the swatch under column i is coloured from _LEGEND_KEYS[i].
# These four are the STATUS legend and must stay exactly the status palette —
# a test asserts it, because a legend that drifts from the bars is worse than
# no legend at all.
_LEGEND = ("Blocked", "Active", "Planned", "Completed / retired")
_LEGEND_KEYS = ("blocked", "active", "planned", "completed")
# The ghost swatch is NOT a status, so it is kept out of the pair above and
# painted into the one spare label column. The LABEL appears only when the
# archive was drawn; the cell's fill is asserted on every run either way.
_GHOST_LEGEND = "Old board"


def _rgb(hex_colour: str) -> dict:
    h = hex_colour.lstrip("#")
    return {"red": int(h[0:2], 16) / 255,
            "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}


def _a1_col(idx: int) -> str:
    """0-based column index -> A1 letters. The grid runs past Z (96 weeks plus
    five label columns), so the two-letter case is not optional."""
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _month_band(weeks: list[date]) -> list[str]:
    """A month name in the first column of each month, blank elsewhere."""
    out, seen = [], None
    for w in weeks:
        key = (w.year, w.month)
        out.append(w.strftime("%b %y") if key != seen else "")
        seen = key
    return out


async def refresh_timeline(spreadsheet_id: str | None = None) -> dict:
    """Rebuild the Timeline tab from the database."""
    ssid = spreadsheet_id or settings.PROJECT_STATUS_SHEET_ID
    if not ssid:
        return {"skipped": "no PROJECT_STATUS_SHEET_ID"}

    data = build_timeline()
    weeks, areas = data["weeks"], data["areas"]
    if not areas:
        # Same reasoning as the readonly-tab force_empty guard: a failed read
        # and a genuinely empty board are indistinguishable here, and blanking
        # the tab is the more expensive mistake.
        logger.warning("[timeline] no areas returned — leaving the tab alone")
        return {"skipped": "no rows"}

    # `n_week_end` bounds the VISIBLE board; `n_cols` includes the two hidden
    # identity columns beyond it. Everything that paints or wipes the board uses
    # n_week_end — a fill that ran to n_cols would colour the hidden cells and
    # make them visible the moment anyone unhid them.
    n_week_end = N_LABEL_COLS + len(weeks)
    n_cols = n_week_end + N_HIDDEN
    today = datetime.now(ISRAEL_TZ).date()
    today_col = next((N_LABEL_COLS + i for i, w in enumerate(weeks)
                      if w <= today < w + timedelta(days=7)), None)

    def _row(kind: str, uid: str = "") -> list:
        """A blank row already carrying its identity."""
        line = [""] * n_cols
        line[n_week_end] = uid
        line[n_week_end + 1] = kind
        return line

    # ---- values -----------------------------------------------------------
    grid: list[list] = [_row(ROW_CHROME) for _ in range(FIRST_BODY_ROW)]
    grid[TITLE_ROW][0] = "TIMELINE — projects on a weekly grid"
    for i, label in enumerate(_LEGEND):
        grid[LEGEND_ROW][i] = label
    show_ghost = bool(data.get("archive"))
    if show_ghost and len(_LEGEND) < N_LABEL_COLS:
        grid[LEGEND_ROW][len(_LEGEND)] = _GHOST_LEGEND
    grid[MONTH_ROW][N_LABEL_COLS:n_week_end] = _month_band(weeks)
    grid[WEEK_ROW][:N_LABEL_COLS] = HEADERS
    grid[WEEK_ROW][N_LABEL_COLS:n_week_end] = [w.strftime("%d/%m") for w in weeks]
    grid[WEEK_ROW][n_week_end:] = HIDDEN_HEADERS

    fills: list[dict] = []          # (row, col_from, col_to, colour)
    area_rows: list[int] = []
    group_spans: list[tuple[int, int]] = []   # task rows to collapse
    snapshot_rows: list[tuple] = []           # (project_id, row, start, target, owner)

    for area in sorted(areas):
        area_rows.append(len(grid))
        line = _row(ROW_AREA)
        line[0] = area
        grid.append(line)

        for proj in sorted(areas[area],
                           key=lambda p: (p["first_col"] < 0, p["first_col"])):
            row = len(grid)
            line = _row(ROW_PROJECT, proj["project_id"])
            line[0] = f"    {proj['name']}"
            line[1] = proj["owner"]
            line[2] = display_date(proj["start"])
            line[3] = display_date(proj["target"])
            line[4] = "retired" if proj["retired"] else ""
            grid.append(line)
            # The merge base is written HERE, by the render, because this is the
            # one moment the sheet and the database agree by construction.
            #
            # Without it the readback has no base at all: "no merge base means
            # no edit" would refuse every typed value, and since the readback
            # only writes snapshots on its LIVE path, shadow mode would never
            # create one either. A shadow week would show nothing happening and
            # prove nothing, and the first real edit would vanish. Found before
            # anyone typed a date. [2026-08-12]
            snapshot_rows.append((proj["project_id"], row + 1,
                                  str(proj["start"] or ""),
                                  str(proj["target"] or ""),
                                  proj["owner"] or ""))

            if proj["first_col"] >= 0:
                a = N_LABEL_COLS + proj["first_col"]
                b = N_LABEL_COLS + proj["last_col"]
                colour = STATUS_BG[proj["status"]]
                if proj["open_ended"] and today_col is not None and today_col < b:
                    # Solid to today, pale beyond: "still going, end unknown".
                    # A project starting in the future has no solid part at all
                    # — the whole run is the pale "we don't know" tint.
                    if today_col >= a:
                        fills.append({"row": row, "a": a, "b": today_col,
                                      "colour": colour})
                    fills.append({"row": row, "a": max(a, today_col + 1), "b": b,
                                  "colour": OPEN_END_BG})
                else:
                    fills.append({"row": row, "a": a, "b": b, "colour": colour})

            first_task_row = len(grid)
            for t in proj["tasks"]:
                # Task rows carry the PROJECT's uid so a stray edit can be
                # attributed, but their kind marks them read-only: Phase 4 opens
                # project rows only. tasks.deadline and tasks.assignee are edited
                # daily on the area tabs, and a second writer on those is the
                # collision this design exists to avoid.
                trow = _row(ROW_TASK, proj["project_id"])
                trow[0] = f"        └ {t['title'][:70]}"
                trow[1] = t["assignee"]
                trow[3] = display_date(t["deadline"])
                trow[4] = t["priority"]
                grid.append(trow)
            if len(grid) > first_task_row:
                group_spans.append((first_task_row, len(grid) - 1))

    # ---- the old board, redrawn on the same columns -------------------------
    # One collapsed block at the bottom rather than a ghost row per project:
    # the archive makes no claim about which project a bar belongs to, because
    # the matching that would support such a claim does not work (see
    # processors.timeline_view.legacy_archive). Collapsed by default, so the
    # answer to "it doubles what is on screen" is that it does not until asked.
    archive = data.get("archive") or []
    archive_span = archive_title_row = None
    if archive:
        line = _row(ROW_ARCHIVE)
        line[0] = "OLD BOARD — what we planned (archived, read-only)"
        grid.append(line)
        archive_title_row = len(grid) - 1
        first_archive_row = len(grid)
        for block in archive:
            area_rows.append(len(grid))
            line = _row(ROW_ARCHIVE)
            line[0] = block["section"]
            grid.append(line)
            for lane in block["lanes"]:
                line = _row(ROW_ARCHIVE)
                line[0] = f"    {lane['lane']}"
                for bar in lane["bars"]:
                    a = N_LABEL_COLS + bar["first_col"]
                    b = N_LABEL_COLS + bar["last_col"]
                    # The label goes in the bar's FIRST cell only. Sheets
                    # overflows text rightwards into empty cells, so it reads
                    # across its own bar and stops at the next one — which is
                    # how the old board labelled its bars in the first place.
                    line[a] = bar["label"][:80]
                    fills.append({"row": len(grid), "a": a, "b": b,
                                  "colour": GHOST_BG})
                grid.append(line)
        if len(grid) > first_archive_row:
            archive_span = (first_archive_row, len(grid) - 1)
            group_spans.append(archive_span)

    # ---- write ------------------------------------------------------------
    sid = await sheets_service._ensure_tab(TIMELINE_TAB, HEADERS,
                                           spreadsheet_id=ssid)
    if sid is None:
        return {"error": "could not create the Timeline tab"}

    # Assert the grid is wide enough BEFORE any ranged write. Not a defect —
    # production renders all 101 columns today, because values().update expands
    # the grid on its way past the edge. But `_ensure_tab` issues a bare
    # `addSheet`, which allocates 26 columns, so the tab is wide only by a side
    # effect nobody wrote down; three independent reviewers read this file and
    # concluded it must fail. One request makes the width a stated invariant
    # instead of an accident, and if the tab is ever recreated it no longer
    # matters which call happens to arrive first. [2026-08-12 review]
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=ssid, body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"columnCount": n_cols}},
                "fields": "gridProperties.columnCount"}}]}))

    # Clear EVERY row of the used columns, not `len(grid) + 40`. The old bound
    # was measured from the NEW render, so a refresh that shrank the tab by more
    # than 40 rows left the tail of the previous one behind — and since the fill
    # wipe below reaches 200 rows, those leftovers appeared as text with no bar
    # under it: rows that read as real projects and lanes but correspond to
    # nothing. An unbounded column range has no arithmetic to get wrong.
    # [2026-08-12 review]
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().clear(
            spreadsheetId=ssid,
            range=f"'{TIMELINE_TAB}'!A:{_a1_col(n_cols - 1)}", body={}))
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().update(
            spreadsheetId=ssid,
            range=f"'{TIMELINE_TAB}'!A1:{_a1_col(n_cols - 1)}{len(grid)}",
            valueInputOption="RAW", body={"values": grid}))

    reqs = _format_requests(sid, n_cols, len(grid), weeks, today_col,
                            fills, area_rows, group_spans, ssid, show_ghost,
                            archive_title_row, archive_span, n_week_end)
    if reqs:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=ssid, body={"requests": reqs}))

    # Merge base LAST, and only after the write succeeded — recording what the
    # sheet shows before it actually shows it would make the next cycle compare
    # against a row that was never rendered. Only when the readback is on: with
    # a read-only tab these rows are never consulted.
    snapped = 0
    if getattr(settings, "TIMELINE_READBACK_ENABLED", False):
        from services.supabase_client import supabase_client
        for project_id, row_number, start, target, owner in snapshot_rows:
            if supabase_client.upsert_timeline_snapshot(
                    project_id=project_id, sheet_row=row_number,
                    start_date=start or None, target_date=target or None,
                    owner=owner or None):
                snapped += 1

    out = {"rows": len(grid), "weeks": len(weeks), "snapshots": snapped,
           **data["stats"]}
    logger.info(f"[timeline] {out}")
    return out


def _format_requests(sid, n_cols, n_rows, weeks, today_col,
                     fills, area_rows, group_spans, ssid,
                     show_ghost: bool = False,
                     archive_title_row: int | None = None,
                     archive_span: tuple | None = None,
                     n_week_end: int | None = None) -> list[dict]:
    # Everything that PAINTS stops at the visible edge; only the grid-width
    # assertion and the hidden-column treatment go past it.
    n_week_end = n_cols - N_HIDDEN if n_week_end is None else n_week_end
    reqs: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {
                "frozenRowCount": FIRST_BODY_ROW,
                "frozenColumnCount": N_LABEL_COLS}},
            "fields": "gridProperties(frozenRowCount,frozenColumnCount)"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": TITLE_ROW,
                      "endRowIndex": TITLE_ROW + 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _TITLE_BG,
                "textFormat": {"bold": True, "fontSize": 12,
                               "foregroundColor": _WHITE}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": WEEK_ROW,
                      "endRowIndex": WEEK_ROW + 1,
                      "startColumnIndex": 0, "endColumnIndex": n_week_end},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": _WHITE},
                "horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}},
    ]

    # The legend swatches, coloured from the old board's own palette.
    for i, key in enumerate(_LEGEND_KEYS):
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": LEGEND_ROW,
                      "endRowIndex": LEGEND_ROW + 1,
                      "startColumnIndex": i, "endColumnIndex": i + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb(STATUS_BG[key]),
                "textFormat": {"fontSize": 9, "foregroundColor": _INK}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}})

    # ASSERT, don't add: this cell is painted on EVERY run, white when there is
    # no ghost layer. Writing it only when show_ghost would leave a lilac swatch
    # with no text behind forever the first time the overlay is turned off —
    # values().clear() takes the label away and never the fill.
    if len(_LEGEND) < N_LABEL_COLS:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": LEGEND_ROW,
                      "endRowIndex": LEGEND_ROW + 1,
                      "startColumnIndex": len(_LEGEND),
                      "endColumnIndex": len(_LEGEND) + 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb(GHOST_BG) if show_ghost else _WHITE,
                "textFormat": {"fontSize": 9, "foregroundColor": _INK}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}})

    # Wipe the whole body to white BEFORE painting anything. A bar that moved or
    # shortened since the last refresh leaves its old cells coloured otherwise —
    # values().clear() removes text, never formatting, so the ghost of the
    # previous plan stays on the board looking like current truth.
    #
    # It starts at column 0, not N_LABEL_COLS. Area headers and the archive title
    # are painted across ALL columns, so a wipe that began at the week grid left
    # their blue-grey and lilac fills sitting in columns A-E at whatever row they
    # occupied last time — stray coloured bands labelling nothing. textFormat is
    # reset for the same reason: those rows are bold, and the archive rows carry
    # a smaller font. Everything that needs either is re-applied below.
    # [2026-08-12 review]
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": FIRST_BODY_ROW,
                  "endRowIndex": max(n_rows, FIRST_BODY_ROW) + 200,
                  "startColumnIndex": 0, "endColumnIndex": n_week_end},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _WHITE,
            "textFormat": {"bold": False, "fontSize": 10,
                           "foregroundColor": _INK}}},
        "fields": "userEnteredFormat(backgroundColor,textFormat)"}})

    # Clear the previous today-marker before drawing this week's. updateBorders
    # is not touched by the fill wipe above — borders live outside
    # userEnteredFormat.backgroundColor — so without this the red line from last
    # Monday stays and a new one appears one column to its right, every week,
    # until the tab carries a dozen lines and none of them means "today". Same
    # explicit style:"NONE" wipe project_status_sheet.py already uses.
    # [2026-08-12 review]
    reqs.append({"updateBorders": {
        "range": {"sheetId": sid, "startRowIndex": MONTH_ROW,
                  "endRowIndex": max(n_rows, MONTH_ROW + 1),
                  "startColumnIndex": N_LABEL_COLS, "endColumnIndex": n_week_end},
        "top": {"style": "NONE"}, "bottom": {"style": "NONE"},
        "left": {"style": "NONE"}, "right": {"style": "NONE"},
        "innerHorizontal": {"style": "NONE"},
        "innerVertical": {"style": "NONE"}}})

    for row in area_rows:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": row, "endRowIndex": row + 1,
                      "startColumnIndex": 0, "endColumnIndex": n_week_end},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _AREA_BG,
                "textFormat": {"bold": True, "foregroundColor": _INK}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}})

    for f in fills:
        if f["b"] < f["a"]:
            continue
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": f["row"],
                      "endRowIndex": f["row"] + 1,
                      "startColumnIndex": f["a"], "endColumnIndex": f["b"] + 1},
            "cell": {"userEnteredFormat": {"backgroundColor": _rgb(f["colour"])}},
            "fields": "userEnteredFormat.backgroundColor"}})

    if today_col is not None:
        reqs.append({"updateBorders": {
            "range": {"sheetId": sid, "startRowIndex": MONTH_ROW,
                      "endRowIndex": n_rows,
                      "startColumnIndex": today_col,
                      "endColumnIndex": today_col + 1},
            "left": {"style": "SOLID_MEDIUM", "color": _TODAY}}})

    # Column widths: the label block readable, the 96 week columns narrow
    # enough that a quarter fits on screen.
    for i, px in enumerate([300, 120, 84, 84, 70][:N_LABEL_COLS]):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": N_LABEL_COLS, "endIndex": n_week_end},
        "properties": {"pixelSize": 21}, "fields": "pixelSize"}})

    # Alignment and wrap, from the one module that answers that question.
    # Column 0 holds project and task titles and stays LEFT: a column of centred
    # sentences has ragged edges on both sides and no vertical line for the eye
    # to follow, which reads worse than the overflow it was meant to fix.
    reqs.append(left_align(sid, FIRST_BODY_ROW, max(n_rows, FIRST_BODY_ROW + 1),
                           0, 1))
    reqs.append(centre_and_wrap(sid, FIRST_BODY_ROW,
                                max(n_rows, FIRST_BODY_ROW + 1), 1, N_LABEL_COLS))
    reqs.append(centre_and_wrap(sid, WEEK_ROW, WEEK_ROW + 1, 0, n_week_end))

    # The identity columns: narrow, hidden, and white-on-white so that unhiding
    # them still shows nothing useful — the same treatment project_status_sheet
    # gives its own. Asserted every run rather than set once, because a person
    # who unhides a column expects it to stay unhidden and this tab does not
    # negotiate: the readback depends on those cells being exactly what the
    # renderer wrote.
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS",
                  "startIndex": n_week_end, "endIndex": n_week_end + N_HIDDEN},
        "properties": {"pixelSize": 120, "hiddenByUser": True},
        "fields": "pixelSize,hiddenByUser"}})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0,
                  "endRowIndex": max(n_rows, 1) + 200,
                  "startColumnIndex": n_week_end,
                  "endColumnIndex": n_week_end + N_HIDDEN},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _WHITE,
            "textFormat": {"fontSize": 8, "foregroundColor": _WHITE}}},
        "fields": "userEnteredFormat(backgroundColor,textFormat)"}})

    if archive_title_row is not None:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": archive_title_row,
                      "endRowIndex": archive_title_row + 1,
                      "startColumnIndex": 0, "endColumnIndex": n_week_end},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb(GHOST_BG),
                "textFormat": {"bold": True, "foregroundColor": _INK}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
        # The archive's bar labels are printed into the week columns, where the
        # cells are 21px wide. Anything but a small font is unreadable soup.
        if archive_span:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": archive_span[0],
                          "endRowIndex": archive_span[1] + 1,
                          "startColumnIndex": N_LABEL_COLS,
                          "endColumnIndex": n_week_end},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"fontSize": 8, "foregroundColor": _INK}}},
                "fields": "userEnteredFormat.textFormat"}})

    # The task waterfall, collapsed by default: a project row is the unit, and
    # its tasks are there when Eyal wants them.
    for a, b in group_spans:
        reqs.append({"addDimensionGroup": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": a, "endIndex": b + 1}}})

    # The archive collapses to a single line by default. addDimensionGroup only
    # CREATES the group — it does not close it — so the answer to "it doubles
    # what is on screen" depends on this second request actually being sent.
    if archive_span:
        reqs.append({"updateDimensionGroup": {
            "dimensionGroup": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": archive_span[0],
                          "endIndex": archive_span[1] + 1},
                "depth": 1, "collapsed": True},
            "fields": "collapsed"}})

    # ASSERT, DON'T ADD. Conditional formats, groups and protected ranges all
    # survive a values clear, so re-running without deleting stacks a duplicate
    # every cycle — five of the fifteen defects in the 2026-08-09 review were
    # exactly this shape.
    try:
        meta = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().get(
                spreadsheetId=ssid,
                fields="sheets(properties.sheetId,protectedRanges,"
                       "rowGroups(range,depth))"))
        for sheet in meta.get("sheets", []):
            if sheet.get("properties", {}).get("sheetId") != sid:
                continue
            for pr in sheet.get("protectedRanges") or []:
                if pr.get("description") == _PROTECT_DESC:
                    reqs.insert(0, {"deleteProtectedRange": {
                        "protectedRangeId": pr["protectedRangeId"]}})
            for g in sheet.get("rowGroups") or []:
                reqs.insert(0, {"deleteDimensionGroup": {"range": g["range"]}})
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[timeline] could not read existing state: {e}")

    # Whole tab protected: every cell is generated. Phase 4 opens the project
    # rows for editing; until then an edit here would vanish on refresh.
    reqs.append({"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": sid},
        "description": _PROTECT_DESC,
        "warningOnly": True,
    }}})
    return reqs
