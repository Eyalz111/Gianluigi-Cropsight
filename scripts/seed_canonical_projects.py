"""
One-time seed: ensure every canonical_projects entry has a matching
topic_threads row.

Canonical projects are the stable anchors for CropSight's strategic topics
(Moldova Pilot, Legal, WEU Marketing, etc.). The topic threading pipeline
creates a thread the first time a meeting mentions any of them, but a
canonical project that hasn't come up in any meeting yet would have no
thread — and therefore no state_json to surface in the morning brief or
MCP get_topic_thread.

This script walks the canonical_projects table and creates an empty active
topic_threads row for any canonical project that doesn't have one. The
state_json stays NULL — it will populate on the first mention (incremental
update) or via the backfill script (one-time Sonnet pass).

Idempotent. Run once after migration.

Usage:
    python scripts/seed_canonical_projects.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.supabase_client import supabase_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def seed() -> dict:
    """Ensure every canonical project has a topic_threads row."""
    # Load all canonical projects
    projects = supabase_client.get_canonical_projects(status="active")
    if not projects:
        logger.info("No canonical projects found — nothing to seed.")
        return {"projects_seen": 0, "threads_created": 0, "threads_already_exist": 0}

    # Load existing threads (by lowercase name for matching)
    thread_rows = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name_lower")
        .limit(1000)
        .execute()
    )
    existing_lower = {r.get("topic_name_lower") for r in (thread_rows.data or [])}

    created = 0
    skipped = 0
    for p in projects:
        name = p.get("name", "").strip()
        if not name:
            continue
        if name.lower() in existing_lower:
            skipped += 1
            continue
        # Create an anchor thread. No first_meeting_id — this is a seed, not
        # an actual mention. meeting_count=0 so downstream consumers can
        # distinguish seed rows from real mentions.
        try:
            supabase_client.client.table("topic_threads").insert({
                "workspace_id": "cropsight",
                "topic_name": name,
                "status": "active",
                "meeting_count": 0,
            }).execute()
            created += 1
            logger.info(f"Seeded canonical project as topic thread: '{name}'")
        except Exception as e:
            logger.warning(f"Failed to seed '{name}': {e}")

    summary = {
        "projects_seen": len(projects),
        "threads_created": created,
        "threads_already_exist": skipped,
    }
    logger.info(f"Seed complete: {summary}")

    try:
        supabase_client.log_action(
            action="seed_canonical_project_threads",
            details=summary,
            triggered_by="auto",
        )
    except Exception as e:
        logger.warning(f"Audit log entry failed (non-fatal): {e}")

    return summary


if __name__ == "__main__":
    result = seed()
    print(f"\nSummary: {result}")
