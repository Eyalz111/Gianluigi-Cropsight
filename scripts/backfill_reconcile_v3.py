"""
v3 Phase 3 one-time backfill — give existing Tasks-sheet rows a UUID (col J) and
seed the initial reconcile snapshot, so the reconcile engine has stable identity
and an empty first diff.

Matches each id-less Sheet row to a DB task by (normalized title, assignee) ONCE.
Idempotent: rows that already have a UUID are kept. ABORTS (no writes) if any
(title, assignee) is ambiguous — maps to >1 DB task OR >1 Sheet row — reporting
the pairs for manual resolution rather than guessing (#9).

Prereqs: apply scripts/migrate_phase_v3_reconcile.sql first.

Usage:
    python scripts/backfill_reconcile_v3.py            # dry-run report (default)
    python scripts/backfill_reconcile_v3.py --apply    # write UUIDs + seed snapshots
"""

import argparse
import asyncio
import logging
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


def _norm(v) -> str:
    return str(v or "").strip().lower()


def _key(title, assignee) -> str:
    return f"{_norm(title)}|{_norm(assignee)}"


async def run_backfill(apply: bool = False) -> dict:
    from services.google_sheets import sheets_service, TASK_COLUMNS

    sheet_tasks = await sheets_service.get_all_tasks()
    db_tasks = supabase_client.get_tasks(status=None, limit=2000, include_pending=True)

    db_by_key: dict[str, list] = {}
    for t in db_tasks:
        if t.get("id"):
            db_by_key.setdefault(_key(t.get("title"), t.get("assignee")), []).append(t)

    idless = [
        st for st in sheet_tasks
        if not str(st.get("id") or "").strip() and str(st.get("task") or "").strip()
    ]
    sheet_key_counts: dict[str, int] = {}
    for st in idless:
        k = _key(st.get("task"), st.get("assignee"))
        sheet_key_counts[k] = sheet_key_counts.get(k, 0) + 1

    ambiguous, matches, unmatched = [], [], []
    for st in idless:
        k = _key(st.get("task"), st.get("assignee"))
        db_matches = db_by_key.get(k, [])
        if len(db_matches) > 1:
            ambiguous.append({"row": st.get("row_number"), "key": k, "reason": ">1 DB task"})
        elif sheet_key_counts.get(k, 0) > 1 and len(db_matches) == 1:
            ambiguous.append({"row": st.get("row_number"), "key": k, "reason": ">1 Sheet row shares key"})
        elif len(db_matches) == 1:
            matches.append((st, db_matches[0]))
        else:
            unmatched.append(st)  # Sheet-only task; reconcile will create it later

    if ambiguous:
        logger.error(f"[backfill] ABORT — {len(ambiguous)} ambiguous pair(s); resolve manually:")
        for a in ambiguous:
            logger.error(f"   row {a['row']}: '{a['key']}' ({a['reason']})")
        return {"status": "aborted_ambiguous", "ambiguous": ambiguous,
                "matched": len(matches), "unmatched": len(unmatched)}

    result = {
        "status": "ok",
        "to_assign": len(matches),
        "already_have_id": len(sheet_tasks) - len(idless),
        "unmatched_sheet_only": len(unmatched),
        "applied": apply,
    }
    if not apply:
        logger.info(f"[backfill][dry-run] {result}")
        return result

    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"
    cell_writes = []
    assigned_by_row = {}
    for st, dt in matches:
        row = st.get("row_number")
        if row:
            cell_writes.append({"range": f"'{tab}'!{TASK_COLUMNS['id']}{row}", "values": [[dt["id"]]]})
            assigned_by_row[row] = dt["id"]
    if cell_writes:
        sheets_service.service.spreadsheets().values().batchUpdate(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            body={"valueInputOption": "RAW", "data": cell_writes},
        ).execute()

    # Seed snapshot for every Sheet row that now has an id (existing + just-assigned)
    # from the CURRENT Sheet action values, so the first reconcile diff is empty.
    seeded = 0
    for st in sheet_tasks:
        row = st.get("row_number")
        sid = str(st.get("id") or "").strip() or assigned_by_row.get(row)
        if not sid:
            continue
        supabase_client.upsert_sheet_snapshot(
            sid, row, st.get("status"), st.get("deadline"),
            st.get("priority"), st.get("assignee"),
        )
        seeded += 1
    result["seeded_snapshots"] = seeded

    try:
        supabase_client.log_action("reconcile_backfill", details=result, triggered_by="system")
    except Exception:
        pass
    logger.info(f"[backfill] applied: {result}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="v3 reconcile one-time backfill")
    parser.add_argument("--apply", action="store_true", help="Write UUIDs + seed snapshots (default: dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(f"v3 reconcile backfill [{'APPLY' if args.apply else 'DRY-RUN'}]...")
    res = asyncio.run(run_backfill(apply=args.apply))
    print(f"Done: {res}")
