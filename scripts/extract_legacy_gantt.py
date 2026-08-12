"""Recover the old Gantt's timelines as real dates, and archive them.

Phase 0 of docs/GANTT_V2_PLAN.md. Run AFTER scripts/migrate_gantt_legacy_bars.sql.

    python scripts/extract_legacy_gantt.py              # dry run, writes nothing
    python scripts/extract_legacy_gantt.py --apply
    python scripts/extract_legacy_gantt.py --apply --tab 2028-2029

WHY THIS EXISTS. The board is a weekly-column grid: a bar is the same text
repeated across consecutive week cells, and row 5 carries "W9 02/03" headers, so
each column has a real calendar date. Roughly 400 bars are recoverable as dated
spans — and that sheet is currently their only record. One dragged row and the
history is gone.

WHAT IT REFUSES TO DO. It does not match bars to projects. Naive name overlap
seeds "Corporate" from a recurring admin line dated 2027-05-31, and maps four
different projects onto one "MVP Product Delivery" bar. That judgement belongs
in a proposal Eyal approves (Phase 1), not in the archive.

Re-running replaces a tab's rows wholesale, so it is idempotent.
"""

import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings                       # noqa: E402
from services.google_sheets import sheets_service          # noqa: E402
from services.supabase_client import supabase_client       # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("extract_legacy_gantt")

HEADER_ROW = 4          # 0-indexed: row 5 carries the week headers
FIRST_BODY_ROW = 5
READ_RANGE = "A1:CZ200"

# "W9\n02/03" -> day 02, month 03. Some headers carry only the date.
_DATE_IN_HEADER = re.compile(r"(\d{1,2})[/.](\d{1,2})")
_OWNER_TAG = re.compile(r"^\s*(\[[^\]]{1,12}\])")
# Two cells belong to the same bar when their text agrees on this prefix. The
# board sometimes appends a per-week note to an otherwise identical label, and
# comparing in full would shatter one bar into weekly fragments.
_SAME_BAR_CHARS = 26
# ...but a fixed prefix alone fails whenever the base label is SHORTER than the
# window: "MVP Work" vs "MVP Work - week 2" share only 8 characters, so a
# 26-char comparison finds them different and splits the bar. Prefix
# containment catches that case. The floor stops two unrelated lines merging on
# a trivial shared opening.
_SAME_BAR_MIN_PREFIX = 8


def _same_bar(a: str, b: str) -> bool:
    """Do these two week cells belong to one continuous bar?"""
    a, b = a.strip(), b.strip()
    if a == b:
        return True
    if a[:_SAME_BAR_CHARS] == b[:_SAME_BAR_CHARS]:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= _SAME_BAR_MIN_PREFIX and long.startswith(short)


def _cell(grid: list, r: int, c: int) -> str:
    row = grid[r] if r < len(grid) else []
    return (row[c] if c < len(row) else "") or ""


def week_columns(grid: list, start_year: int) -> dict[int, date]:
    """Column index -> real date, read from the week headers.

    The headers give day and month but never a year. Weeks run left to right,
    so the year rolls over exactly when the month goes BACKWARDS against the
    previous column — that is what turns a 2026 board into a 2026-2027 one.
    Getting this wrong silently dates half the board a year early, which is why
    it is a named function with its own test rather than an inline loop.
    """
    out: dict[int, date] = {}
    year = start_year
    prev: date | None = None
    for c in range(4, 140):
        header = _cell(grid, HEADER_ROW, c)
        m = _DATE_IN_HEADER.search(header)
        if not m:
            continue
        day, month = int(m.group(1)), int(m.group(2))
        candidate_year = year
        if prev is not None and (month, day) < (prev.month, prev.day):
            candidate_year += 1
        try:
            d = date(candidate_year, month, day)
        except ValueError:
            # 30/02 and friends. Skip the column WITHOUT committing the year
            # roll — an unparseable header is not evidence that a year passed,
            # and advancing on it would date every later column a year late.
            continue
        out[c] = d
        year, prev = candidate_year, d
    return out


def recover_bars(grid: list, weeks: dict[int, date], tab: str) -> list[dict]:
    """Walk the body, grouping consecutive same-text week cells into bars."""
    bars: list[dict] = []
    section = ""
    ordered_cols = sorted(weeks)

    for r in range(FIRST_BODY_ROW, len(grid)):
        col_a, col_b = _cell(grid, r, 0).strip(), _cell(grid, r, 1).strip()
        if col_a and not col_b:                 # a section header row
            section = col_a
            continue
        if not col_b:
            continue

        run: dict | None = None
        for c in ordered_cols:
            text = _cell(grid, r, c).strip()
            if not text:
                if run:
                    bars.append(_close(run, weeks, section, col_b, r, tab))
                    run = None
                continue
            if run and _same_bar(text, run["text"]):
                run["last"] = c
            else:
                if run:
                    bars.append(_close(run, weeks, section, col_b, r, tab))
                run = {"text": text, "first": c, "last": c}
        if run:
            bars.append(_close(run, weeks, section, col_b, r, tab))
    return bars


def _close(run: dict, weeks: dict[int, date], section: str,
           lane: str, row: int, tab: str) -> dict:
    tag = _OWNER_TAG.match(run["text"])
    return {
        "source_tab": tab,
        "section": section[:200],
        "lane": lane[:200],
        "row_number": row + 1,                  # 1-indexed, matches the sheet
        "label": run["text"][:2000],
        "start_date": weeks[run["first"]].isoformat(),
        "end_date": weeks[run["last"]].isoformat(),
        "weeks": sum(1 for c in weeks if run["first"] <= c <= run["last"]),
        "owner_tag": tag.group(1) if tag else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to the archive")
    ap.add_argument("--tab", default=settings.GANTT_MAIN_TAB or "2026-2027")
    ap.add_argument("--start-year", type=int, default=0,
                    help="year of the FIRST week column (default: from the tab name)")
    args = ap.parse_args()

    if not settings.GANTT_SHEET_ID:
        logger.error("GANTT_SHEET_ID is not set")
        return 1

    start_year = args.start_year or int(re.match(r"(\d{4})", args.tab).group(1))

    grid = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().get(
            spreadsheetId=settings.GANTT_SHEET_ID,
            range=f"'{args.tab}'!{READ_RANGE}")).get("values", [])
    if not grid:
        logger.error(f"tab {args.tab!r} read back EMPTY — refusing to touch the archive")
        return 1

    weeks = week_columns(grid, start_year)
    if not weeks:
        logger.error("no week columns parsed — the header row may have moved")
        return 1
    bars = recover_bars(grid, weeks, args.tab)

    logger.info(f"tab           : {args.tab}")
    logger.info(f"week columns  : {len(weeks)}  "
                f"({weeks[min(weeks)]} -> {weeks[max(weeks)]})")
    logger.info(f"bars recovered: {len(bars)}")
    if bars:
        logger.info(f"span          : {min(b['start_date'] for b in bars)} -> "
                    f"{max(b['end_date'] for b in bars)}")
        logger.info("\nlongest bars:")
        for b in sorted(bars, key=lambda x: -x["weeks"])[:8]:
            label = re.sub(r"^\[[^\]]*\]\s*", "", b["label"])[:44]
            logger.info(f"   r{b['row_number']:<4} {b['section'][:18]:18} "
                        f"{label:46} {b['start_date']} -> {b['end_date']}")

    if not args.apply:
        logger.info("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    # An empty read is already refused above, so reaching here means we have
    # real rows. Replace this tab wholesale: re-extraction should not append.
    supabase_client.client.table("gantt_legacy_bars").delete().eq(
        "source_tab", args.tab).execute()
    for i in range(0, len(bars), 200):
        supabase_client.client.table("gantt_legacy_bars").insert(
            bars[i:i + 200]).execute()

    stored = (supabase_client.client.table("gantt_legacy_bars")
              .select("id", count="exact").eq("source_tab", args.tab)
              .limit(1).execute()).count
    logger.info(f"\narchived {stored} row(s) for {args.tab}")
    if stored != len(bars):
        logger.error(f"MISMATCH: recovered {len(bars)} but stored {stored}")
        return 1

    supabase_client.log_action(
        action="gantt_legacy_archived",
        details={"tab": args.tab, "bars": stored,
                 "span": [min(b["start_date"] for b in bars),
                          max(b["end_date"] for b in bars)]},
        triggered_by="eyal",
    )
    logger.info("logged to audit_log as gantt_legacy_archived")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
