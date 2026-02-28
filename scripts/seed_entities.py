"""
Seed the entity registry with known CropSight entities.

Run once after the entity tables migration to pre-populate the registry
with entities that appear frequently in meetings.

Usage:
    python scripts/seed_entities.py
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.supabase_client import db


# Known entities to seed
SEED_ENTITIES = [
    # People
    {
        "canonical_name": "Jason Adelman",
        "entity_type": "person",
        "aliases": ["Jason", "Adelman"],
        "metadata": {"role": "Advisor / Partner", "notes": "Key contact for BD"},
    },
    # Organizations
    {
        "canonical_name": "IIA",
        "entity_type": "organization",
        "aliases": ["Israel Innovation Authority", "Innovation Authority"],
        "metadata": {"type": "Government body", "notes": "Grant/funding source"},
    },
    {
        "canonical_name": "Lavazza",
        "entity_type": "organization",
        "aliases": ["Lavazza Group"],
        "metadata": {"type": "Corporate partner", "industry": "Coffee/Agriculture"},
    },
    {
        "canonical_name": "Ferrero",
        "entity_type": "organization",
        "aliases": ["Ferrero Group"],
        "metadata": {"type": "Corporate partner", "industry": "Food/Agriculture"},
    },
    # Locations / Projects
    {
        "canonical_name": "Gagauzia",
        "entity_type": "location",
        "aliases": ["Gagauzia region"],
        "metadata": {"country": "Moldova", "notes": "Pilot location"},
    },
    {
        "canonical_name": "Moldova",
        "entity_type": "location",
        "aliases": ["Republic of Moldova"],
        "metadata": {"notes": "Pilot country for CropSight deployment"},
    },
    {
        "canonical_name": "Moldova Pilot",
        "entity_type": "project",
        "aliases": ["Moldova PoC", "Moldova deployment", "Moldova project"],
        "metadata": {"status": "Active", "notes": "First international pilot"},
    },
]


def seed():
    """Insert seed entities, skipping any that already exist."""
    created = 0
    skipped = 0

    for entity_data in SEED_ENTITIES:
        name = entity_data["canonical_name"]
        existing = db.find_entity_by_name(name)
        if existing:
            print(f"  SKIP: {name} (already exists)")
            skipped += 1
            continue

        try:
            db.create_entity(
                canonical_name=entity_data["canonical_name"],
                entity_type=entity_data["entity_type"],
                aliases=entity_data.get("aliases", []),
                metadata=entity_data.get("metadata", {}),
            )
            print(f"  OK: {name}")
            created += 1
        except Exception as e:
            print(f"  ERROR: {name} — {e}")

    print(f"\nDone: {created} created, {skipped} skipped")


if __name__ == "__main__":
    print("Seeding entity registry...")
    seed()
