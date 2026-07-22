#!/usr/bin/env python3
"""
Create + seed the Meetings tab, and backfill its snapshots. [2026-07-22]

Run ONCE after migrate_meeting_reconcile.sql, BEFORE flipping
MEETING_RECONCILE_ENABLED. Three steps:

  1. Create the 'Meetings' tab (appended last — never at index 0, which is what
     silently broke every sheet read in April 2026).
  2. Seed it from follow_up_meetings, sorted into a worklist: not_scheduled
     first, then oldest proposed date, so the most overdue booking is row 2.
  3. Backfill one sheet_snapshots row per meeting from the values just written.

Step 3 is the one that is easy to skip and expensive to skip. Without a
snapshot, the first reconcile sees `sheet != snapshot` for EVERY cell, attributes
all of it to a human, and mass-pulls the sheet into the DB while marking every
field manually-sticky. Seeding makes snap == sheet == db so the first run is a
clean no-op.

SAFETY
  - Dry run is the DEFAULT.
  - Refuses to seed a tab that already has data rows unless --force (re-running
    would duplicate all 101 meetings).
  - Never deletes; --rollback only clears the tab it created.

Usage:
    python scripts/rollout_meetings_tab.py            # dry run
    python scripts/rollout_meetings_tab.py --apply
    python scripts/rollout_meetings_tab.py --rollback
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def _run(apply: bool, force: bool) -> int:
    from config.settings import settings
    from services.google_sheets import (
        sheets_service, MEETING_TAB_NAME, MEETING_TRACKER_HEADERS, _sorted_meetings,
    )
    from services.supabase_client import supabase_client as sc

    meetings = sc.list_follow_up_meetings(limit=2000, include_pending=True)
    logger.info(f"follow_up_meetings in DB: {len(meetings)}")
    if not meetings:
        logger.error("No follow-up meetings found — nothing to seed.")
        return 1

    existing = []
    try:
        existing = await sheets_service.get_all_meetings()
    except Exception:
        pass  # tab does not exist yet — expected on the first run
    logger.info(f"Rows already on the {MEETING_TAB_NAME} tab: {len(existing)}")

    if existing and not force:
        logger.error(
            f"The {MEETING_TAB_NAME} tab already has {len(existing)} data row(s). "
            f"Seeding again would DUPLICATE every meeting. Re-run with --force "
            f"only if you intend to append anyway."
        )
        return 1

    ordered = _sorted_meetings(meetings)
    by_status: dict[str, int] = {}
    for m in ordered:
        s = m.get("status") or "not_scheduled"
        by_status[s] = by_status.get(s, 0) + 1

    print(f"\n=== {MEETING_TAB_NAME} TAB ===")
    print(f"  headers : {MEETING_TRACKER_HEADERS}")
    print(f"  rows    : {len(ordered)}")
    print(f"  status  : {by_status}")
    print("\n  first 8 rows (worklist order — not_scheduled first, oldest date first):")
    for m in ordered[:8]:
        print(f"    [{(m.get('status') or ''):<13}] {str(m.get('proposed_date') or '—')[:10]:<11} "
              f"{(m.get('title') or '')[:64]}")
    unlabelled = sum(1 for m in ordered if not (m.get("label") or "").strip())
    print(f"\n  without a Project label: {unlabelled}/{len(ordered)}")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    sid = await sheets_service.ensure_meetings_tab()
    if sid is None:
        logger.error("Could not create/find the Meetings tab.")
        return 1
    logger.info(f"{MEETING_TAB_NAME} tab ready (sheetId={sid})")

    ok = await sheets_service.add_meetings_batch_to_sheet(ordered)
    if not ok:
        logger.error("Seeding the tab failed — NOT writing snapshots.")
        return 1

    await sheets_service.format_meetings_tab()
    logger.info("Formatting applied")

    # Snapshots LAST, and read back from the SHEET so each snapshot records the
    # row number and the exact rendered values — not what we hoped we wrote.
    written = await sheets_service.get_all_meetings()
    by_id = {str(r.get("id") or ""): r for r in written}
    seeded = 0
    for m in ordered:
        r = by_id.get(m["id"])
        if not r:
            logger.warning(f"Seeded meeting {m['id']} not found on the tab — no snapshot")
            continue
        if sc.upsert_meeting_snapshot(
            m["id"], r.get("row_number"), r.get("title"), r.get("label"),
            r.get("led_by"), r.get("proposed_date"), r.get("participants"),
            r.get("status"),
        ):
            seeded += 1
    logger.info(f"Seeded {seeded} snapshot(s)")

    sc.log_action(
        action="meetings_tab_rollout",
        details={"rows": len(ordered), "snapshots": seeded},
        triggered_by="eyal",
    )
    print(f"\nDone: {len(ordered)} meeting(s) on the tab, {seeded} snapshot(s) seeded.")
    print("Next: MEETING_RECONCILE_ENABLED=true with MEETING_RECONCILE_SHADOW_MODE=true,")
    print("      inspect the shadow diff, then flip shadow off.")
    return 0


async def _rollback() -> int:
    from config.settings import settings
    from services.google_sheets import sheets_service, MEETING_TAB_NAME, MEETING_COLUMNS
    from services.supabase_client import supabase_client as sc

    end_col = max(MEETING_COLUMNS.values())
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().clear(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            range=f"'{MEETING_TAB_NAME}'!A2:{end_col}",
            body={},
        )
    )
    sc.client.table("sheet_snapshots").delete().eq("entity_type", "meeting").execute()
    logger.info("Cleared the Meetings tab data rows + their snapshots "
                "(follow_up_meetings rows untouched).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="seed even if the tab already has rows (will duplicate)")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()
    if args.rollback:
        return asyncio.run(_rollback())
    return asyncio.run(_run(apply=args.apply, force=args.force))


if __name__ == "__main__":
    sys.exit(main())
