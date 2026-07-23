#!/usr/bin/env python3
"""
Backfill task assignees to canonical FIRST + LAST names. [2026-07-22]

Why: live data carried the SAME person under two spellings — "Eyal Zror" x31 and
"Eyal" x9, with Paolo/Roye/Yoram split identically — plus multi-owner cells
("Paolo, Eyal") and a truncated surname ("Debra N"). get_tasks filters with
`ilike` and NO wildcards, so "what does Paolo owe?" returned 4 of 15 rows. That
makes a shared weekly review impossible, which is the whole point of the
office-manager workspace.

resolve_assignee() now canonicalizes on WRITE; this normalizes the history.

DB SAFETY PROTOCOL (Eyal's constraint: do not damage the DB)
  - NO deletes. Field normalization only.
  - Snapshots every affected row to JSON BEFORE any write.
  - Dry run is the DEFAULT; --apply is required to write.
  - Every write is audit-logged (`assignee_backfill`) with before/after.
  - Writes a rollback file that --rollback replays exactly.
  - Multi-owner cells keep the PRIMARY owner and append the others to the
    title, per Eyal's call — never split into two tasks (that would change
    task counts and history).

Usage:
    python scripts/backfill_assignees_2026_07.py                  # dry run (default)
    python scripts/backfill_assignees_2026_07.py --apply          # write
    python scripts/backfill_assignees_2026_07.py --rollback FILE  # undo
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

# Explicit spelling repairs that resolve_assignee cannot infer, because the
# stored value is not a prefix of the canonical name. Eyal supplied these.
EXPLICIT_MAP = {
    "debra n": "Debra Nachlis",
    "debra": "Debra Nachlis",
    "marco": "Marco Sutter",
    "matti": "Matti Sevitt",
    "shemer": "Shemer Topper",
}

_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backfill_snapshots")


def _plan_row(sc, task: dict, roster: list[dict]) -> dict | None:
    """Return {'id','before','after','title_before','title_after'} or None if unchanged."""
    raw = (task.get("assignee") or "").strip()
    if not raw:
        return None

    title = task.get("title") or ""
    new_title = title

    explicit = EXPLICIT_MAP.get(raw.lower())
    resolved = explicit or sc.resolve_assignee(raw, roster=roster)

    # Multi-owner: keep the primary, name the rest in the title.
    if ("," in raw or " and " in raw.lower()) and resolved != raw:
        parts = [p.strip() for p in raw.replace(" and ", ",").split(",") if p.strip()]
        others = []
        for p in parts[1:]:
            canon = EXPLICIT_MAP.get(p.lower()) or sc.resolve_assignee(p, roster=roster)
            if canon and canon.lower() != resolved.lower():
                others.append(canon)
        if others and "with " not in title.lower():
            new_title = f"{title} (with {', '.join(others)})"

    if resolved == raw and new_title == title:
        return None
    return {
        "id": task["id"],
        "before": raw,
        "after": resolved,
        "title_before": title,
        "title_after": new_title,
    }


def build_plan() -> list[dict]:
    from services.supabase_client import supabase_client as sc

    roster = sc.list_team_members() or []
    if not roster:
        logger.error("Empty team roster — refusing to plan (would mangle every assignee).")
        return []
    logger.info(f"Roster: {[m.get('name') for m in roster]}")

    tasks = sc.get_tasks(status=None, limit=5000, include_pending=True, include_archived=True)
    logger.info(f"Scanned {len(tasks)} tasks")

    plan: list[dict] = []
    for t in tasks:
        if not t.get("id"):
            continue
        row = _plan_row(sc, t, roster)
        if row:
            plan.append(row)
    return plan


def _snapshot(plan: list[dict]) -> str:
    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SNAPSHOT_DIR, f"assignee_backfill_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    logger.info(f"Snapshot written: {path}")
    return path


def apply_plan(plan: list[dict]) -> int:
    """Apply the plan to the DB.

    NOTE on the Sheet: rows whose assignee is `manual_assignee=True` will NOT be
    refreshed by reconcile — Rule 4 deliberately holds a human-owned field
    against a DB-side change (2026-07-22 rail). That is correct in general, but
    WRONG for this backfill: the flag records "a human chose this spelling", and
    the backfill overrides that choice deliberately and with Eyal's approval. So
    those cells must be written directly, or the Sheet keeps the old spelling
    forever while the DB holds the corrected one. `sync_sheet_for_held_rows()`
    below does that; run it after --apply.
    """
    from services.supabase_client import supabase_client as sc

    applied = 0
    for row in plan:
        try:
            updates = {"assignee": row["after"]}
            if row["title_after"] != row["title_before"]:
                updates["title"] = row["title_after"]
            sc.update_task(row["id"], **updates)
            sc.log_action(
                action="assignee_backfill",
                details={"task_id": row["id"], "before": row["before"],
                         "after": row["after"],
                         "title_before": row["title_before"],
                         "title_after": row["title_after"]},
                triggered_by="eyal",
            )
            applied += 1
        except Exception as e:
            logger.error(f"FAILED {row['id']}: {e}")
    return applied


async def sync_sheet_for_held_rows() -> int:
    """Write the corrected assignee into cells reconcile will refuse to refresh.

    Only touches rows where the Sheet disagrees with the DB AND
    `manual_assignee` is set — i.e. exactly the rows the Rule 4 rail holds.
    Everything else is left for the normal reconcile push.
    """
    from config.settings import settings
    from services.google_sheets import sheets_service, TASK_COLUMNS
    from services.supabase_client import supabase_client as sc

    rows = await sheets_service.get_all_tasks()
    if not rows:
        logger.error("Sheet read returned 0 rows — refusing to write.")
        return 0
    db = {t["id"]: t for t in sc.get_tasks(
        status=None, limit=5000, include_pending=True, include_archived=True)
        if t.get("id")}
    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"

    writes = []
    for r in rows:
        rid = str(r.get("id") or "").strip()
        t = db.get(rid)
        if not t or not r.get("row_number"):
            continue
        sheet_v = (r.get("assignee") or "").strip()
        db_v = (t.get("assignee") or "").strip()
        if sheet_v != db_v and t.get("manual_assignee"):
            writes.append({
                "range": f"'{tab}'!{TASK_COLUMNS['owner']}{r['row_number']}",
                "values": [[db_v]],
            })
    if not writes:
        return 0
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().values().batchUpdate(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            body={"valueInputOption": "RAW", "data": writes},
        )
    )
    sc.log_action(
        action="assignee_backfill_sheet_sync",
        details={"rows": len(writes)}, triggered_by="eyal",
    )
    logger.info(f"Synced {len(writes)} manual-held assignee cell(s) to the Sheet")
    return len(writes)


def rollback(path: str) -> int:
    from services.supabase_client import supabase_client as sc

    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    restored = 0
    for row in plan:
        try:
            updates = {"assignee": row["before"]}
            if row["title_after"] != row["title_before"]:
                updates["title"] = row["title_before"]
            # Write DIRECTLY, bypassing update_task: it canonicalizes `assignee`
            # on every write, so replaying a pre-canonical value like 'eyal'
            # just resolves back to 'Eyal Zror' and the rollback is a silent
            # no-op — for the ROSTER short names, which are the majority of what
            # this backfill changed. The docstring promises "--rollback replays
            # exactly"; only a raw write actually does. [2026-07-23]
            from datetime import datetime as _dt, timezone as _tz
            updates["updated_at"] = _dt.now(_tz.utc).isoformat()
            sc.client.table("tasks").update(updates).eq("id", row["id"]).execute()
            sc.log_action(
                action="assignee_backfill_rollback",
                details={"task_id": row["id"], "restored_to": row["before"]},
                triggered_by="eyal",
            )
            restored += 1
        except Exception as e:
            logger.error(f"ROLLBACK FAILED {row['id']}: {e}")
    return restored


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    ap.add_argument("--rollback", metavar="FILE", help="restore from a snapshot file")
    args = ap.parse_args()

    if args.rollback:
        n = rollback(args.rollback)
        logger.info(f"Rolled back {n} task(s)")
        return 0

    plan = build_plan()
    if not plan:
        logger.info("Nothing to change.")
        return 0

    # Grouped preview — this is what Eyal reviews before approving.
    by_change: dict[tuple, int] = {}
    for r in plan:
        by_change[(r["before"], r["after"])] = by_change.get((r["before"], r["after"]), 0) + 1
    print("\n=== PLANNED ASSIGNEE CHANGES ===")
    for (before, after), n in sorted(by_change.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}x  {before!r:<32} -> {after!r}")
    retitled = [r for r in plan if r["title_after"] != r["title_before"]]
    if retitled:
        print(f"\n=== {len(retitled)} MULTI-OWNER TITLE EDIT(S) ===")
        for r in retitled:
            print(f"  {r['before']!r} -> {r['after']!r}")
            print(f"      title: {r['title_after'][:110]}")
    print(f"\nTotal rows affected: {len(plan)}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    path = _snapshot(plan)
    n = apply_plan(plan)
    print(f"\nApplied {n}/{len(plan)}.")
    synced = asyncio.run(sync_sheet_for_held_rows())
    if synced:
        print(f"Synced {synced} manual-held cell(s) directly to the Sheet "
              f"(reconcile's Rule 4 rail would have kept the old spelling there).")
    print(f"Rollback:  python scripts/backfill_assignees_2026_07.py --rollback {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
