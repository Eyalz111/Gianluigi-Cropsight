"""
v2.5 PR1 backfill — seed Areas from the live Gantt structure and assign
existing topic threads to them. Idempotent; safe to re-run.

- Areas come from DISTINCT gantt_schema.section (the Gantt is the naming
  source — a one-way dependency), excluding structural/overlay sections
  (OPERATIONAL RULES, STRATEGIC MILESTONES, Meeting Cadence, _config/_metadata).
- Each existing topic_thread is assigned to an Area by a CONSERVATIVE
  word-overlap match; left unassigned (NULL) when uncertain — those get
  routed to the 1d clustering proposals later, not guessed here.
- A belongs_to link is materialized in knowledge_links for each assignment.

Prereqs: apply scripts/migrate_phase_v25_knowledge.sql first, and ensure
gantt_schema is populated (scripts/parse_gantt_schema.py).

Usage:
    python scripts/backfill_knowledge_v25.py
"""

import logging
import os
import re
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Sections that are structure/overlay, not workstreams — never become Areas.
# STRATEGIC MILESTONES is a marker layer over the workstreams; its items become
# `advances` links to the real Area/topic (handled later), not their own Area.
_EXCLUDED_SECTIONS = {
    "operational rules",
    "strategic milestones",
    "meeting cadence",
    "_config",
    "_metadata",
}

_STOP_WORDS = {
    "the", "a", "an", "and", "of", "for", "to", "in", "on", "project", "plan",
}


def _words(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    return {w for w in cleaned.split() if w and w not in _STOP_WORDS}


def _distinct_sections() -> list[str]:
    """DISTINCT workstream sections from gantt_schema (excludes structural)."""
    rows = supabase_client.client.table("gantt_schema").select("section").execute().data or []
    seen: dict[str, str] = {}
    for r in rows:
        sec = (r.get("section") or "").strip()
        if not sec or sec.lower() in _EXCLUDED_SECTIONS:
            continue
        seen.setdefault(sec.lower(), sec)  # preserve first-seen original casing
    return sorted(seen.values())


def seed_areas() -> dict[str, dict]:
    """Create an Area per distinct workstream section. Returns name -> area row."""
    areas_by_name: dict[str, dict] = {}
    sections = _distinct_sections()
    if not sections:
        logger.warning(
            "No workstream sections found in gantt_schema — "
            "run scripts/parse_gantt_schema.py first."
        )
    for sec in sections:
        area = supabase_client.add_area(name=sec, gantt_section=sec)
        if area:
            areas_by_name[sec] = area
    logger.info(f"Seeded/confirmed {len(areas_by_name)} areas: {list(areas_by_name)}")
    return areas_by_name


def _best_area(topic_name: str, areas: list[dict]) -> dict | None:
    """
    Conservative match: pick the Area sharing the most distinctive words with
    the topic name. Returns None when nothing meaningful overlaps (leave NULL).
    """
    tw = _words(topic_name)
    if not tw:
        return None
    best, best_overlap = None, 0
    for a in areas:
        aw = _words(a.get("name", "")) | _words(a.get("gantt_section", "") or "")
        overlap = len(tw & aw)
        if overlap > best_overlap:
            best, best_overlap = a, overlap
    return best if best_overlap >= 1 else None


def assign_topics(areas_by_name: dict[str, dict]) -> dict:
    """Best-effort assign existing topic threads to Areas + materialize links."""
    areas = list(areas_by_name.values())
    topics = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, area_id")
        .execute()
        .data
        or []
    )
    assigned, skipped = 0, 0
    for t in topics:
        if t.get("area_id"):
            continue  # idempotent: already assigned
        area = _best_area(t.get("topic_name", ""), areas)
        if not area:
            skipped += 1
            continue
        supabase_client.set_topic_area(t["id"], area["id"])
        supabase_client.create_knowledge_link(
            from_type="topic", from_id=t["id"],
            to_type="area", to_id=area["id"],
            link_type="belongs_to", created_by="backfill",
        )
        assigned += 1
    logger.info(
        f"Assigned {assigned} topics to areas; left {skipped} unassigned "
        f"(-> 1d clustering proposals)."
    )
    return {"assigned": assigned, "unassigned": skipped, "total_topics": len(topics)}


def run_backfill() -> dict:
    """Seed Areas, then assign topics. Idempotent end-to-end."""
    areas_by_name = seed_areas()
    result = assign_topics(areas_by_name)
    result["areas"] = list(areas_by_name)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Backfilling v2.5 knowledge (areas from Gantt + topic assignment)...")
    res = run_backfill()
    print(f"Done: {res}")
