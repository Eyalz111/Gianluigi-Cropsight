#!/usr/bin/env python3
"""
One-off (2026-07-05): hard-delete the 4 "outage" rejected tombstones so the
transcript watcher will REPROCESS the still-present Drive files (and any
re-drops) on its next poll, producing real summaries.

These 4 came in EMPTY while Claude credits were down (2026-06/07) and were
tombstoned (approval_status='rejected') on 2026-07-04. A rejected tombstone
blocks a same-name re-drop, which is why Eyal's re-drop today produced no
summary. Clearing the row unblocks reprocessing.

SAFETY:
- Targets ONLY the 4 explicit id-prefixes below. It will NOT touch the two
  GENUINE rejections (f2d75c33 Nibbana 06-25, d117e30c Monthly 06-13).
- Verifies each row is approval_status='rejected' and its title matches the
  expected outage meeting before deleting.
- Supabase-only (delete_meeting_cascade, keep_tombstone=False). Does NOT write
  the Google Sheet (no rebuild step).

Usage:
    python scripts/clear_outage_tombstones_2026_07_05.py            # dry-run
    python scripts/clear_outage_tombstones_2026_07_05.py --apply    # execute
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: F401  (ensures .env loads)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# (id-prefix, expected-title-substring) for the 4 outage tombstones.
TARGETS = [
    ("d2cd6cfe", "2026-06-20"),   # 2026-06-20 CropSight weekly
    ("5ae7adf7", "2026-06-28"),   # 2026-06-28 מתקינים MVP
    ("e5dc467b", "2026-06-28"),   # 2026-06-28 CropSight weekly
    ("5c49b9a2", "2026-07-02"),   # 2026-07-02 CropSight <> Ido Biton
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    args = parser.parse_args()

    from services.supabase_client import supabase_client

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info(f"=== clear_outage_tombstones_2026_07_05.py ({mode}) ===")

    # Pull all rejected meetings once; match by id-prefix so we never delete
    # anything outside the allow-list.
    rejected = supabase_client.list_meetings(approval_status="rejected", limit=1000)
    by_prefix = {}
    for m in rejected:
        mid = m.get("id") or ""
        by_prefix[mid[:8]] = m

    to_delete = []
    for prefix, expected in TARGETS:
        m = by_prefix.get(prefix)
        if not m:
            logger.warning(f"  {prefix}: NOT FOUND among rejected meetings (already cleared?) — skipping")
            continue
        title = m.get("title") or ""
        status = m.get("approval_status")
        if status != "rejected":
            logger.error(f"  {prefix}: status is '{status}', not 'rejected' — SKIPPING for safety")
            continue
        if expected not in title:
            logger.error(f"  {prefix}: title '{title[:50]}' lacks expected '{expected}' — SKIPPING for safety")
            continue
        logger.info(f"  MATCH {m['id']} | {status} | {title[:55]}")
        to_delete.append(m)

    logger.info(f"{len(to_delete)}/{len(TARGETS)} targets matched and eligible.")

    if not args.apply:
        logger.info("Dry run complete. Re-run with --apply to hard-delete these tombstones.")
        return 0

    for m in to_delete:
        mid = m["id"]
        try:
            counts = supabase_client.delete_meeting_cascade(mid, keep_tombstone=False)
            logger.info(f"  DELETED {mid} ({m.get('title','')[:45]}) -> {counts}")
        except Exception as e:
            logger.error(f"  FAILED {mid}: {e}")
            return 2

    logger.info("Done. The transcript watcher will reprocess the Drive files on its next poll (~15 min).")
    logger.info("NOTE: reprocessed meetings may take TODAY's date (Tactiq filename date-parse bug) — fix meetings.date by hand after.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
