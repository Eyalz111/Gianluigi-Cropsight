"""Rebuild `Meetings` + `Past Meetings` into the simplified, aligned layout.

WHAT CHANGES
------------
    Meeting | Project | Led By | Proposed Date | Participants | Status   (+ hidden _id)

Agenda, Prep Needed and Source Meeting are gone from the sheet — they were
write-only there and still live in the database. Source Meeting survives as a
`[auto · meeting · date]` chip inside the title. `_id` is HIDDEN rather than
removed: without it the reconcile cannot tell which meeting a row IS, which is
the defect that made "Schedule: X" rows multiply forever.

The tab also gains the three-row header block the area tabs use, so the workbook
reads as one document.

WHY REBUILD RATHER THAN MIGRATE IN PLACE
----------------------------------------
Every row moves down two and loses three columns. Rewriting from the DATABASE is
simpler than transforming cells in place and, more importantly, it is verifiable
— the database is the source of truth for all six fields and the id, so the
check afterwards is "does the sheet now say what the database says?" rather than
"did my transform preserve everything?".

The one thing the database does NOT hold is a row somebody typed that has not
been created yet. So the script REFUSES to run while any such row exists: those
rows are created and stamped on the next reconcile cycle, and rebuilding first
would erase them. Wait one cycle, then re-run.

    python scripts/rollout_meetings_redesign_2026_08.py            # dry run
    python scripts/rollout_meetings_redesign_2026_08.py --apply
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings                            # noqa: E402
from services.google_sheets import (                            # noqa: E402
    MEETINGS_ARCHIVE_HEADERS, MEETINGS_ARCHIVE_TAB_NAME,
    MEETING_FIRST_BODY_ROW, MEETING_HEADER_ROW, MEETING_TAB_NAME,
    MEETING_TRACKER_HEADERS, MEETING_VISIBLE_COLUMNS,
    meetings_header_block, sheets_service,
)
from services.supabase_client import supabase_client            # noqa: E402

_CLOSED = ("held", "dropped")


def _end_col(headers: list) -> str:
    return chr(ord("A") + len(headers) - 1)


def _sheet_ids() -> dict:
    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=sheets_service.meetings_workbook(),
            fields="sheets.properties"))
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])}


async def _read_rows_either_layout(new_layout: bool) -> list:
    """[{row, title, id}] from the Meetings tab, whichever shape it is in.

    OLD: header on row 1, data from row 2, `_id` in column J (index 9).
    NEW: header on row 3, data from row 4, `_id` in column G (index 6).
    """
    if new_layout:
        return [{"row": r.get("row_number"), "title": (r.get("title") or "").strip(),
                 "id": (r.get("id") or "").strip()}
                for r in await sheets_service.get_all_meetings()]

    raw = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().get(
            spreadsheetId=sheets_service.meetings_workbook(),
            range=f"'{MEETING_TAB_NAME}'!A2:J")).get("values", [])
    out = []
    for i, row in enumerate(raw, start=2):
        row = list(row) + [""] * 10
        title = str(row[0]).strip()
        if not title:
            continue
        out.append({"row": i, "title": title, "id": str(row[9]).strip()})
    return out


def _rows_for(tab: str, meetings: list, archived_on: dict) -> list:
    """Build a tab's data rows from the database records."""
    out = []
    for m in sorted(meetings, key=lambda r: (
            (r.get("title") or "").lower())):
        row = sheets_service._meeting_row(m)
        if tab == MEETINGS_ARCHIVE_TAB_NAME:
            moved = archived_on.get(m["id"]) or ""
            out.append(row[:MEETING_VISIBLE_COLUMNS] + [moved, row[-1]])
        else:
            out.append(row)
    return out


async def run(apply_it: bool) -> int:
    ssid = sheets_service.meetings_workbook()
    print("APPLY" if apply_it else "DRY RUN (nothing written) — re-run with --apply")
    print("=" * 72)
    print(f"  workbook: {ssid}")
    if not ssid:
        print("\n  ABORTED — no meetings workbook configured.")
        return 1

    print("\n  1. PREFLIGHT")
    tabs = _sheet_ids()
    for tab in (MEETING_TAB_NAME, MEETINGS_ARCHIVE_TAB_NAME):
        if tab not in tabs:
            print(f"    [FAIL] {tab!r} is not in the workbook")
            return 1

    # A row with no UUID has not been created yet and exists ONLY on the sheet.
    # Rebuilding from the database would erase it without a trace.
    #
    # READ IT WITH WHATEVER LAYOUT THE TAB IS ACTUALLY ON. This script exists
    # precisely because the tab is on the OLD shape, so get_all_meetings — which
    # now maps the NEW one — reads the identity out of the old "Agenda" column
    # and reports all 36 rows as hand-typed. The check has to be right in both
    # states or the migration can never start.
    new_layout = await sheets_service.meetings_layout_ok()
    print(f"    [OK ] Meetings tab is on the "
          f"{'NEW' if new_layout else 'OLD'} layout")
    live = await _read_rows_either_layout(new_layout)
    pending = [r for r in live if not r["id"] and r["title"]]
    if pending:
        print(f"    [FAIL] {len(pending)} hand-typed row(s) have no UUID yet — "
              "they exist ONLY on the sheet")
        for r in pending[:5]:
            print(f"           r{r['row']}: {r['title'][:60]}")
        print("           Wait for one reconcile cycle to create them, then re-run.")
        return 1
    print(f"    [OK ] {len(live)} row(s) on the Meetings tab, all with a UUID")

    db = supabase_client.list_follow_up_meetings(limit=2000,
                                                 include_pending=True) or []
    if not db:
        print("    [FAIL] the database returned NO meetings — refusing to "
              "rebuild from an empty read")
        return 1
    working = [m for m in db
               if (m.get("status") or "").strip().lower() not in _CLOSED]
    closed = [m for m in db
              if (m.get("status") or "").strip().lower() in _CLOSED]
    print(f"    [OK ] database: {len(db)} meeting(s) — {len(working)} working, "
          f"{len(closed)} held/dropped")

    projects = sorted(p["name"] for p in
                      (supabase_client.get_canonical_projects(status="active") or [])
                      if p.get("name"))
    print(f"    [OK ] {len(projects)} canonical project(s) for the dropdown")
    off_vocab = sorted({(m.get("label") or "").strip() for m in db
                        if (m.get("label") or "").strip()
                        and (m.get("label") or "").strip() not in projects})
    if off_vocab:
        print(f"    [ .. ] {len(off_vocab)} label(s) are not canonical projects "
              f"and will show a warning triangle: {off_vocab}")
        print("           They are LEFT AS TYPED — nothing is silently erased.")

    # What "Moved" should say for an archived row: keep whatever the tab already
    # records, so a rebuild does not restamp history with today's date.
    archived_on = {}
    try:
        end = _end_col(MEETINGS_ARCHIVE_HEADERS)
        raw = sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=ssid,
                range=f"'{MEETINGS_ARCHIVE_TAB_NAME}'!A:{end}")).get("values", [])
        for r in raw:
            # Read by POSITION from the OLD layout (id was column J, 10th).
            if len(r) >= 10 and str(r[9]).strip():
                archived_on[str(r[9]).strip()] = str(r[10]).strip() if len(r) > 10 else ""
    except Exception as e:                                      # noqa: BLE001
        print(f"    [ .. ] could not read the existing Moved dates ({e}) — "
              "archived rows will be stamped today")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for m in closed:
        archived_on.setdefault(m["id"], today)

    plan = {
        MEETING_TAB_NAME: _rows_for(MEETING_TAB_NAME, working, archived_on),
        MEETINGS_ARCHIVE_TAB_NAME: _rows_for(
            MEETINGS_ARCHIVE_TAB_NAME, closed, archived_on),
    }
    print(f"\n  WOULD WRITE  {len(plan[MEETING_TAB_NAME])} working meeting(s) "
          f"and {len(plan[MEETINGS_ARCHIVE_TAB_NAME])} past meeting(s)")
    if not apply_it:
        print("\n  Nothing was written.")
        return 0

    print("\n  2. BACKUP")
    from services.google_drive import drive_service
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H%M")
    copy = drive_service._execute_with_retry(
        lambda: drive_service.service.files().copy(
            fileId=ssid,
            body={"name": f"CropSight — Projects Status (before meetings "
                          f"redesign {stamp} UTC)"},
            supportsAllDrives=True, fields="id"))
    print(f"      copied to {copy['id']}")

    print("\n  3. REWRITE")
    for tab, rows in plan.items():
        headers = (MEETING_TRACKER_HEADERS if tab == MEETING_TAB_NAME
                   else MEETINGS_ARCHIVE_HEADERS)
        title = "MEETINGS" if tab == MEETING_TAB_NAME else "PAST MEETINGS"
        # The WHOLE tab, banner included — the old header sat on row 1 and would
        # otherwise survive as a stray row of column names above the new block.
        sheets_service._execute_with_retry(
            lambda t=tab: sheets_service.service.spreadsheets().values().clear(
                spreadsheetId=ssid, range=f"'{t}'", body={}))
        values = meetings_header_block(headers, title) + rows
        sheets_service._execute_with_retry(
            lambda t=tab, v=values: sheets_service.service.spreadsheets()
            .values().update(
                spreadsheetId=ssid, range=f"'{t}'!A1",
                valueInputOption="RAW", body={"values": v}))
        print(f"      {tab}: header block + {len(rows)} row(s)")

    print("\n  4. FORMAT")
    reqs = []
    for tab, headers in ((MEETING_TAB_NAME, MEETING_TRACKER_HEADERS),
                         (MEETINGS_ARCHIVE_TAB_NAME, MEETINGS_ARCHIVE_HEADERS)):
        sid = tabs[tab]
        reqs.extend(sheets_service._clear_conditional_format_rules_for_sheet(
            ssid, sid))
        reqs.extend(sheets_service.meetings_format_requests(
            sid, headers, projects))
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=ssid, body={"requests": reqs}))
    print(f"      applied {len(reqs)} formatting request(s) to both tabs")

    print("\n  5. VERIFY (read back)")
    back = await sheets_service.get_all_meetings()
    want_ids = {m["id"] for m in working}
    got_ids = {(r.get("id") or "").strip() for r in back}
    ok = want_ids == got_ids
    print(f"      {'OK ' if ok else 'BAD'} Meetings: {len(back)} row(s), "
          f"{len(got_ids & want_ids)}/{len(want_ids)} identities match")
    if not ok:
        print(f"      ** missing: {sorted(want_ids - got_ids)[:5]}")
        print(f"      ** unexpected: {sorted(got_ids - want_ids)[:5]}")
        print(f"      the pre-change copy is {copy['id']}")
        return 1
    blanks = [r for r in back if not (r.get("status") or "").strip()]
    if blanks:
        print(f"      ** {len(blanks)} row(s) have no status")
        return 1
    print(f"\n  DONE  https://docs.google.com/spreadsheets/d/{ssid}/edit")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(run(ap.parse_args().apply)))
