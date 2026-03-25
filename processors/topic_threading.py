"""
Topic threading — track how projects/topics evolve across meetings.

After extraction, identifies key topics (using labels from decisions and tasks),
stores topic threads, and links each meeting to its threads. Enables cross-meeting
intelligence: "Moldova Pilot has been discussed in 4 meetings over 3 weeks."

Inspired by Hedy AI's "Topic Insights" (Nov 2025) — cross-session meeting intelligence.

Usage:
    from processors.topic_threading import link_meeting_to_topics, get_topic_evolution
    await link_meeting_to_topics(meeting_id, decisions, tasks)
    narrative = await get_topic_evolution("Moldova Pilot")
"""

import logging
from datetime import datetime, timezone

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


async def link_meeting_to_topics(
    meeting_id: str,
    decisions: list[dict],
    tasks: list[dict],
) -> list[dict]:
    """
    Link a meeting to topic threads based on extracted labels.

    For each unique label from decisions and tasks:
    1. Find or create a topic thread (exact match first, then fuzzy)
    2. Create a topic_thread_mention linking this meeting

    Args:
        meeting_id: UUID of the meeting.
        decisions: Extracted decisions with label fields.
        tasks: Extracted tasks with label fields.

    Returns:
        List of topic thread dicts that were linked.
    """
    # Collect all labels from this meeting
    labels = set()
    for d in decisions:
        label = (d.get("label") or "").strip()
        if label and len(label) > 2:
            labels.add(label)
    for t in tasks:
        label = (t.get("label") or "").strip()
        if label and len(label) > 2:
            labels.add(label)

    if not labels:
        logger.debug(f"No labels found for meeting {meeting_id}, skipping topic threading")
        return []

    linked_threads = []

    for label in labels:
        try:
            # Find existing thread (exact match first)
            thread = _find_thread_by_name(label)

            if thread:
                # Update existing thread
                _update_thread_for_meeting(thread, meeting_id)
                _create_mention(thread["id"], meeting_id, decisions, tasks, label)
                linked_threads.append(thread)
            else:
                # Try fuzzy match via canonical names
                canonical = _match_canonical_name(label)
                if canonical and canonical != label:
                    thread = _find_thread_by_name(canonical)
                    if thread:
                        _update_thread_for_meeting(thread, meeting_id)
                        _create_mention(thread["id"], meeting_id, decisions, tasks, label)
                        linked_threads.append(thread)
                        continue

                # Create new thread
                thread = _create_thread(canonical or label, meeting_id)
                _create_mention(thread["id"], meeting_id, decisions, tasks, label)
                linked_threads.append(thread)

        except Exception as e:
            logger.error(f"Error linking topic '{label}' for meeting {meeting_id}: {e}")

    if linked_threads:
        logger.info(
            f"Linked meeting {meeting_id} to {len(linked_threads)} topic threads: "
            f"{[t.get('topic_name', '?') for t in linked_threads]}"
        )

    return linked_threads


async def generate_topic_evolution(topic_id: str) -> str:
    """
    Generate a chronological narrative of how a topic evolved across meetings.

    Uses Sonnet to create a paragraph summarizing the topic's journey.

    Args:
        topic_id: UUID of the topic thread.

    Returns:
        Narrative string.
    """
    from core.llm import call_llm

    thread = _get_thread_with_mentions(topic_id)
    if not thread:
        return "Topic thread not found."

    mentions = thread.get("mentions", [])
    if not mentions:
        return f"Topic '{thread.get('topic_name')}' has no meeting mentions."

    # Build timeline
    timeline_parts = []
    for m in mentions:
        meeting = m.get("meetings", {}) or {}
        date = str(meeting.get("date", ""))[:10]
        title = meeting.get("title", "Unknown")
        context = m.get("context", "")[:200]
        decisions = m.get("decisions_made") or []
        dec_text = "; ".join(decisions[:2]) if decisions else "no decisions"
        timeline_parts.append(f"- {date} ({title}): {context}. Decisions: {dec_text}")

    timeline = "\n".join(timeline_parts)

    prompt = f"""Write a 2-3 sentence narrative summarizing how this topic evolved across meetings.

Topic: {thread.get('topic_name')}
Meeting count: {thread.get('meeting_count', 1)}
Status: {thread.get('status', 'active')}

Timeline:
{timeline}

Write a concise narrative (2-3 sentences) that captures the evolution — what changed, what was decided, and where it stands now."""

    try:
        narrative, _usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,  # Sonnet
            max_tokens=256,
            call_site="topic_evolution",
        )

        # Store the narrative
        supabase_client.client.table("topic_threads").update({
            "evolution_summary": narrative.strip(),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }).eq("id", topic_id).execute()

        return narrative.strip()

    except Exception as e:
        logger.error(f"Topic evolution generation failed: {e}")
        return f"Evolution narrative unavailable: {e}"


def list_active_threads(status: str | None = None) -> list[dict]:
    """List topic threads with optional status filter."""
    query = supabase_client.client.table("topic_threads").select("*")
    if status:
        query = query.eq("status", status)
    else:
        query = query.eq("status", "active")
    result = query.order("last_updated", desc=True).limit(50).execute()
    return result.data or []


def merge_threads(source_id: str, target_id: str) -> dict:
    """
    Merge source thread into target. Re-links all mentions, deletes source.

    Args:
        source_id: Thread to merge FROM (will be deleted).
        target_id: Thread to merge INTO (will be kept).

    Returns:
        Updated target thread.
    """
    # Re-link mentions
    supabase_client.client.table("topic_thread_mentions").update({
        "topic_id": target_id,
    }).eq("topic_id", source_id).execute()

    # Update target meeting count
    mentions = supabase_client.client.table("topic_thread_mentions").select(
        "id", count="exact"
    ).eq("topic_id", target_id).execute()
    new_count = mentions.count or 0

    supabase_client.client.table("topic_threads").update({
        "meeting_count": new_count,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }).eq("id", target_id).execute()

    # Delete source thread
    supabase_client.client.table("topic_threads").delete().eq("id", source_id).execute()

    logger.info(f"Merged topic thread {source_id} into {target_id}")

    result = supabase_client.client.table("topic_threads").select("*").eq("id", target_id).execute()
    return result.data[0] if result.data else {}


def rename_thread(topic_id: str, new_name: str) -> dict:
    """Rename a topic thread."""
    supabase_client.client.table("topic_threads").update({
        "topic_name": new_name,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }).eq("id", topic_id).execute()

    logger.info(f"Renamed topic thread {topic_id} to '{new_name}'")

    result = supabase_client.client.table("topic_threads").select("*").eq("id", topic_id).execute()
    return result.data[0] if result.data else {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_thread_by_name(name: str) -> dict | None:
    """Find a topic thread by exact name match (case-insensitive)."""
    result = (
        supabase_client.client.table("topic_threads")
        .select("*")
        .eq("topic_name_lower", name.lower())
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _match_canonical_name(label: str) -> str | None:
    """
    Match a label to a canonical project name using DB-backed canonical_projects table.

    Falls back to in-memory matching if DB is unavailable.
    """
    # Try DB-backed matching first
    try:
        matched = supabase_client.match_label_to_canonical(label)
        if matched:
            return matched
    except Exception:
        pass

    # Fallback: partial match and word overlap against DB projects
    try:
        projects = supabase_client.get_canonical_projects(status="active")
    except Exception:
        return None

    label_lower = label.lower()

    # Partial match (label contains canonical name or vice versa)
    for p in projects:
        name_lower = p["name"].lower()
        if label_lower in name_lower or name_lower in label_lower:
            return p["name"]

    # Word overlap (>50% of words match)
    label_words = set(label_lower.split())
    for p in projects:
        name_words = set(p["name"].lower().split())
        if label_words and name_words:
            overlap = len(label_words & name_words)
            if overlap / max(len(label_words), len(name_words)) > 0.5:
                return p["name"]

    return None


def _create_thread(topic_name: str, meeting_id: str) -> dict:
    """Create a new topic thread."""
    result = supabase_client.client.table("topic_threads").insert({
        "workspace_id": "cropsight",
        "topic_name": topic_name,
        "status": "active",
        "first_meeting_id": meeting_id,
        "last_meeting_id": meeting_id,
        "meeting_count": 1,
    }).execute()
    thread = result.data[0] if result.data else {}
    logger.info(f"Created new topic thread: '{topic_name}' (id: {thread.get('id')})")
    return thread


def _update_thread_for_meeting(thread: dict, meeting_id: str) -> None:
    """Update an existing thread with a new meeting reference."""
    supabase_client.client.table("topic_threads").update({
        "last_meeting_id": meeting_id,
        "meeting_count": (thread.get("meeting_count", 1) or 1) + 1,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }).eq("id", thread["id"]).execute()


def _create_mention(
    topic_id: str,
    meeting_id: str,
    decisions: list[dict],
    tasks: list[dict],
    label: str,
) -> None:
    """Create a topic thread mention for this meeting."""
    # Build context from decisions and tasks matching this label
    related_decisions = [
        d.get("description", "")[:80]
        for d in decisions
        if (d.get("label") or "").lower() == label.lower()
    ]
    related_tasks = [
        t.get("title", "")[:80]
        for t in tasks
        if (t.get("label") or "").lower() == label.lower()
    ]

    context_parts = []
    if related_decisions:
        context_parts.append(f"Decisions: {'; '.join(related_decisions[:2])}")
    if related_tasks:
        context_parts.append(f"Tasks: {'; '.join(related_tasks[:2])}")

    supabase_client.client.table("topic_thread_mentions").insert({
        "topic_id": topic_id,
        "meeting_id": meeting_id,
        "context": ". ".join(context_parts) if context_parts else None,
        "decisions_made": related_decisions[:3],
    }).execute()


def _get_thread_with_mentions(topic_id: str) -> dict | None:
    """Get a thread with all its mentions and meeting details."""
    thread_result = supabase_client.client.table("topic_threads").select("*").eq(
        "id", topic_id
    ).execute()
    if not thread_result.data:
        return None

    thread = thread_result.data[0]

    mentions_result = (
        supabase_client.client.table("topic_thread_mentions")
        .select("*, meetings(title, date)")
        .eq("topic_id", topic_id)
        .order("created_at")
        .execute()
    )
    thread["mentions"] = mentions_result.data or []

    return thread
