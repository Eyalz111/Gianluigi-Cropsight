"""Re-assert every piece of formatting on the workbook. Touches NO cell value.

Formatting is per-cell and survives `values().clear()`, so a rebuild leaves the
previous layout's validation, borders and protected ranges sitting at whatever
row and column they were applied to. Three symptoms of exactly that, all
reported from the live file on 2026-08-09:

  · `To do` raised "invalid date" on every row — it now sits where `Date` used
    to, and the old DATE_IS_VALID rule never moved.
  · Action rows carried the thick blue fence that marks the top of a project
    block, spanning seven columns rather than nine.
  · Editing Priority on the Meetings tab asked for confirmation, because a
    protected range from the old layout still covered that column.

This reads the tabs to learn WHICH rows are projects and which are actions,
clears the body's validation and borders, and puts back only what belongs. The
values are never read for content and never written.

    python scripts/restyle_workbook.py            # report only
    python scripts/restyle_workbook.py --apply
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings                            # noqa: E402
from processors.project_status_reconcile import NON_AREA_TABS    # noqa: E402
from services.google_sheets import (                            # noqa: E402
    MEETINGS_ARCHIVE_HEADERS, MEETINGS_ARCHIVE_TAB_NAME,
    MEETING_TAB_NAME, MEETING_TRACKER_HEADERS, sheets_service,
)
from services.project_status_rows import (                      # noqa: E402
    SYSTEM_ACTION, SYSTEM_PROJECT, parse_tab,
)
from services.project_status_sheet import (                     # noqa: E402
    _PROTECT_DESC, _checkbox_requests, _conditional_format_rules,
    _date_validation_requests, _header_note_requests,
    _marker_bold_requests, _priority_requests, _project_row_requests,
    _tab_format_requests, _v2_structure_requests,
)
from services.supabase_client import supabase_client            # noqa: E402


def _rebuild_rows(grid: list) -> list:
    """The tab's body as row-lists, so the per-row passes can see the kinds.

    Read back off the SHEET rather than rebuilt from the database — the point of
    this script is to restyle exactly what is there, including everything Eyal
    typed since the last rebuild.
    """
    return [list(r) for r in grid[3:]]


async def run(apply_it: bool) -> int:
    ssid = settings.PROJECT_STATUS_SHEET_ID
    msid = sheets_service.meetings_workbook()
    print("APPLY" if apply_it else "REPORT ONLY — re-run with --apply")
    print("=" * 72)

    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=ssid,
            fields="sheets(properties(title,sheetId),protectedRanges,"
                   "conditionalFormats)"))
    sheets = {s["properties"]["title"]: s for s in meta.get("sheets", [])}
    area_tabs = [t for t in sheets if t not in NON_AREA_TABS]

    grids = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().batchGet(
            spreadsheetId=ssid,
            ranges=[f"'{t}'!A1:N2000" for t in area_tabs]))
    by_tab = {t: (vr.get("values") or [])
              for t, vr in zip(area_tabs, grids.get("valueRanges", []))}

    print("\n  AREA TABS")
    reqs: list = []
    for tab in area_tabs:
        grid = by_tab[tab]
        if not grid:
            print(f"    .. {tab}: read back empty — skipped, nothing touched")
            continue
        sheet = sheets[tab]
        sid = sheet["properties"]["sheetId"]
        blocks, orphans, _cols = parse_tab(grid)
        rows = _rebuild_rows(grid)
        projects = sum(1 for b in blocks
                       if b.project and b.project.kind == SYSTEM_PROJECT)
        actions = sum(1 for b in blocks for a in b.actions
                      if a.kind == SYSTEM_ACTION)
        print(f"    ok {tab}: {len(rows)} row(s), {projects} project(s), "
              f"{actions} action(s)")

        # Drop what would otherwise stack: old colour rules and the protection
        # this module re-adds. Descending index, or each delete shifts the next.
        for i in range(len(sheet.get("conditionalFormats") or []) - 1, -1, -1):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid,
                                                         "index": i}})
        for pr in (sheet.get("protectedRanges") or []):
            if str(pr.get("description") or "").startswith("Gianluigi"):
                reqs.append({"deleteProtectedRange": {
                    "protectedRangeId": pr["protectedRangeId"]}})

        reqs.extend(_tab_format_requests(sid))
        reqs.extend(_v2_structure_requests(
            sid, settings.PROJECT_STATUS_DUE_SOON_DAYS))
        reqs.extend(_header_note_requests(sid))
        # Per-row, computed from what the tab ACTUALLY holds right now.
        reqs.extend(_checkbox_requests(sid, rows))
        reqs.extend(_priority_requests(sid, rows))
        reqs.extend(_date_validation_requests(sid, rows))
        reqs.extend(_project_row_requests(sid, rows))
        reqs.extend(_marker_bold_requests(sid, rows))

    print("\n  MEETINGS TABS")
    mmeta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=msid,
            fields="sheets(properties(title,sheetId),protectedRanges,"
                   "conditionalFormats)"))
    msheets = {s["properties"]["title"]: s for s in mmeta.get("sheets", [])}
    try:
        names = sorted(p["name"] for p in
                       (supabase_client.get_canonical_projects(status="active") or [])
                       if p.get("name"))
    except Exception:                                           # noqa: BLE001
        names = []
    mreqs: list = []
    for tab, headers in ((MEETING_TAB_NAME, MEETING_TRACKER_HEADERS),
                         (MEETINGS_ARCHIVE_TAB_NAME, MEETINGS_ARCHIVE_HEADERS)):
        if tab not in msheets:
            print(f"    .. {tab}: not in the workbook")
            continue
        sheet = msheets[tab]
        sid = sheet["properties"]["sheetId"]
        stale = [pr for pr in (sheet.get("protectedRanges") or [])
                 if str(pr.get("description") or "").startswith("Gianluigi")]
        print(f"    ok {tab}: {len(sheet.get('conditionalFormats') or [])} colour "
              f"rule(s), {len(stale)} stale protected range(s)")
        for i in range(len(sheet.get("conditionalFormats") or []) - 1, -1, -1):
            mreqs.append({"deleteConditionalFormatRule": {"sheetId": sid,
                                                          "index": i}})
        for pr in stale:
            mreqs.append({"deleteProtectedRange": {
                "protectedRangeId": pr["protectedRangeId"]}})
        mreqs.extend(sheets_service.meetings_format_requests(sid, headers, names))
        raw = sheets_service._execute_with_retry(
            lambda t=tab: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=msid, range=f"'{t}'!A4:I2000")).get("values", [])
        mreqs.extend(sheets_service.meeting_marker_requests(sid, raw))

    print(f"\n  {len(reqs)} request(s) for the area tabs, {len(mreqs)} for the "
          "meetings tabs — formatting only, no cell value is written")
    if not apply_it:
        print("\n  Nothing was written.")
        return 0

    if reqs:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=ssid, body={"requests": reqs}))
        print("  area tabs restyled")
    if mreqs:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=msid, body={"requests": mreqs}))
        print("  meetings tabs restyled")
    print("\n  DONE")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(run(ap.parse_args().apply)))
