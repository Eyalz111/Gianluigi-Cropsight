#!/usr/bin/env python3
"""
Roll out the Tasks-sheet 'Last Update' column (L) + header renames. [2026-07-22]

Run ONCE, after TASK_SHEET_LAST_UPDATE_ENABLED=true is live. Three steps:

  1. Rewrite the header row  — Label -> Project, Category -> Area, + Last Update.
     ensure_task_tracker_headers only fires on an EMPTY sheet, so the renames
     never happen by themselves; this is the deliberate action.
  2. Backfill col L from each task's DB updated_at, matched by the col-J UUID.
     Without this every existing row shows blank and the staleness formatting
     has nothing to colour.
  3. Re-apply formatting — widths (one per column now), the H:J and L protected
     ranges, and the 60d/30d staleness rules.

SAFETY
  - Dry run is the DEFAULT; --apply is required to write.
  - Reads the sheet first and refuses to touch an empty/short read — the same
    class of guard that exists in reconcile, because a transient Sheets read
    returning [] is what wiped this sheet in April.
  - Writes ONLY column L and the header row. Never clears, never reorders, never
    touches a data cell in A:K.
  - Snapshots the existing header row so --rollback can restore it.

Usage:
    python scripts/rollout_tasks_sheet_l_column.py            # dry run
    python scripts/rollout_tasks_sheet_l_column.py --apply
    python scripts/rollout_tasks_sheet_l_column.py --rollback FILE
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backfill_snapshots")


async def _run(apply: bool) -> int:
    from config.settings import settings
    from services.google_sheets import (
        sheets_service, TASK_COLUMNS, TASK_TRACKER_HEADERS, _fmt_day,
    )
    from services.supabase_client import supabase_client as sc

    if "last_update" not in TASK_COLUMNS:
        logger.error(
            "TASK_SHEET_LAST_UPDATE_ENABLED is off in THIS process — the column "
            "map has no 'last_update'. Set it before running:  "
            "$env:TASK_SHEET_LAST_UPDATE_ENABLED='true'"
        )
        return 1

    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"
    last_col = max(TASK_COLUMNS.values())

    sheet_rows = await sheets_service.get_all_tasks()
    if not sheet_rows:
        logger.error("Sheet read returned 0 rows — refusing to write (bad read guard).")
        return 1
    logger.info(f"Sheet rows: {len(sheet_rows)}")

    db_tasks = sc.get_tasks(status=None, limit=5000, include_pending=True,
                            include_archived=True)
    by_id = {t["id"]: t for t in db_tasks if t.get("id")}
    logger.info(f"DB tasks: {len(db_tasks)}")

    # --- plan column L ---
    updates, missing = [], 0
    for r in sheet_rows:
        rid = str(r.get("id") or "").strip()
        row_no = r.get("row_number")
        if not rid or not row_no:
            missing += 1
            continue
        dt = by_id.get(rid)
        if not dt:
            missing += 1
            continue
        want = _fmt_day(dt.get("updated_at"))
        if want and want != (r.get("last_update") or "").strip():
            updates.append({
                "range": f"'{tab}'!{TASK_COLUMNS['last_update']}{row_no}",
                "values": [[want]],
            })

    current_header = None
    try:
        hdr = await sheets_service._read_sheet_range(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            range_name=f"'{tab}'!A1:{last_col}1",
        )
        current_header = hdr[0] if hdr else None
    except Exception as e:
        logger.warning(f"Could not read current header: {e}")

    # GUARD: this script runs LOCALLY against the live sheet, so its column map
    # comes from local env flags. If prod has a column this process doesn't
    # (e.g. TASK_SHEET_URGENCY_AREA_ENABLED unset here but true in prod), the
    # header write would be NARROWER than the sheet and would blank a real
    # header cell. Refuse rather than truncate.
    if current_header and len(current_header) > len(TASK_TRACKER_HEADERS):
        logger.error(
            f"Sheet header has {len(current_header)} columns but this process "
            f"builds only {len(TASK_TRACKER_HEADERS)} "
            f"({TASK_TRACKER_HEADERS}). Writing it would blank the extra "
            f"column(s): {current_header[len(TASK_TRACKER_HEADERS):]}. "
            f"Set the missing flags and re-run — e.g. "
            f"$env:TASK_SHEET_URGENCY_AREA_ENABLED='true'"
        )
        return 1

    print("\n=== HEADER ROW ===")
    print(f"  before: {current_header}")
    print(f"  after : {TASK_TRACKER_HEADERS}")
    print(f"\n=== COLUMN L BACKFILL ===")
    print(f"  rows to write : {len(updates)}")
    print(f"  rows skipped  : {missing} (no col-J UUID, or no matching DB row)")
    print(f"  sample        : {[u['values'][0][0] for u in updates[:5]]}")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = os.path.join(_SNAPSHOT_DIR, f"tasks_sheet_header_{stamp}.json")
    with open(snap, "w", encoding="utf-8") as f:
        json.dump({"header": current_header, "range": f"'{tab}'!A1:{last_col}1"},
                  f, indent=2, ensure_ascii=False)
    logger.info(f"Header snapshot: {snap}")

    # 1. header row
    await sheets_service._write_sheet_range(
        sheet_id=settings.TASK_TRACKER_SHEET_ID,
        range_name=f"'{tab}'!A1:{last_col}1",
        values=[TASK_TRACKER_HEADERS],
    )
    logger.info("Header row written")

    # 2. column L, one batched write
    if updates:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().batchUpdate(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                body={"valueInputOption": "RAW", "data": updates},
            )
        )
        logger.info(f"Wrote Last Update for {len(updates)} row(s)")

    # 3. formatting
    await sheets_service.format_task_tracker()
    logger.info("Formatting re-applied (widths, protection, staleness rules)")

    sc.log_action(
        action="tasks_sheet_l_column_rollout",
        details={"rows_written": len(updates), "skipped": missing,
                 "header": TASK_TRACKER_HEADERS},
        triggered_by="eyal",
    )
    print(f"\nDone. Rollback header:  python scripts/rollout_tasks_sheet_l_column.py --rollback {snap}")
    return 0


async def _rollback(path: str) -> int:
    from config.settings import settings
    from services.google_sheets import sheets_service

    with open(path, encoding="utf-8") as f:
        snap = json.load(f)
    if not snap.get("header"):
        logger.error("Snapshot has no header to restore.")
        return 1
    await sheets_service._write_sheet_range(
        sheet_id=settings.TASK_TRACKER_SHEET_ID,
        range_name=snap["range"],
        values=[snap["header"]],
    )
    logger.info("Header row restored (column L values left in place — harmless).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="FILE")
    args = ap.parse_args()
    if args.rollback:
        return asyncio.run(_rollback(args.rollback))
    return asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
