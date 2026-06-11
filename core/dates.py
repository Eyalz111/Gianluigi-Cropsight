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
from datetime import date, datetime

# 20.6.26 / 20/6/2026 / 20-6-26 — day-first; separators . / -
_DMY = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\s*$")
# ISO date, optionally with a time suffix to discard: 2026-06-20T10:00:00Z
_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")


def parse_human_date(value) -> str | None:
    """
    Parse a human-entered date to ISO 'YYYY-MM-DD', or None if unparseable.

    Accepts date/datetime objects, ISO strings (time portion discarded), and
    day-first numeric strings (20.6.26, 20/6/2026, 20-6-26). Day-first wins
    for ambiguous input — this system's users write Israeli-style dates.
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

    # Last resort: natural language ("March 30", "next Friday" comes pre-parsed
    # by MCP callers; this covers "Jun 20 2026"-style cells). dayfirst keeps
    # the Israeli convention for anything dateutil finds ambiguous.
    try:
        from dateutil.parser import parse as _du_parse
        return _du_parse(text, dayfirst=True, fuzzy=False).date().isoformat()
    except Exception:
        return None


def _safe_iso(y: int, mo: int, d: int) -> str | None:
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None
