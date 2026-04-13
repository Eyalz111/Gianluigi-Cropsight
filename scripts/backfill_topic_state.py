"""
One-time backfill: build TopicState JSON for every existing topic_threads
row that has mentions but no state_json yet.

Uses one Sonnet pass per thread (not Haiku — backfill gets a higher-quality
single shot since there's no incremental context to merge against). Reads
the full mention timeline and synthesizes a structured TopicState from it.

**Rate limiting:** time.sleep(1) between calls to stay under Anthropic API
rate limits. For ~50 active topics, total wall time is ~1 minute plus the
generation latency. Cost ~$0.15 total.

Idempotent — running twice is safe. The second run sees state_json already
populated and skips. Re-run with FORCE_REGENERATE=1 to regenerate state for
threads that already have state_json (e.g., after a prompt change).

Usage:
    python scripts/backfill_topic_state.py
    FORCE_REGENERATE=1 python scripts/backfill_topic_state.py   # rebuild all
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from core.llm import call_llm
from models.schemas import TopicState
from processors.topic_threading import _get_thread_with_mentions, _parse_topic_state_json
from services.supabase_client import supabase_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

FORCE = os.environ.get("FORCE_REGENERATE", "").lower() in ("1", "true", "yes")


def _build_backfill_prompt(topic_name: str, thread: dict, mentions: list[dict]) -> str:
    """Build a Sonnet prompt that constructs a TopicState from the full timeline."""
    timeline_parts = []
    for m in mentions:
        meeting = m.get("meetings", {}) or {}
        date = str(meeting.get("date", ""))[:10]
        title = meeting.get("title", "Unknown")
        context = (m.get("context") or "")[:300]
        decisions = m.get("decisions_made") or []
        dec_text = "; ".join(decisions[:3]) if decisions else ""
        timeline_parts.append(
            f"- {date} | {title}\n    context: {context}\n    decisions: {dec_text}"
        )
    timeline = "\n".join(timeline_parts) or "(no mentions)"

    return f"""You are constructing structured state for a CropSight topic thread from its historical mentions.

Topic: {topic_name}
Meeting count: {thread.get('meeting_count', 0)}
Status: {thread.get('status', 'active')}
Evolution narrative (existing): {thread.get('evolution_summary', '(none)')}

Full mention timeline (oldest first):
{timeline}

Construct a TopicState JSON capturing the CURRENT state of this topic based on the timeline. Return ONLY the JSON object:

{{
  "current_status": "active" | "blocked" | "pending_decision" | "stale" | "closed",
  "summary": "2-3 sentence current-state narrative",
  "stakeholders": ["names of people actively involved across the timeline"],
  "open_items": [
    {{"kind": "task"|"question"|"blocker", "description": "...", "owner": "name or null", "source_meeting_id": "uuid or null"}}
  ],
  "last_decision": {{"text": "...", "date": "YYYY-MM-DD", "meeting_id": "...", "meeting_title": "..."}} or null,
  "key_facts": ["durable facts — milestones, targets, structural decisions"],
  "last_activity_date": "YYYY-MM-DD (date of most recent mention)"
}}

Rules:
- current_status reflects the state as of the MOST RECENT mention, not an average
- summary is the current state, not a history — "Paolo is finalizing the RFP" not "We discussed the RFP three times"
- Include only OPEN items that remain unresolved at the last mention. Don't list tasks that were marked done or questions that got answered
- last_activity_date = date of the latest timeline entry
- key_facts should be durable (not fluid status) — pilots, targets, structural commitments
- Return ONLY JSON. No prose, no code fences, no explanation."""


def backfill_one(thread_id: str) -> bool:
    """
    Build state_json for a single thread. Returns True if written.
    Skips if state_json already exists (unless FORCE is set).
    """
    thread = _get_thread_with_mentions(thread_id)
    if not thread:
        logger.warning(f"Thread {thread_id} not found")
        return False

    if thread.get("state_json") and not FORCE:
        return False  # already populated, skip

    mentions = thread.get("mentions") or []
    if not mentions:
        return False  # nothing to build from

    topic_name = thread.get("topic_name", "")
    prompt = _build_backfill_prompt(topic_name, thread, mentions)

    try:
        response, _usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,  # Sonnet — single high-quality backfill pass
            max_tokens=800,
            call_site="topic_state_backfill",
        )
    except Exception as e:
        logger.warning(f"LLM call failed for '{topic_name}' ({thread_id}): {e}")
        return False

    new_state = _parse_topic_state_json(response)
    if not new_state:
        logger.warning(f"Malformed Sonnet JSON for '{topic_name}' ({thread_id}); skipping")
        return False

    new_state["version"] = 1
    try:
        validated = TopicState(**new_state).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"Schema validation failed for '{topic_name}': {e}; skipping")
        return False

    try:
        supabase_client.client.table("topic_threads").update({
            "state_json": validated,
            "state_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", thread_id).execute()
        logger.info(
            f"Backfilled state for '{topic_name}' "
            f"(status={validated.get('current_status')}, "
            f"{len(validated.get('open_items', []))} open items)"
        )
        return True
    except Exception as e:
        logger.warning(f"DB update failed for '{topic_name}' ({thread_id}): {e}")
        return False


def backfill() -> dict:
    """Walk all threads with mentions and backfill state_json."""
    # Load all threads with at least one mention
    rows = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, meeting_count, state_json")
        .gt("meeting_count", 0)
        .limit(1000)
        .execute()
    )
    threads = rows.data or []
    if not threads:
        logger.info("No threads with mentions — nothing to backfill.")
        return {"threads_seen": 0, "threads_backfilled": 0, "threads_skipped": 0}

    logger.info(
        f"Backfill target: {len(threads)} threads with mentions "
        f"(FORCE_REGENERATE={FORCE})"
    )

    backfilled = 0
    skipped = 0
    for i, t in enumerate(threads, 1):
        thread_id = t["id"]
        name = t.get("topic_name", "?")
        if t.get("state_json") and not FORCE:
            logger.debug(f"[{i}/{len(threads)}] skipping '{name}' — already has state")
            skipped += 1
            continue
        logger.info(f"[{i}/{len(threads)}] backfilling '{name}'")
        if backfill_one(thread_id):
            backfilled += 1
        # Rate limit — 1s between calls to stay under Anthropic API limits
        time.sleep(1)

    summary = {
        "threads_seen": len(threads),
        "threads_backfilled": backfilled,
        "threads_skipped": skipped,
    }
    logger.info(f"Backfill complete: {summary}")

    try:
        supabase_client.log_action(
            action="backfill_topic_state",
            details=summary,
            triggered_by="auto",
        )
    except Exception as e:
        logger.warning(f"Audit log entry failed (non-fatal): {e}")

    return summary


if __name__ == "__main__":
    result = backfill()
    print(f"\nSummary: {result}")
