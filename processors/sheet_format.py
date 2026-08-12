"""One answer to "how does a cell look".

Eyal, 2026-08-12, on the Timeline and Focus tabs: *"the dates in the timeline
and in the focus dont look the same format as in the projects etc - thats not
comfort and not good!"*

He is right, and the cause is that four modules each decided independently:

    processors/project_status._fmt_date            %d/%m/%Y
    processors/project_status_reconcile._display   %d/%m/%Y
    services/google_sheets (inline helper)         %d/%m/%Y
    services/timeline_sheet                        raw ISO      <- deviant
    services/ceo_sheet                             "%-d %b %Y"  <- deviant
    services/focus_sheet                           raw ISO      <- deviant

The three older surfaces already agreed; the three tabs built this month did
not, and they are the ones sitting next to the area tabs Eyal reads every day.

So this module exists to be the ONLY place that answers the question. Adding a
seventh surface should mean importing from here, not writing a seventh
strftime — otherwise this drifts again the moment someone is in a hurry.

`processors/summary_context._fmt_date` is deliberately NOT included: it renders
"Mon DD, YYYY" into prose for email and Telegram, where a slash-separated
numeric date reads badly. Different medium, different answer, on purpose.

No I/O here. The style helpers return Sheets API request dicts; the caller
sends them.
"""

from datetime import date, datetime

# DD/MM/YYYY, because that is what the area tabs have always used and they are
# the surface Eyal and Nechama work in daily. The generated tabs move to match
# them, not the other way round.
DATE_FORMAT = "%d/%m/%Y"


def display_date(value) -> str:
    """A stored date -> what belongs in a cell. Blank when unset.

    Anything unparseable comes back AS GIVEN rather than truncated. A cell can
    legitimately hold "Once a week" or "end of August" — the meetings pool is
    full of both — and cutting that to ten characters would silently corrupt
    what a person typed.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime(DATE_FORMAT)
    text = str(value).strip()
    if text.lower() in ("none", "null"):
        return ""
    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")).strftime(DATE_FORMAT)
    except (ValueError, TypeError):
        return text


def centre_and_wrap(sheet_id: int, first_row: int, last_row: int,
                    first_col: int, last_col: int,
                    vertical: str = "MIDDLE") -> dict:
    """Centre a block and let it wrap.

    Eyal: *"all of the dates and content in the different tabs are
    hidden/overlayed and just not visible - i think wrap text can help. i also
    want all the file to be centered."*

    OVERFLOW is the actual complaint. A cell whose text is longer than its
    column spills into the neighbour only while the neighbour is empty; the
    moment it is not, the text is silently clipped and the sheet looks like it
    is hiding things. WRAP trades width for row height, which is the right
    trade on a tab that is read rather than scrolled.
    """
    return {"repeatCell": {
        "range": {"sheetId": sheet_id,
                  "startRowIndex": first_row, "endRowIndex": last_row,
                  "startColumnIndex": first_col, "endColumnIndex": last_col},
        "cell": {"userEnteredFormat": {
            "horizontalAlignment": "CENTER",
            "verticalAlignment": vertical,
            "wrapStrategy": "WRAP"}},
        "fields": ("userEnteredFormat(horizontalAlignment,"
                   "verticalAlignment,wrapStrategy)")}}


def left_align(sheet_id: int, first_row: int, last_row: int,
               first_col: int, last_col: int) -> dict:
    """Keep a column left-aligned and wrapping.

    Centring is right for dates, owners and short status words. It is wrong for
    a long title: a centred sentence has ragged edges on BOTH sides, so a column
    of them has no vertical line for the eye to follow and reads worse than the
    overflow it was meant to fix.
    """
    return {"repeatCell": {
        "range": {"sheetId": sheet_id,
                  "startRowIndex": first_row, "endRowIndex": last_row,
                  "startColumnIndex": first_col, "endColumnIndex": last_col},
        "cell": {"userEnteredFormat": {
            "horizontalAlignment": "LEFT",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP"}},
        "fields": ("userEnteredFormat(horizontalAlignment,"
                   "verticalAlignment,wrapStrategy)")}}
