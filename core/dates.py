"""
Robust date parsing for human-entered dates (Sheets cells, MCP params).

The Tasks sheet is hand-edited: Eyal types dates as "20.6.26", "20/6/2026",
"20-6-26" (Israeli day-first convention) alongside ISO "2026-06-20". The
2026-06-11 incident: reconcile pulled "20.6.26" cells into the DB, where
_serialize_datetime couldn't parse them and silently stored NULL — erasing
deadlines. Every Sheet->DB date path must go through parse_human_date().

Convention: ambiguous numeric dates are DAY-FIRST (20.6.26 = 20 June 2026).
Two-digit years are 20xx. Unparseable input returns None — callers must treat
None as "leave the existing value alone", never as "clear the field".
"""

import re
from datetime import date, datetime, timedelta

from dateutil.parser import parse as _du_parse

# 20.6.26 / 20/6/2026 / 20-6-26 — day-first; separators . / -
_DMY = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s*$")
# ISO date, optionally with a time suffix to discard: 2026-06-20T10:00:00Z
_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
# Day + month, NO year: "12/8", "12.8", "12 Aug", "12 August", "Aug 12".
# These are specific — unlike "June" or "2026", which name no day and must stay
# rejected. The Project Status sheet's how-to promises they work, and a review
# meeting is exactly where somebody writes "12/8" and means it. [2026-08-07]
_DM_NUM = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DM_NAME = re.compile(r"^\s*(\d{1,2})\s+([a-z]{3,9})\.?\s*$", re.IGNORECASE)
_MD_NAME = re.compile(r"^\s*([a-z]{3,9})\.?\s+(\d{1,2})\s*$", re.IGNORECASE)

_WEEKDAYS = {d: i for i, d in enumerate(
    ["monday", "tuesday", "wednesday", "thursday",
     "friday", "saturday", "sunday"])}
_RELATIVE = re.compile(
    r"^\s*(today|tomorrow|(?:next\s+|this\s+)?"
    r"(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?)\s*$",
    re.IGNORECASE)
_IN_N = re.compile(r"^\s*in\s+(\d{1,3})\s*(day|week|month)s?\s*$", re.IGNORECASE)

# Two deliberately different defaults: dateutil fills unspecified components
# (year/month/day) from its default, so parsing with both and comparing
# detects underspecified input ("2026", "30", "June") — which must be
# REJECTED, not silently completed with today's date (invented-deadline trap).
_DEFAULT_A = datetime(2001, 1, 1)
_DEFAULT_B = datetime(2002, 12, 28)


def parse_human_date(value, today: date | None = None) -> str | None:
    """
    Parse a human-entered date to ISO 'YYYY-MM-DD', or None if unparseable.

    Accepts date/datetime objects, ISO strings (time portion discarded),
    day-first numeric strings (20.6.26, 20/6/2026, 20-6-26), day+month with NO
    year (12/8, 12 Aug, Aug 12) and relative forms (today, tomorrow, next
    Tuesday, in 3 weeks). Day-first wins for ambiguous input — this system's
    users write Israeli-style dates.

    `today` is injectable so the relative and year-less branches are testable;
    it defaults to the real date.

    Underspecified input that names no DAY — "June", "2026", "30" — is still
    rejected. That is the invented-deadline guard and it has to stay: a task
    with a made-up deadline is worse than one with none.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None

    relative = _parse_relative(text, today)
    if relative:
        return relative

    m = _ISO.match(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _safe_iso(y, mo, d)

    m = _DMY.match(text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        # Day-first by convention; if impossible (e.g. 6.20.26), try month-first.
        return _safe_iso(y, mo, d) or _safe_iso(y, d, mo)

    # Day + month with no year. Specific enough to honour, unlike "June".
    m = _DM_NUM.match(text)
    if m:
        return _with_year(int(m.group(1)), int(m.group(2)), today)
    m = _DM_NAME.match(text)
    if m and m.group(2)[:3].lower() in _MONTHS:
        return _with_year(int(m.group(1)), _MONTHS[m.group(2)[:3].lower()], today)
    m = _MD_NAME.match(text)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return _with_year(int(m.group(2)), _MONTHS[m.group(1)[:3].lower()], today)

    # Last resort: written-out dates like "Jun 20 2026" / "20 June 2026".
    # dayfirst keeps the Israeli convention for anything dateutil finds
    # ambiguous. Parse twice with different defaults — if the results differ,
    # the input was underspecified (e.g. "2026", "30", "June") and dateutil
    # filled the gaps from the default; reject instead of inventing a date.
    try:
        a = _du_parse(text, dayfirst=True, fuzzy=False, default=_DEFAULT_A).date()
        b = _du_parse(text, dayfirst=True, fuzzy=False, default=_DEFAULT_B).date()
        return a.isoformat() if a == b else None
    except Exception:
        return None


def _safe_iso(y: int, mo: int, d: int) -> str | None:
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _with_year(day: int, month: int, today: date | None) -> str | None:
    """Pick the year for a day+month the writer didn't give one for.

    This year, unless that lands more than six months in the PAST — then next
    year. Someone writing "12/1" in December means the coming January, while
    someone writing "12/8" a fortnight late means the deadline they just missed.
    Six months is the only split that gets both right without asking.
    """
    today = today or date.today()
    iso = _safe_iso(today.year, month, day)
    if not iso:
        return None
    chosen = date.fromisoformat(iso)
    if chosen < today - timedelta(days=183):
        return _safe_iso(today.year + 1, month, day)
    return iso


def _parse_relative(text: str, today: date | None) -> str | None:
    """today / tomorrow / next Tuesday / in 3 weeks.

    Worth supporting because this is how people actually speak in a review —
    "let's say next Tuesday" gets typed verbatim into the cell.
    """
    today = today or date.today()
    m = _IN_N.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return (today + timedelta(days=days)).isoformat()

    m = _RELATIVE.match(text)
    if not m:
        return None
    phrase = " ".join(m.group(1).lower().split())
    if phrase == "today":
        return today.isoformat()
    if phrase == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    explicit_next = phrase.startswith("next")
    word = phrase.replace("next", "").replace("this", "").strip()
    target = next((i for name, i in _WEEKDAYS.items()
                   if name.startswith(word[:3])), None)
    if target is None:
        return None
    # Always the COMING one: "Tuesday" said on a Tuesday means next week's, not
    # today's — nobody sets a deadline for the meeting they are sitting in.
    ahead = (target - today.weekday()) % 7 or 7
    if explicit_next and ahead < 7:
        ahead += 0        # "next Tuesday" == the coming Tuesday, not +1 week
    return (today + timedelta(days=ahead)).isoformat()
