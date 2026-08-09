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
import re
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


_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _rows_on_the_tab() -> list:
    """[{row, title, id}] read WITHOUT assuming a layout.

    The question this has to answer is "does the sheet hold anything the
    database does not?", and asking it must not depend on the very geometry the
    script exists to change. A layout-specific reader got it wrong twice: once
    against the old shape (id read from the vacated Agenda column) and once
    against a half-migrated one.

    So: the title is the first non-empty cell, and the id is whatever cell in
    the row LOOKS like a UUID. Both are true in every layout this tab has ever
    had.
    """
    raw = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().get(
            spreadsheetId=sheets_service.meetings_workbook(),
            range=f"'{MEETING_TAB_NAME}'!A1:N400")).get("values", [])
    # Rows that are STRUCTURE, not meetings: the banner, the distribution line
    # and the column headers, in whichever position this layout puts them.
    structural = {"meetings", "past meetings", "meeting"}
    structural |= {h.lower() for h in MEETING_TRACKER_HEADERS}
    out = []
    for i, row in enumerate(raw, start=1):
        cells = [str(c).strip() for c in row]
        title = next((c for c in cells if c), "")
        if not title:
            continue
        low = title.lower()
        if low in structural or low.startswith("distribution:"):
            continue
        out.append({"row": i, "title": title,
                    "id": next((c for c in cells if _UUID.match(c)), "")})
    return out


def _operational_key(m: dict):
    """scheduled -> to schedule -> parked -> held -> dropped, then by date.

    The SAME order the every-cycle sort applies. Writing the rebuild in
    alphabetical order instead meant the tab was handed over wrong and only
    became right on the next cycle — which is the one moment somebody is
    actually looking at it.
    """
    from services.google_sheets import MEETING_DISPLAY_ORDER
    status = (m.get("status") or "not_scheduled").strip().lower()
    date = str(m.get("proposed_date") or "")
    return (MEETING_DISPLAY_ORDER.get(status, 1), 0 if date else 1,
            date or "9999-99-99", (m.get("title") or "").lower())


def _rows_for(tab: str, meetings: list, archived_on: dict) -> list:
    """Build a tab's data rows from the database records."""
    out = []
    for m in sorted(meetings, key=_operational_key):
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

    new_layout = await sheets_service.meetings_layout_ok()
    print(f"    [OK ] Meetings tab is on the "
          f"{'NEW' if new_layout else 'an older'} layout")

    db = supabase_client.list_follow_up_meetings(limit=2000,
                                                 include_pending=True) or []
    if not db:
        print("    [FAIL] the database returned NO meetings — refusing to "
              "rebuild from an empty read")
        return 1

    # THE ONE THING THE REBUILD COULD DESTROY: a row that exists only here.
    # Everything else is rewritten from the database, which is the source of
    # truth for all six fields and the id. A row is safe if its UUID is known
    # OR its text matches a meeting we already hold; anything else is somebody's
    # typing that has not been created yet, and rebuilding would erase it.
    from services.project_status_rows import strip_provenance
    known_ids = {m["id"] for m in db}
    known_titles = {" ".join((m.get("title") or "").split()).strip().lower()
                    for m in db}
    live = _rows_on_the_tab()
    unique = [r for r in live
              if r["id"] not in known_ids
              and " ".join(strip_provenance(r["title"]).split()).strip().lower()
              not in known_titles]
    if unique:
        print(f"    [FAIL] {len(unique)} row(s) exist ONLY on the sheet — "
              "rebuilding from the database would erase them")
        for r in unique[:6]:
            print(f"           r{r['row']}: {r['title'][:60]}")
        print("           Wait for one reconcile cycle to create them, then re-run.")
        return 1
    print(f"    [OK ] {len(live)} row(s) read; every one is already in the "
          "database, so nothing is lost by rewriting")
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
