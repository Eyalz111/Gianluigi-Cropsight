#!/usr/bin/env python3
"""
Cleanup script: find rejected meetings and cascade-delete their extracted data.

The approval flow historically only flipped meetings.approval_status to
'rejected' without deleting the extracted child data (tasks, decisions,
embeddings, open_questions, follow_up_meetings, entity_mentions,
task_mentions, topic_thread_mentions). This script finds those orphans and
removes them via delete_meeting_cascade(), then rebuilds the Tasks and
Decisions Sheets from fresh DB state.

Also performs an orphan sweep: extracted rows referencing non-existent
meeting IDs (defense in depth against partial cascades).

Idempotent — running it twice is safe. Second run finds zero rejected
meetings and zero orphans.

Usage:
    python scripts/cleanup_rejected_meetings.py                  # dry-run (default)
    python scripts/cleanup_rejected_meetings.py --apply          # execute
    python scripts/cleanup_rejected_meetings.py --apply --skip-rebuild
"""

import argparse
import asyncio
import logging
import os
import sys

# Add project root to path so we can import services/, etc.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: F401  (ensures env loads)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _count_children(sb_client, meeting_id: str) -> dict:
    """Count child rows for a given meeting_id across all extracted tables."""
    counts = {}
    for table, fk_col in [
        ("tasks", "meeting_id"),
        ("decisions", "meeting_id"),
        ("open_questions", "meeting_id"),
        ("follow_up_meetings", "source_meeting_id"),
        ("task_mentions", "meeting_id"),
        ("entity_mentions", "meeting_id"),
        ("topic_thread_mentions", "meeting_id"),
        ("commitments", "meeting_id"),
    ]:
        try:
            r = (
                sb_client.client.table(table)
                .select("id", count="exact")
                .eq(fk_col, meeting_id)
                .execute()
            )
            counts[table] = r.count or 0
        except Exception:
            counts[table] = 0
    try:
        r = (
            sb_client.client.table("embeddings")
            .select("id", count="exact")
            .eq("source_id", meeting_id)
            .execute()
        )
        counts["embeddings"] = r.count or 0
    except Exception:
        counts["embeddings"] = 0
    return counts


def _find_orphan_rows(sb_client) -> dict:
    """
    Find extracted rows referencing meeting_ids that don't exist in meetings table.
    Returns {table: [row_ids...]}.
    """
    # Get all current meeting IDs as a set
    try:
        meetings = sb_client.client.table("meetings").select("id").execute()
        valid_ids = {m["id"] for m in (meetings.data or [])}
    except Exception as e:
        logger.error(f"Could not fetch meetings for orphan check: {e}")
        return {}

    orphans: dict = {}
    for table, fk_col in [
        ("tasks", "meeting_id"),
        ("decisions", "meeting_id"),
        ("open_questions", "meeting_id"),
        ("follow_up_meetings", "source_meeting_id"),
    ]:
        try:
            rows = (
                sb_client.client.table(table)
                .select(f"id, {fk_col}")
                .not_.is_(fk_col, "null")
                .execute()
            )
            orphan_ids = [
                r["id"] for r in (rows.data or [])
                if r.get(fk_col) and r[fk_col] not in valid_ids
            ]
            if orphan_ids:
                orphans[table] = orphan_ids
        except Exception as e:
            logger.debug(f"Orphan scan for {table} skipped: {e}")
    return orphans


def _delete_orphans(sb_client, orphans: dict) -> dict:
    """Delete the orphan rows and return counts."""
    deleted: dict = {}
    for table, ids in orphans.items():
        count = 0
        for orphan_id in ids:
            try:
                sb_client.client.table(table).delete().eq("id", orphan_id).execute()
                count += 1
            except Exception as e:
                logger.warning(f"Failed to delete orphan {table}:{orphan_id}: {e}")
        deleted[table] = count
    return deleted


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cleanup rejected meetings and orphan extracted data."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete (default is dry-run)",
    )
    parser.add_argument(
        "--skip-rebuild",
        action="store_true",
        help="Skip Sheets rebuild step (only relevant with --apply)",
    )
    args = parser.parse_args()

    from services.supabase_client import supabase_client

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info(f"=== cleanup_rejected_meetings.py ({mode}) ===")

    # ---- 1. Rejected meetings ----
    rejected = supabase_client.list_meetings(approval_status="rejected", limit=1000)
    logger.info(f"Found {len(rejected)} rejected meetings")

    total_counts: dict = {
        "tasks": 0,
        "decisions": 0,
        "open_questions": 0,
        "follow_up_meetings": 0,
        "task_mentions": 0,
        "entity_mentions": 0,
        "topic_thread_mentions": 0,
        "commitments": 0,
        "embeddings": 0,
        "meetings": 0,
    }

    for m in rejected:
        mid = m.get("id")
        title = (m.get("title") or "")[:60]
        counts = _count_children(supabase_client, mid)
        total = sum(counts.values())
        logger.info(
            f"  {mid[:8]} | {title:60s} | "
            f"tasks={counts.get('tasks',0)} "
            f"decisions={counts.get('decisions',0)} "
            f"embed={counts.get('embeddings',0)} "
            f"other={total - counts.get('tasks',0) - counts.get('decisions',0) - counts.get('embeddings',0)}"
        )

        if args.apply:
            try:
                del_counts = supabase_client.delete_meeting_cascade(mid)
                for k, v in del_counts.items():
                    total_counts[k] = total_counts.get(k, 0) + v
            except Exception as e:
                logger.error(f"Cascade delete failed for {mid}: {e}")

    if args.apply:
        logger.info(f"Rejected-meeting cleanup totals: {total_counts}")

    # ---- 2. Orphan sweep ----
    logger.info("Scanning for orphan extracted rows (referencing missing meetings)...")
    orphans = _find_orphan_rows(supabase_client)
    if orphans:
        for table, ids in orphans.items():
            logger.info(f"  {table}: {len(ids)} orphan rows")
        if args.apply:
            deleted = _delete_orphans(supabase_client, orphans)
            logger.info(f"Deleted orphans: {deleted}")
    else:
        logger.info("  no orphan rows found")

    # ---- 3. Sheets rebuild (async) ----
    if args.apply and not args.skip_rebuild:
        logger.info("Rebuilding Tasks and Decisions sheets from fresh DB state...")
        try:
            from services.google_sheets import sheets_service

            fresh_tasks = supabase_client.get_tasks(status=None, limit=1000)
            fresh_decisions = supabase_client.list_decisions(limit=1000)
            logger.info(
                f"  Pulled {len(fresh_tasks)} tasks and "
                f"{len(fresh_decisions)} decisions from DB"
            )
            await sheets_service.rebuild_tasks_sheet(fresh_tasks)
            await sheets_service.rebuild_decisions_sheet(fresh_decisions)
            logger.info("  Sheets rebuild complete")
        except Exception as e:
            logger.error(f"Sheets rebuild failed: {e}")
            return 2

    if not args.apply:
        logger.info("Dry run complete. Re-run with --apply to execute.")
    else:
        logger.info("Cleanup complete.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
