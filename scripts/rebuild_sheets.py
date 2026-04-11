#!/usr/bin/env python3
"""
Phase 10: Combined Sheets Rebuild Script.

Rebuilds both Tasks and Decisions sheets from Supabase (source of truth),
applying the Phase 10 column layout and formatting.

Steps:
1. Backup current sheet tabs (Tasks_Backup_YYYYMMDD, Decisions_Backup_YYYYMMDD)
2. Backfill decision labels (if --backfill-labels flag)
3. Rebuild Tasks sheet from Supabase
4. Rebuild Decisions sheet from Supabase
5. Remove Commitments tab if it exists

Usage:
    python scripts/rebuild_sheets.py                    # rebuild only
    python scripts/rebuild_sheets.py --backfill-labels  # backfill then rebuild
    python scripts/rebuild_sheets.py --skip-backup      # skip backup step
"""

import argparse
import asyncio
import logging
import sys
import os
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def backup_tab(tab_name: str) -> str | None:
    """
    Copy a sheet tab to a backup tab named {tab_name}_Backup_YYYYMMDD.

    Returns the backup tab name, or None if backup failed.
    """
    from services.google_sheets import sheets_service

    if not settings.TASK_TRACKER_SHEET_ID:
        return None

    backup_name = f"{tab_name}_Backup_{datetime.now().strftime('%Y%m%d')}"

    try:
        svc = sheets_service.service
        meta = svc.spreadsheets().get(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            fields="sheets.properties",
        ).execute()

        # Find the source tab
        source_id = None
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == tab_name:
                source_id = props["sheetId"]
                break

        if source_id is None:
            logger.warning(f"Tab '{tab_name}' not found, skipping backup")
            return None

        # Remove existing backup tab with same name (if re-running same day)
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == backup_name:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": [{"deleteSheet": {"sheetId": props["sheetId"]}}]},
                ).execute()
                break

        # Duplicate the tab
        svc.spreadsheets().batchUpdate(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            body={"requests": [{
                "duplicateSheet": {
                    "sourceSheetId": source_id,
                    "newSheetName": backup_name,
                }
            }]},
        ).execute()

        logger.info(f"Backed up '{tab_name}' → '{backup_name}'")
        return backup_name

    except Exception as e:
        logger.error(f"Failed to backup '{tab_name}': {e}")
        return None


async def remove_commitments_tab() -> bool:
    """Remove the Commitments tab if it exists."""
    from services.google_sheets import sheets_service

    if not settings.TASK_TRACKER_SHEET_ID:
        return False

    try:
        svc = sheets_service.service
        meta = svc.spreadsheets().get(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            fields="sheets.properties",
        ).execute()

        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == "Commitments":
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": [{"deleteSheet": {"sheetId": props["sheetId"]}}]},
                ).execute()
                logger.info("Removed Commitments tab")
                return True

        logger.info("No Commitments tab found (already removed)")
        return True

    except Exception as e:
        logger.error(f"Failed to remove Commitments tab: {e}")
        return False


async def main():
    parser = argparse.ArgumentParser(description="Rebuild Tasks and Decisions sheets")
    parser.add_argument("--backfill-labels", action="store_true",
                        help="Run Haiku decision label backfill before rebuild")
    parser.add_argument("--skip-backup", action="store_true",
                        help="Skip the backup step")
    args = parser.parse_args()

    from services.supabase_client import supabase_client
    from services.google_sheets import sheets_service

    # Step 0: Verify connection
    logger.info("Verifying Google Sheets connection...")
    if not await sheets_service.authenticate():
        logger.error("Google Sheets auth failed. Check credentials.")
        return

    # Step 1: Backup
    if not args.skip_backup:
        logger.info("\n=== Step 1: Backing up current sheets ===")
        tab_name = settings.TASK_TRACKER_TAB_NAME or "Tasks"
        await backup_tab(tab_name)
        await backup_tab("Decisions")
    else:
        logger.info("\n=== Step 1: Backup skipped ===")

    # Step 2: Backfill decision labels (optional)
    if args.backfill_labels:
        logger.info("\n=== Step 2: Backfilling decision labels ===")
        from scripts.backfill_decision_labels import (
            get_unlabeled_decisions, get_canonical_names,
            label_decisions_with_haiku, apply_labels,
        )
        unlabeled = get_unlabeled_decisions()
        if unlabeled:
            canonical = get_canonical_names()
            labels = label_decisions_with_haiku(unlabeled, canonical)
            count = apply_labels(labels)
            logger.info(f"Backfilled {count} decision labels")
        else:
            logger.info("No unlabeled decisions found")
    else:
        logger.info("\n=== Step 2: Label backfill skipped (use --backfill-labels) ===")

    # Step 3: Rebuild Tasks sheet
    # NOTE: limit=10000 to match approval_flow.py and cleanup_rejected_meetings.py.
    # The default limit=100 silently truncates once total approved tasks crosses 100.
    logger.info("\n=== Step 3: Rebuilding Tasks sheet ===")
    try:
        tasks = supabase_client.get_tasks(limit=10000)
        logger.info(f"Fetched {len(tasks)} tasks from Supabase")
        ok = await sheets_service.rebuild_tasks_sheet(tasks)
        if ok:
            logger.info("Tasks sheet rebuilt successfully")
        else:
            logger.error("Tasks sheet rebuild failed")
    except Exception as e:
        logger.error(f"Tasks rebuild error: {e}")

    # Step 4: Rebuild Decisions sheet
    logger.info("\n=== Step 4: Rebuilding Decisions sheet ===")
    try:
        decisions = supabase_client.list_decisions(limit=10000)
        logger.info(f"Fetched {len(decisions)} decisions from Supabase")
        ok = await sheets_service.rebuild_decisions_sheet(decisions)
        if ok:
            logger.info("Decisions sheet rebuilt successfully")
        else:
            logger.error("Decisions sheet rebuild failed")
    except Exception as e:
        logger.error(f"Decisions rebuild error: {e}")

    # Step 5: Remove Commitments tab
    logger.info("\n=== Step 5: Removing Commitments tab ===")
    await remove_commitments_tab()

    logger.info("\n=== Rebuild complete ===")


if __name__ == "__main__":
    asyncio.run(main())
