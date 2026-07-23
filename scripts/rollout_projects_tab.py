#!/usr/bin/env python3
"""
Create + seed the Projects tab from canonical_projects. [2026-07-23]

The editable face of the vocabulary: see every project + aliases + Area, add a
project by typing a row, RENAME by editing the Name cell (the reconcile
backfills every reference). Run once, then flip PROJECTS_RECONCILE_ENABLED
(shadow first).

Dry run by default. The tab is generated from the DB, so there is no snapshot to
seed and no destructive path — --rollback just clears the tab it created.

Usage:
    python scripts/rollout_projects_tab.py            # dry run
    python scripts/rollout_projects_tab.py --apply
    python scripts/rollout_projects_tab.py --rollback
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def _run(apply: bool) -> int:
    from services.google_sheets import sheets_service, PROJECTS_HEADERS
    from services.supabase_client import supabase_client as sc

    projects = sc.get_canonical_projects(status="active")
    areas = sc.get_areas()
    area_names = {a["id"]: a.get("name", "") for a in areas}
    logger.info(f"canonical projects: {len(projects)}  |  areas: {len(areas)}")
    if not projects:
        logger.error("No canonical projects — nothing to seed.")
        return 1

    print(f"\n=== Projects tab ({PROJECTS_HEADERS}) ===")
    for p in sorted(projects, key=lambda x: (x.get("name") or "").lower()):
        area = area_names.get(p.get("area_id"), "(no area)")
        al = ", ".join(p.get("aliases") or [])
        print(f"  {p.get('name'):<28} area={area:<32} aliases=[{al}]")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    ok = await sheets_service.rebuild_projects_sheet(projects, area_names, force_empty=True)
    print(f"\nProjects tab built: {ok}")
    print("Next: PROJECTS_RECONCILE_ENABLED=true (+ shadow), verify a rename preview, flip shadow off.")
    return 0 if ok else 1


async def _rollback() -> int:
    from config.settings import settings
    from services.google_sheets import sheets_service, PROJECTS_TAB_NAME, PROJECTS_COLUMNS

    end_col = max(PROJECTS_COLUMNS.values())
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().clear(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            range=f"'{PROJECTS_TAB_NAME}'!A2:{end_col}", body={},
        )
    )
    logger.info("Cleared the Projects tab data rows (canonical_projects untouched).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()
    if args.rollback:
        return asyncio.run(_rollback())
    return asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
