"""When Eyal wants a meeting to happen, in his own words.

Eyal, 2026-08-12: *"in the proposed date i am writing to nechama around when we
want to have this meeting in a dates time frame. make the system robust and able
to accept it, both date wise and writing wise such as 'every week' or every 2
weeks etc."*

THIS IS A DATA-LOSS FIX, NOT A CONVENIENCE. The Proposed Date column was going
straight into a `timestamptz`, so `"Once a week"` raised 22007, a broad `except`
returned None, and the caller did `if not created: continue` — discarding the
WHOLE ROW. Six meetings Eyal typed lived only in the sheet, retried and dropped
every thirty minutes for hours:

    Error creating manual follow-up meeting 'CropSight Weekly Managment Meeting':
    invalid input syntax for type timestamp with time zone: "Once a week"

THE RULE THAT PREVENTS IT HAPPENING AGAIN: an unreadable cell costs you that
CELL, never the row. The same rule the Timeline readback already follows.

WHAT IS STORED. `timing_text` keeps the words verbatim and is never parsed away
— it is what Nechama reads, and the system has no business rewriting it. The
structure sits alongside: a real date when there is one, a recurrence when the
text expresses a cadence, a window when it expresses a range. That is what sorts
and schedules. Round-tripping the text through a parser and back would eventually
turn "end of August, before Paolo travels" into "31/08/2026" and lose the reason.
"""

import re
from datetime import date, datetime, timedelta

# Cadences, longest phrasing first so "every 2 weeks" is not eaten by "every".
_RECURRENCE = [
    (r"\b(?:every|each)\s+(?:2|two)\s+weeks?\b", "biweekly"),
    (r"\b(?:bi[-\s]?weekly|fortnight(?:ly)?)\b", "biweekly"),
    (r"\bevery\s+other\s+week\b", "biweekly"),
    (r"\b(?:once\s+a\s+week|every\s+week|weekly|1\s*/\s*week)\b", "weekly"),
    (r"\b(?:twice|2\s*times?)\s+a\s+week\b", "twice_weekly"),
    (r"\b(?:once\s+a\s+month|every\s+month|monthly|1\s*/\s*month)\b", "monthly"),
    (r"\b(?:every|each)\s+(?:2|two)\s+months?\b", "bimonthly"),
    (r"\b(?:quarterly|every\s+quarter|once\s+a\s+quarter)\b", "quarterly"),
    (r"\b(?:annual(?:ly)?|yearly|once\s+a\s+year|every\s+year)\b", "annual"),
    (r"\b(?:daily|every\s+day)\b", "daily"),
]

RECURRENCE_LABEL = {
    "daily": "every day", "weekly": "once a week", "twice_weekly": "twice a week",
    "biweekly": "every 2 weeks", "monthly": "once a month",
    "bimonthly": "every 2 months", "quarterly": "quarterly", "annual": "yearly",
}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d-%m-%Y",
                 "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y")


def _month_index(word: str) -> "int | None":
    return _MONTHS.get((word or "")[:3].lower())


def _month_bounds(month: int, year: int) -> tuple:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def parse_single_date(text: str) -> "str | None":
    """A clean date, or None. Refuses anything it is not sure of."""
    s = (text or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # Bare "15/9" — the year is this one unless that is already past, in which
    # case the person almost certainly means next year.
    m = re.fullmatch(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            today = date.today()
            for year in (today.year, today.year + 1):
                try:
                    cand = date(year, mo, d)
                except ValueError:
                    return None
                if cand >= today or year != today.year:
                    return cand.isoformat()
    return None


def parse_timing(text, today: "date | None" = None) -> dict:
    """Eyal's words -> {text, kind, date, recurrence, window_start, window_end}.

    `kind` is one of date | recurrence | window | unknown. `unknown` is a
    legitimate, non-failing answer: the text is still kept, and the meeting is
    still created. Only the sorting loses.
    """
    today = today or date.today()
    raw = "" if text is None else str(text).strip()
    out = {"text": raw, "kind": "unknown", "date": None, "recurrence": None,
           "window_start": None, "window_end": None}
    if not raw:
        return out

    low = raw.lower()

    # A recurrence wins over any date inside it: "every week from 15/9" is a
    # cadence with a start, not a one-off booking.
    for pattern, name in _RECURRENCE:
        if re.search(pattern, low):
            out["kind"] = "recurrence"
            out["recurrence"] = name
            out["date"] = parse_single_date(raw)
            return out

    exact = parse_single_date(raw)
    if exact:
        out["kind"] = "date"
        out["date"] = exact
        return out

    # "between 10/9 and 20/9", "10/9 - 20/9", "10/9 to 20/9"
    m = re.search(r"(\d{1,2}\s*[/.\-]\s*\d{1,2}(?:\s*[/.\-]\s*\d{2,4})?)\s*"
                  r"(?:-|–|—|to|until|till|and)\s*"
                  r"(\d{1,2}\s*[/.\-]\s*\d{1,2}(?:\s*[/.\-]\s*\d{2,4})?)", low)
    if m:
        a, b = parse_single_date(m.group(1)), parse_single_date(m.group(2))
        if a and b:
            out.update(kind="window", window_start=min(a, b), window_end=max(a, b))
            return out

    # "week of 15/9"
    m = re.search(r"week\s+(?:of|beginning|starting|commencing)\s+(.+)", low)
    if m:
        anchor = parse_single_date(m.group(1).strip())
        if anchor:
            start = datetime.fromisoformat(anchor).date()
            start -= timedelta(days=start.weekday())      # back to Monday
            out.update(kind="window", window_start=start.isoformat(),
                       window_end=(start + timedelta(days=6)).isoformat())
            return out

    # "end of August", "beginning of Sep", "mid October", "during November"
    m = re.search(r"\b(end|beginning|start|early|mid|middle|late|during|in)\b"
                  r"(?:\s+of)?\s+([a-z]{3,9})\b", low)
    if m:
        month = _month_index(m.group(2))
        if month:
            year = today.year if month >= today.month else today.year + 1
            first, last = _month_bounds(month, year)
            where = m.group(1)
            if where in ("end", "late"):
                out.update(kind="window",
                           window_start=(last - timedelta(days=9)).isoformat(),
                           window_end=last.isoformat())
            elif where in ("beginning", "start", "early"):
                out.update(kind="window", window_start=first.isoformat(),
                           window_end=(first + timedelta(days=9)).isoformat())
            elif where in ("mid", "middle"):
                out.update(kind="window",
                           window_start=first.replace(day=11).isoformat(),
                           window_end=first.replace(day=20).isoformat())
            else:
                out.update(kind="window", window_start=first.isoformat(),
                           window_end=last.isoformat())
            return out

    # A bare month name: "September", "Sept 2027"
    m = re.fullmatch(r"([a-z]{3,9})\.?(?:\s+(\d{4}))?", low)
    if m:
        month = _month_index(m.group(1))
        if month:
            year = int(m.group(2)) if m.group(2) else (
                today.year if month >= today.month else today.year + 1)
            first, last = _month_bounds(month, year)
            out.update(kind="window", window_start=first.isoformat(),
                       window_end=last.isoformat())
            return out

    # "Q3", "Q4 2027"
    m = re.fullmatch(r"q([1-4])(?:\s*(\d{4}))?", low)
    if m:
        q = int(m.group(1))
        year = int(m.group(2)) if m.group(2) else today.year
        first, _ = _month_bounds(q * 3 - 2, year)
        _, last = _month_bounds(q * 3, year)
        out.update(kind="window", window_start=first.isoformat(),
                   window_end=last.isoformat())
        return out

    return out


def sort_key(parsed: dict) -> str:
    """One comparable value, so a pool of mixed timings still orders sensibly.

    A recurrence has no single date and must not sort as "no date" — it is the
    most live thing on the tab. It sorts FIRST; then real dates and windows by
    when they start; then genuinely undated work.
    """
    if not parsed:
        return "5"
    kind = parsed.get("kind")
    if kind == "recurrence":
        return "0"
    if kind == "date" and parsed.get("date"):
        return "1" + parsed["date"]
    if kind == "window" and parsed.get("window_start"):
        return "1" + parsed["window_start"]
    return "5"
