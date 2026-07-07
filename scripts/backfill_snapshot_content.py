"""Backfill title/label onto existing sheet_snapshots rows (Phase 1, 2026-07).

Run ONCE after applying scripts/migrate_task_reconcile_editable_content.sql and
BEFORE flipping RECONCILE_SHADOW_MODE=false.

Why: the content reconcile attributes a Task-text/Label edit to Eyal when the
Sheet cell differs from the snapshot. Existing snapshots predate the title/label
columns (they're NULL), so without this backfill the first reconcile would see
snapshot.title == NULL != sheet.title and could mis-read an untouched cell as an
edit. (The reconcile also guards with a "!= DB" check, so this is belt-and-
suspenders — but it keeps the snapshots honest.)

Writes ONLY the DB `sheet_snapshots` table (additive — sets title/label from the
current DB task). Does NOT touch the Google Sheet. Idempotent: re-running just
re-seeds the same values.

    python scripts/backfill_snapshot_content.py            # dry-run (counts only)
    python scripts/backfill_snapshot_content.py --apply    # write
"""

import sys

from services.supabase_client import supabase_client


def main(apply: bool) -> None:
    snaps = (
        supabase_client.client.table("sheet_snapshots")
        .select("id,task_id,title,label")
        .eq("entity_type", "task")
        .execute()
        .data
        or []
    )
    print(f"Found {len(snaps)} task snapshot rows.")

    # Map task_id -> current DB title/label.
    tasks = supabase_client.get_tasks(
        status=None, limit=2000, include_pending=True, include_archived=True
    )
    by_id = {t["id"]: t for t in tasks if t.get("id")}

    to_write = 0
    for s in snaps:
        tid = s.get("task_id")
        t = by_id.get(tid)
        if not t:
            continue  # snapshot for a task the DB no longer has — leave it
        title, label = t.get("title") or None, t.get("label") or None
        # Only write when the snapshot is missing content (don't stomp a value a
        # post-deploy reconcile may already have recorded).
        if s.get("title") is None and s.get("label") is None and (title or label):
            to_write += 1
            if apply:
                supabase_client.client.table("sheet_snapshots").update(
                    {"title": title, "label": label}
                ).eq("id", s["id"]).execute()

    print(f"{'Wrote' if apply else 'Would write'} content to {to_write} snapshot rows.")
    if not apply:
        print("Dry-run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
