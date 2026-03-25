#!/usr/bin/env python3
"""
Backfill empty decision labels using Haiku.

Old decisions (pre-Phase 9A) have empty label fields. This script
uses Haiku to retroactively label them based on decision text, meeting
title, and canonical project names from the DB.

Usage:
    python scripts/backfill_decision_labels.py --dry-run   # preview labels
    python scripts/backfill_decision_labels.py              # apply labels
"""

import argparse
import json
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def get_unlabeled_decisions() -> list[dict]:
    """Fetch decisions that have empty or null labels."""
    from services.supabase_client import supabase_client

    result = (
        supabase_client.client.table("decisions")
        .select("id, description, meeting_id, created_at, label")
        .execute()
    )

    unlabeled = []
    for d in (result.data or []):
        if not d.get("label") or d["label"].strip() == "":
            unlabeled.append(d)

    return unlabeled


def get_canonical_names() -> list[str]:
    """Get canonical project names from DB."""
    from services.supabase_client import supabase_client

    projects = supabase_client.get_canonical_projects(status="active")
    return [p["name"] for p in projects]


def get_meeting_info(meeting_id: str) -> dict:
    """Get meeting title and participants for context."""
    if not meeting_id:
        return {}
    try:
        from services.supabase_client import supabase_client

        result = (
            supabase_client.client.table("meetings")
            .select("title, participants")
            .eq("id", meeting_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else {}
    except Exception:
        return {}


def label_decisions_with_haiku(
    decisions: list[dict],
    canonical_names: list[str],
) -> list[dict]:
    """
    Use Haiku to generate labels for a batch of decisions.

    Returns list of {id, label} dicts.
    """
    from core.llm import call_llm

    results = []
    batch_size = 10

    for i in range(0, len(decisions), batch_size):
        batch = decisions[i:i + batch_size]

        # Build context for each decision
        items = []
        for d in batch:
            meeting = get_meeting_info(d.get("meeting_id", ""))
            items.append({
                "id": d["id"],
                "decision": d.get("description", ""),
                "meeting_title": meeting.get("title", d.get("source_meeting", "")),
                "participants": meeting.get("participants", []),
            })

        prompt = f"""Label each decision with a 2-3 word topic tag.

CANONICAL PROJECT NAMES (use these when possible):
{json.dumps(canonical_names)}

DECISIONS TO LABEL:
{json.dumps(items, indent=2)}

Return ONLY a JSON array of objects with "id" and "label" fields.
Example: [{{"id": "abc-123", "label": "Moldova Pilot"}}]

Rules:
- Use canonical names when the decision relates to a known project
- If no canonical name fits, create a short descriptive label (2-4 words)
- Every decision must get a label"""

        try:
            response, _ = call_llm(
                prompt=prompt,
                model=settings.model_simple,
                max_tokens=2000,
                call_site="backfill_decision_labels",
            )

            # Parse JSON from response
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            parsed = json.loads(text)
            results.extend(parsed)
            logger.info(f"  Labeled batch {i // batch_size + 1}: {len(parsed)} decisions")

        except Exception as e:
            logger.error(f"  Error labeling batch {i // batch_size + 1}: {e}")
            # Skip batch, don't crash

    return results


def apply_labels(labels: list[dict]) -> int:
    """Write labels to Supabase. Returns count of updated rows."""
    from services.supabase_client import supabase_client

    updated = 0
    for item in labels:
        try:
            supabase_client.client.table("decisions").update(
                {"label": item["label"]}
            ).eq("id", item["id"]).execute()
            updated += 1
        except Exception as e:
            logger.error(f"  Failed to update decision {item['id']}: {e}")

    return updated


def main():
    parser = argparse.ArgumentParser(description="Backfill decision labels using Haiku")
    parser.add_argument("--dry-run", action="store_true", help="Preview labels without writing")
    args = parser.parse_args()

    logger.info("Fetching unlabeled decisions...")
    unlabeled = get_unlabeled_decisions()
    logger.info(f"Found {len(unlabeled)} decisions without labels")

    if not unlabeled:
        logger.info("Nothing to do.")
        return

    logger.info("Fetching canonical project names...")
    canonical_names = get_canonical_names()
    logger.info(f"Canonical names: {canonical_names}")

    logger.info("Labeling decisions with Haiku...")
    labels = label_decisions_with_haiku(unlabeled, canonical_names)
    logger.info(f"Generated {len(labels)} labels")

    if args.dry_run:
        logger.info("\n=== DRY RUN — proposed labels ===")
        for item in labels:
            # Find original decision text
            original = next((d for d in unlabeled if d["id"] == item["id"]), {})
            desc = original.get("description", "???")[:60]
            logger.info(f"  [{item['label']}] {desc}")
        logger.info(f"\nTotal: {len(labels)} labels. Run without --dry-run to apply.")
    else:
        logger.info("Applying labels to Supabase...")
        count = apply_labels(labels)
        logger.info(f"Updated {count} decisions with labels")


if __name__ == "__main__":
    main()
