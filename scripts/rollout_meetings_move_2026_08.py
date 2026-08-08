"""Move the `Meetings` + `Past Meetings` tabs into the Project Status workbook.

WHY
---
Eyal now works entirely in the Project Status file. Meetings have nowhere to
live there, so anything meeting-shaped keeps landing in the action list as a
task — "Coordinate in-person meeting with Calabria region president" was sitting
in the open-task pool the morning this was written. Putting the pool beside the
work it belongs to is the fix; the Task Tracker becomes a read-only mirror of
tasks and nothing else.

HOW IT IS SAFE
--------------
1. `sheets.copyTo` COPIES each tab into the destination — values, formatting,
   validation and all — so nothing is deleted to make the move happen.
2. The copies are verified row-for-row and UUID-for-UUID against the source
   BEFORE the source is touched at all.
3. The source tabs are then RENAMED, not deleted. Nothing can read a tab called
   `Meetings` in the tracker any more, and every row is still recoverable.
   Rollback is: rename them back, clear MEETINGS_SHEET_ID.
4. Tabs are appended LAST in the destination. A tab at index 0 is what silently
   broke every sheet read in April 2026, because bare A1 ranges resolve against
   whichever sheet sits first — the same note `_ensure_tab` carries.

Dress-rehearse against a COPY of the destination first (--dest <copy id>). The
structural pass on 2026-08-08 is the precedent: shadow mode cannot exercise a
tab move, so the only honest rehearsal is a real one somewhere harmless.

    python scripts/rollout_meetings_move_2026_08.py                          # dry run
    python scripts/rollout_meetings_move_2026_08.py --apply --dest <copy>    # rehearsal

    python scripts/rollout_meetings_move_2026_08.py --apply                  # 1. copy
    gcloud run services update gianluigi --region europe-west1 \\             # 2. flip
        --update-env-vars MEETINGS_SHEET_ID=<project status sheet id>
    python scripts/rollout_meetings_move_2026_08.py --apply --retire-source  # 3. rename
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings                       # noqa: E402
from services.google_sheets import (                       # noqa: E402
    MEETING_COL_INDEX, MEETING_TAB_NAME, MEETINGS_ARCHIVE_TAB_NAME,
    sheets_service,
)

TABS = [MEETING_TAB_NAME, MEETINGS_ARCHIVE_TAB_NAME]


def _tabs(ssid: str) -> dict:
    """{title: sheetId} for a workbook."""
    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=ssid, fields="sheets.properties"))
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])}


def _rows(ssid: str, tab: str) -> list:
    """Data rows of a tab (header excluded), as raw lists."""
    try:
        return sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=ssid, range=f"'{tab}'!A2:Z")).get("values", []) or []
    except Exception:                                       # noqa: BLE001
        return []


def _ids(rows: list) -> list:
    col = MEETING_COL_INDEX["id"]
    return [str(r[col]).strip() for r in rows
            if len(r) > col and str(r[col]).strip()]


def _titles(rows: list) -> list:
    col = MEETING_COL_INDEX["title"]
    return [str(r[col]).strip() for r in rows
            if len(r) > col and str(r[col]).strip()]


async def run(apply_it: bool, dest: str, retire_only: bool = False) -> int:
    src = settings.TASK_TRACKER_SHEET_ID
    print("APPLY" if apply_it else "DRY RUN (nothing written) — re-run with --apply")
    print("=" * 72)
    print(f"  source      {src}")
    print(f"  destination {dest}")
    if not src or not dest:
        print("\n  ABORTED — both workbook ids are required.")
        return 1
    if src == dest:
        print("\n  ABORTED — source and destination are the same workbook.")
        return 1

    src_tabs, dest_tabs = _tabs(src), _tabs(dest)

    if retire_only:
        # Step 3 of the cutover: the copies are live and production is already
        # reading them. Re-verify before renaming anything — this is the last
        # point at which the originals are still findable under their own name.
        print("\n  RETIRE THE ORIGINALS (renamed, not deleted)")
        for tab in TABS:
            if tab not in dest_tabs:
                print(f"    [FAIL] {tab!r} is not in the destination — copy first")
                return 1
            n_src, n_dst = len(_rows(src, tab)), len(_rows(dest, tab))
            if n_dst < n_src:
                print(f"    [FAIL] {tab!r}: destination has {n_dst} row(s), "
                      f"source has {n_src} — refusing to retire the original")
                return 1
            print(f"    [OK ] {tab!r}: {n_dst} row(s) in the destination "
                  f"({n_src} in the source)")
        if not apply_it:
            print("\n  WOULD rename the source tabs. Nothing was written.")
            return 0
        return _retire(src, src_tabs)

    print("\n  1. PREFLIGHT")
    before = {}
    ok = True
    for tab in TABS:
        if tab not in src_tabs:
            print(f"    [FAIL] {tab!r} is not in the source workbook")
            ok = False
            continue
        rows = _rows(src, tab)
        before[tab] = rows
        print(f"    [OK ] {tab!r}: {len(rows)} row(s), {len(_ids(rows))} with a UUID")
        if tab in dest_tabs:
            existing = _rows(dest, tab)
            if existing:
                print(f"    [FAIL] destination already has {tab!r} with "
                      f"{len(existing)} row(s) — refusing to write over it")
                ok = False
            else:
                print(f"    [OK ] destination has an EMPTY {tab!r}; it will be "
                      "replaced")
    if not ok:
        print("\n  ABORTED — nothing was written.")
        return 1

    if not apply_it:
        print("\n  WOULD copy both tabs to the destination (appended last), "
              "verify row-for-row, then rename the originals.")
        print("  Nothing was written.")
        return 0

    print("\n  2. COPY")
    for tab in TABS:
        # An empty same-named tab in the destination would collide with the
        # copy's rename, so it goes first — verified empty in the preflight.
        if tab in dest_tabs:
            sheets_service._execute_with_retry(
                lambda t=tab: sheets_service.service.spreadsheets().batchUpdate(
                    spreadsheetId=dest,
                    body={"requests": [{"deleteSheet": {
                        "sheetId": dest_tabs[t]}}]}))
            print(f"      removed the empty placeholder {tab!r}")

        copied = sheets_service._execute_with_retry(
            lambda t=tab: sheets_service.service.spreadsheets().sheets().copyTo(
                spreadsheetId=src, sheetId=src_tabs[t],
                body={"destinationSpreadsheetId": dest}))
        new_id = copied["sheetId"]
        # copyTo names it "Copy of X" and places it last, which is what we want
        # — index 0 is the trap. Only the name needs fixing.
        n_tabs = len(_tabs(dest))
        sheets_service._execute_with_retry(
            lambda t=tab, i=new_id, n=n_tabs: sheets_service.service.spreadsheets()
            .batchUpdate(spreadsheetId=dest, body={"requests": [
                {"updateSheetProperties": {
                    "properties": {"sheetId": i, "title": t, "index": n - 1},
                    "fields": "title,index"}}]}))
        print(f"      copied {tab!r} -> sheetId {new_id}, appended last")

    print("\n  3. VERIFY (row-for-row, before anything is touched)")
    for tab in TABS:
        after = _rows(dest, tab)
        src_ids, dst_ids = _ids(before[tab]), _ids(after)
        src_titles, dst_titles = _titles(before[tab]), _titles(after)
        same = (len(after) == len(before[tab])
                and src_ids == dst_ids and src_titles == dst_titles)
        print(f"      {'OK ' if same else 'BAD'} {tab!r}: "
              f"{len(before[tab])} -> {len(after)} row(s), "
              f"{len(src_ids)} -> {len(dst_ids)} UUID(s)")
        if not same:
            print("      ** MISMATCH — the source is UNTOUCHED. Delete the "
                  "copies in the destination and investigate.")
            return 1

    # THE COPY AND THE RETIRE ARE TWO SEPARATE RUNS, AND THAT IS THE POINT.
    #
    # Production reads the Task Tracker until MEETINGS_SHEET_ID is flipped. Do
    # both halves in one invocation and there is a gap — minutes, but the
    # meetings reconcile runs every 30 and writes — in which prod looks for a
    # tab that no longer exists under that name. Splitting them puts the env
    # flip in between, so at no moment does the workbook prod is reading lack
    # the tabs. It is also what makes a rehearsal against a throwaway copy
    # honest: the first rehearsal renamed the live tabs anyway, recovered in a
    # minute only because the step is a rename and not a delete — which is the
    # whole reason it is a rename. [2026-08-09]
    print("\n  4. RETIRE THE ORIGINALS — NOT IN THIS RUN")
    print(f"\n  COPIED AND VERIFIED into {dest}. The source is untouched, so "
          "production keeps reading it until you flip the setting:")
    print(f"    gcloud run services update gianluigi --region europe-west1 \\\n"
          f"        --update-env-vars MEETINGS_SHEET_ID={dest}")
    print("  Then re-run with --retire-source to rename the originals.")
    return 0


def _retire(src: str, src_tabs: dict) -> int:
    """Rename the source tabs. NEVER deletes — that is what makes this undoable."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    reqs = [{"updateSheetProperties": {
        "properties": {"sheetId": src_tabs[t], "title": f"{t} (moved {stamp})"},
        "fields": "title"}} for t in TABS if t in src_tabs]
    if not reqs:
        print("      nothing to retire — the source tabs are already renamed")
        return 0
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=src, body={"requests": reqs}))
    for t in TABS:
        if t in src_tabs:
            print(f"      {t!r} -> {t + f' (moved {stamp})'!r}")
    print("\n  DONE. Rollback: rename these two tabs back and clear "
          "MEETINGS_SHEET_ID.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--retire-source", action="store_true",
                    help="step 3: rename the source tabs, AFTER the env flip")
    ap.add_argument("--dest", default="",
                    help="destination workbook; defaults to PROJECT_STATUS_SHEET_ID. "
                         "Point it at a COPY for the dress rehearsal.")
    a = ap.parse_args()
    sys.exit(asyncio.run(run(a.apply, a.dest or settings.PROJECT_STATUS_SHEET_ID,
                             retire_only=a.retire_source)))
