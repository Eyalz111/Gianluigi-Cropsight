"""Seed a sheet_snapshots row per approved decision (Phase 2, editable Decisions).

Run ONCE after applying scripts/migrate_decision_reconcile_editable.sql and as
part of the cutover (before the first live decision reconcile). Mirrors the task
backfill_snapshot_content.py, but decisions have NO pre-existing snapshot rows —
so this CREATES one per decision from the current DB values, so the first
reconcile sees snapshot == DB and does NOT mis-read an untouched Sheet cell as an
Eyal edit (phantom-pull).

Writes ONLY the DB `sheet_snapshots` table (entity_type='decision'). Does NOT
touch the Google Sheet — populating col-H ids on the Sheet happens in PROD via a
rebuild once DECISION_RECONCILE_ENABLED is on (never write the live Sheet from a
local script — the "tasks vanished" incident). Idempotent: skips a decision that
already has a snapshot, so a re-run won't stomp values a live reconcile recorded.

    python scripts/backfill_decision_snapshots.py            # dry-run (counts only)
    python scripts/backfill_decision_snapshots.py --apply    # write
"""

import sys

from services.supabase_client import supabase_client


def main(apply: bool) -> None:
    # Approved decisions, INCLUDING superseded ones (they still appear on the
    # sheet with their status) — the full set the reconcile will key on.
    decisions = supabase_client.list_decisions(
        limit=2000, include_pending=False, include_superseded=True
    )
    print(f"Found {len(decisions)} approved decisions.")

    existing = supabase_client.get_decision_snapshots()  # {decision_id: row}
    print(f"{len(existing)} already have a snapshot (will be skipped).")

    to_write = 0
    for d in decisions:
        did = d.get("id")
        if not did or did in existing:
            continue  # no id, or already snapshotted — don't stomp
        to_write += 1
        if apply:
            supabase_client.upsert_decision_snapshot(
                decision_id=did,
                sheet_row=None,
                description=d.get("description"),
                label=d.get("label"),
                rationale=d.get("rationale"),
                confidence=d.get("confidence"),
                # store the DB value (lowercase) — the reconcile normalizes case
                # when comparing the Sheet's capitalized "Active" to the snapshot.
                decision_status=d.get("decision_status"),
            )

    print(f"{'Wrote' if apply else 'Would write'} {to_write} new decision snapshots.")
    if not apply:
        print("Dry-run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
