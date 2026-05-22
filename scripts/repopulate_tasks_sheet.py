"""
One-off recovery: repopulate the Tasks sheet from the DB (source of truth) and
seed reconcile snapshots.

Use when the Sheet⇄DB has diverged so the Tasks sheet is empty (header only) but
the DB retained tasks (the "tasks vanished" class). Rebuilds the sheet from the
approved DB tasks (with col-J UUIDs), normalizes status casing, then seeds one
`sheet_snapshots` row per task so the first reconcile diff is empty.

Idempotent: rebuild overwrites the sheet from DB; snapshots are upserted.

Usage:
    python scripts/repopulate_tasks_sheet.py            # dry-run report (default)
    python scripts/repopulate_tasks_sheet.py --apply    # write sheet + seed snapshots
"""

import argparse
import asyncio
import logging
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_VALID_STATUSES = ("pending", "in_progress", "done", "overdue")


async def run(apply: bool = False) -> dict:
    from services.google_sheets import sheets_service

    tasks = supabase_client.get_tasks(status=None, limit=2000)  # approved only
    # Normalize status casing (e.g. "Done" -> "done")
    casing_fixed = 0
    for t in tasks:
        s = t.get("status")
        if s and s != s.lower() and s.lower() in _VALID_STATUSES:
            if apply:
                supabase_client.update_task(t["id"], status=s.lower())
            t["status"] = s.lower()
            casing_fixed += 1

    result = {"approved_tasks": len(tasks), "casing_fixed": casing_fixed, "applied": apply}

    if not apply:
        result["note"] = "dry-run: would rebuild the sheet + seed snapshots"
        logger.warning(f"[repopulate][dry-run] {result}")
        return result

    ok = await sheets_service.rebuild_tasks_sheet(tasks)
    result["rebuild_ok"] = bool(ok)

    # Seed snapshots from the freshly written sheet (so the first reconcile diff is empty)
    rows = await sheets_service.get_all_tasks()
    seeded = 0
    for r in rows:
        rid = str(r.get("id") or "").strip()
        if rid and supabase_client.upsert_sheet_snapshot(
            rid, r.get("row_number"), r.get("status"),
            r.get("deadline"), r.get("priority"), r.get("assignee"),
        ):
            seeded += 1
    result["sheet_rows_after"] = len(rows)
    result["snapshots_seeded"] = seeded

    try:
        supabase_client.log_action("tasks_sheet_repopulated", details=result, triggered_by="eyal")
    except Exception:
        pass
    logger.warning(f"[repopulate] {result}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repopulate Tasks sheet from DB")
    parser.add_argument("--apply", action="store_true", help="Write the sheet + seed snapshots (default: dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print(f"Repopulate Tasks sheet [{'APPLY' if args.apply else 'DRY-RUN'}]...")
    res = asyncio.run(run(apply=args.apply))
    print(f"Done: {res}")
