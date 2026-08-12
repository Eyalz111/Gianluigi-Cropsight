"""The CEO tab — milestones, and a block Eyal keeps by hand.

Phase 3 of docs/GANTT_V2_PLAN.md. Eyal: *"maybe we should have another
'managment' or ceo tab."*

A SEPARATE TAB, NOT A BAND ON THE TIMELINE. Its rows are a different kind of
object on a different edit cadence: a project moves when work moves, a milestone
moves when a commitment changes, and mixing the two puts a quarterly decision
next to a weekly one.

THE MANAGEMENT BLOCK IS HAND-MAINTAINED, AND SURVIVING A REFRESH IS THE WHOLE
DESIGN PROBLEM. Nothing in the database corresponds to OKRs, escalations or
availability, and inventing a source would be worse than a block that is honest
about being typed by a person. But this tab regenerates on every reconcile
cycle, so a naive rewrite would delete Eyal's typing every thirty minutes.

So the refresh READS THE BLOCK BACK FIRST and re-emits it verbatim below the
milestones, which lets the milestone list grow and shrink without the block
drifting or being clipped. If that read fails for any reason, the refresh is
ABANDONED rather than run — the same instinct as the Timeline's empty-areas
guard, and a stronger case for it, because here the cost of guessing is a
person's own words.

The milestone rows show "moved 1 Jun -> 6 Jul", never "SLIPPED". Whether a move
was a slip or a re-plan is Eyal's read; the board records the fact and does not
put a word in his mouth.
"""

import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings
from processors.milestones import CEO_TAB, list_milestones
from services.google_sheets import sheets_service

logger = logging.getLogger(__name__)

ISRAEL_TZ = timezone(timedelta(hours=3))

HEADERS = ["Milestone", "Kind", "Target", "History", "Status"]
N_COLS = len(HEADERS)

# The line that separates what this module owns from what Eyal owns. Everything
# from this row down is read back and re-emitted untouched.
MANAGEMENT_MARKER = "MANAGEMENT — hand-maintained (Gianluigi never edits below this line)"

# Seeded once, only when the tab has no block at all. The four headings are the
# ones the old board carried under MANAGEMENT — CEO OP.
_DEFAULT_BLOCK = [
    ["Company OKRs", "", "", "", ""],
    ["Strategy & Decisions", "", "", "", ""],
    ["Escalations", "", "", "", ""],
    ["Availability", "", "", "", ""],
]

_TITLE_BG = {"red": 0.75, "green": 0.0, "blue": 0.0}
_HEADER_BG = {"red": 0.0, "green": 0.44, "blue": 0.75}
_BLOCK_BG = {"red": 0.85, "green": 0.81, "blue": 0.91}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_INK = {"red": 0.13, "green": 0.13, "blue": 0.13}
_MOVED_INK = {"red": 0.72, "green": 0.33, "blue": 0.05}

_KIND_BG = {
    "funding": {"red": 0.85, "green": 0.92, "blue": 0.83},
    "product": {"red": 0.80, "green": 0.88, "blue": 0.94},
    "commercial": {"red": 0.99, "green": 0.90, "blue": 0.79},
    "corporate": {"red": 0.91, "green": 0.89, "blue": 0.95},
}
_STATUS_BG = {
    "hit": {"red": 0.72, "green": 0.84, "blue": 0.69},
    "missed": {"red": 0.91, "green": 0.31, "blue": 0.31},
    "dropped": {"red": 0.82, "green": 0.82, "blue": 0.80},
}

_PROTECT_DESC = "Gianluigi: milestones are generated — the management block below is yours"


def _fmt(d) -> str:
    """1 Jun — short, because these are read at a glance."""
    if not d:
        return ""
    try:
        return datetime.fromisoformat(str(d)[:10]).strftime("%-d %b %Y")
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(str(d)[:10]).strftime("%d %b %Y").lstrip("0")
        except (ValueError, TypeError):
            return str(d)[:10]


def history_cell(milestone: dict) -> str:
    """What this milestone's dates have done. Fact only, no verdict.

    "moved 1 Jun 2026 -> 6 Jul 2026" reads as a record. "SLIPPED 5w" reads as an
    accusation, and the board is not entitled to one: a move can be a slip or a
    deliberate re-plan and nothing here can tell the difference.
    """
    original = milestone.get("original_date")
    target = milestone.get("target_date")
    moves = milestone.get("moves") or []
    if not moves and (not original or str(original) == str(target)):
        return ""
    if len(moves) > 1:
        return (f"moved {len(moves)}× · {_fmt(original)} → {_fmt(target)}")
    return f"moved {_fmt(original)} → {_fmt(target)}"


def _split_block(rows: list[list]) -> "list[list] | None":
    """Everything below the marker, or None if the tab has no marker yet."""
    for i, row in enumerate(rows):
        if row and str(row[0]).strip().startswith("MANAGEMENT"):
            return [list(r) for r in rows[i + 1:]]
    return None


async def refresh_ceo_tab(spreadsheet_id: str | None = None) -> dict:
    """Rebuild the milestone half of the CEO tab; carry the manual half across."""
    ssid = spreadsheet_id or settings.PROJECT_STATUS_SHEET_ID
    if not ssid:
        return {"skipped": "no PROJECT_STATUS_SHEET_ID"}

    milestones = list_milestones()

    sid = await sheets_service._ensure_tab(CEO_TAB, HEADERS, spreadsheet_id=ssid)
    if sid is None:
        return {"error": "could not create the CEO tab"}

    # Read the hand-maintained block BEFORE touching anything. A failure here
    # means we do not know what Eyal wrote, and writing anyway would delete it.
    try:
        existing = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=ssid, range=f"'{CEO_TAB}'!A1:E400"))
        prior = existing.get("values") or []
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[ceo] could not read the manual block — not writing: {e}")
        return {"skipped": "manual block unreadable"}

    block = _split_block(prior)
    seeded = block is None
    if seeded:
        block = [list(r) for r in _DEFAULT_BLOCK]
    # Trailing blank rows accumulate every cycle otherwise: the block is
    # re-emitted, the grid grows, and the next read picks up the padding too.
    while block and not any(str(c).strip() for c in block[-1]):
        block.pop()

    grid: list[list] = [
        ["CEO — milestones and management"] + [""] * (N_COLS - 1),
        HEADERS,
    ]
    milestone_rows: list[tuple[int, dict]] = []
    for m in milestones:
        milestone_rows.append((len(grid), m))
        grid.append([
            m.get("title") or "",
            m.get("kind") or "",
            _fmt(m.get("target_date")),
            history_cell(m),
            m.get("status") or "open",
        ])
    if not milestones:
        grid.append(["(no milestones yet — approve the proposals to populate this)"]
                    + [""] * (N_COLS - 1))

    grid.append([""] * N_COLS)
    marker_row = len(grid)
    grid.append([MANAGEMENT_MARKER] + [""] * (N_COLS - 1))
    for row in block:
        grid.append((list(row) + [""] * N_COLS)[:N_COLS])

    # Clear every row of the used columns, not a computed bound — the Timeline's
    # `len(grid) + 40` left stale rows behind when the render shrank, and a
    # shrinking milestone list has exactly the same shape.
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().clear(
            spreadsheetId=ssid, range=f"'{CEO_TAB}'!A:E", body={}))
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().update(
            spreadsheetId=ssid, range=f"'{CEO_TAB}'!A1:E{len(grid)}",
            valueInputOption="RAW", body={"values": grid}))

    reqs = _format_requests(sid, len(grid), marker_row, milestone_rows, ssid)
    if reqs:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=ssid, body={"requests": reqs}))

    out = {"milestones": len(milestones),
           "moved": len([m for m in milestones if m.get("moves")]),
           "manual_rows": len(block), "seeded_block": seeded}
    logger.info(f"[ceo] {out}")
    return out


def _format_requests(sid, n_rows, marker_row, milestone_rows, ssid) -> list[dict]:
    reqs: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
        # ASSERT, DON'T ADD. Every colour below is re-applied from scratch, so
        # the body is reset first — a milestone that moved row position would
        # otherwise leave its kind colour behind on a row that no longer holds
        # it. values().clear() takes the text and never the fill.
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 2,
                      "endRowIndex": max(n_rows, 3) + 100,
                      "startColumnIndex": 0, "endColumnIndex": N_COLS},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _WHITE,
                "textFormat": {"bold": False, "italic": False, "fontSize": 10,
                               "foregroundColor": _INK}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
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
                      "startColumnIndex": 0, "endColumnIndex": N_COLS},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": _WHITE}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
    ]

    for row, m in milestone_rows:
        kind = _KIND_BG.get((m.get("kind") or "").lower())
        if kind:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": row,
                          "endRowIndex": row + 1,
                          "startColumnIndex": 1, "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {"backgroundColor": kind}},
                "fields": "userEnteredFormat.backgroundColor"}})

        status = _STATUS_BG.get((m.get("status") or "").lower())
        if status:
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": row,
                          "endRowIndex": row + 1,
                          "startColumnIndex": 4, "endColumnIndex": 5},
                "cell": {"userEnteredFormat": {"backgroundColor": status}},
                "fields": "userEnteredFormat.backgroundColor"}})

        if m.get("moves"):
            # Amber italic, not red: this says "the date changed", which is a
            # fact, rather than "this is late", which is a judgement.
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": row,
                          "endRowIndex": row + 1,
                          "startColumnIndex": 3, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"italic": True,
                                   "foregroundColor": _MOVED_INK}}},
                "fields": "userEnteredFormat.textFormat"}})

    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": marker_row,
                  "endRowIndex": marker_row + 1,
                  "startColumnIndex": 0, "endColumnIndex": N_COLS},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _BLOCK_BG,
            "textFormat": {"bold": True, "foregroundColor": _INK}}},
        "fields": "userEnteredFormat(backgroundColor,textFormat)"}})

    for i, px in enumerate([420, 110, 120, 240, 90]):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}})

    # Delete our own protected range before re-adding: it survives a values
    # clear, so re-running would stack a duplicate every cycle.
    try:
        meta = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().get(
                spreadsheetId=ssid,
                fields="sheets(properties.sheetId,protectedRanges)"))
        for sheet in meta.get("sheets", []):
            if sheet.get("properties", {}).get("sheetId") != sid:
                continue
            for pr in sheet.get("protectedRanges") or []:
                if pr.get("description") == _PROTECT_DESC:
                    reqs.insert(0, {"deleteProtectedRange": {
                        "protectedRangeId": pr["protectedRangeId"]}})
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[ceo] could not read existing protection: {e}")

    # Protect ONLY the generated half. The management block below the marker is
    # Eyal's to type in, so it is deliberately left unprotected — protecting the
    # whole tab, as the Timeline does, would make the block read-only and the
    # hand-maintained design pointless.
    reqs.append({"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": sid, "startRowIndex": 0,
                  "endRowIndex": marker_row + 1,
                  "startColumnIndex": 0, "endColumnIndex": N_COLS},
        "description": _PROTECT_DESC,
        "warningOnly": True,
    }}})
    return reqs
