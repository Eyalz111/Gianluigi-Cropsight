"""
One-time backfill: populate tasks.deadline_confidence for legacy rows.

Rule:
- Tasks with a non-null deadline → 'INFERRED'
- Tasks with a null deadline → 'NONE' (matches default, but we write it
  explicitly so the audit log shows when the backfill ran)

Rationale: we can't retroactively know which legacy deadlines were stated
verbatim by a participant vs. guessed by the LLM. Better to mark them all
INFERRED so the v2.3 reminder/alert filters suppress them — silent is safer
than false-alarming. Users can re-mark any critical legacy task as EXPLICIT
via the PR 5 inline buttons or a direct DB update.

Run once, after scripts/migrate_v2_3.sql has been applied. Idempotent —
running twice is safe (the second run finds all rows already set and
updates 0 rows).

Usage:
    python scripts/backfill_deadline_confidence.py
"""

import logging
import sys
from pathlib import Path

# Make repo root importable when running as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.supabase_client import supabase_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def backfill() -> dict:
    """
    Backfill deadline_confidence on existing tasks.

    Returns summary dict with counts of rows updated per bucket.
    """
    # Tasks with deadline set AND deadline_confidence still default/null →
    # INFERRED. Bound the update narrowly so a re-run doesn't flip rows that
    # have already been set to a different value (e.g. EXPLICIT from a PR 5
    # inline-button edit).
    inferred_result = (
        supabase_client.client.table("tasks")
        .update({"deadline_confidence": "INFERRED"})
        .not_.is_("deadline", "null")
        .eq("deadline_confidence", "NONE")
        .execute()
    )
    inferred_count = len(inferred_result.data or [])

    # Tasks without a deadline are already 'NONE' by column default. No write
    # needed — the migration's DEFAULT 'NONE' handled them. We log the count
    # so the backfill output shows the full picture.
    no_deadline_result = (
        supabase_client.client.table("tasks")
        .select("id", count="exact")
        .is_("deadline", "null")
        .execute()
    )
    no_deadline_count = no_deadline_result.count or 0

    # Sanity check: tasks already at EXPLICIT (shouldn't exist pre-PR 5, but
    # surface the count in case someone ran a partial deploy).
    explicit_result = (
        supabase_client.client.table("tasks")
        .select("id", count="exact")
        .eq("deadline_confidence", "EXPLICIT")
        .execute()
    )
    explicit_count = explicit_result.count or 0

    summary = {
        "inferred_updated": inferred_count,
        "no_deadline_already_none": no_deadline_count,
        "already_explicit": explicit_count,
    }

    logger.info(
        f"Backfill complete: {inferred_count} deadlined tasks → INFERRED, "
        f"{no_deadline_count} no-deadline tasks remain NONE, "
        f"{explicit_count} pre-existing EXPLICIT rows untouched."
    )

    # Audit log entry for the run
    try:
        supabase_client.log_action(
            action="backfill_deadline_confidence",
            details=summary,
            triggered_by="auto",
        )
    except Exception as e:
        logger.warning(f"Failed to write audit log entry (non-fatal): {e}")

    return summary


if __name__ == "__main__":
    result = backfill()
    print(f"\nSummary: {result}")
