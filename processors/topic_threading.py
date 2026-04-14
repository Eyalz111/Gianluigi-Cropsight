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
from models.schemas import filter_by_sensitivity
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


async def update_topic_state(
    topic_id: str,
    meeting_id: str,
    decisions: list[dict],
    tasks: list[dict],
    open_questions: list[dict] | None = None,
) -> dict | None:
    """
    Incrementally update a topic thread's structured state_json.

    Called post-approval (mirrors T3.1 approval_status gating) for each topic
    thread that this meeting touched. Uses Haiku to merge the previous state
    with this meeting's new context into a fresh TopicState JSON, validates
    against the schema, and writes back.

    Fire-and-forget semantics: on any failure (LLM error, malformed JSON,
    DB write failure), logs a warning and returns None. The previous state
    stays intact. Never raises — the approval flow must continue even if
    state updates fail.

    Args:
        topic_id: UUID of the topic_threads row.
        meeting_id: UUID of the meeting that just touched this topic.
        decisions: Extracted decisions for this topic from this meeting.
        tasks: Extracted tasks for this topic from this meeting.
        open_questions: Optional open questions from this meeting.

    Returns:
        The new state_json dict on success, None on failure.
    """
    import json
    from core.llm import call_llm
    from models.schemas import TopicState

    try:
        # Load existing thread + mentions for context
        thread_row = (
            supabase_client.client.table("topic_threads")
            .select("*")
            .eq("id", topic_id)
            .limit(1)
            .execute()
        )
        if not thread_row.data:
            logger.warning(f"[topic_state] thread {topic_id} not found")
            return None
        thread = thread_row.data[0]
        prev_state = thread.get("state_json") or {}

        # Load meeting metadata
        meeting_row = (
            supabase_client.client.table("meetings")
            .select("id, title, date, summary")
            .eq("id", meeting_id)
            .limit(1)
            .execute()
        )
        meeting = meeting_row.data[0] if meeting_row.data else {}

        # Narrow to items matching this topic. Topic name is stored on
        # topic_threads; the canonical form lives in topic_name. Decisions
        # and tasks were linked via _create_mention() using `label` or the
        # canonical name — match case-insensitively here.
        topic_name_lower = (thread.get("topic_name") or "").lower()

        def _is_topic_item(item: dict) -> bool:
            label = (item.get("label") or "").lower()
            return label == topic_name_lower or topic_name_lower in label or label in topic_name_lower

        topic_decisions = [d for d in decisions if _is_topic_item(d)]
        topic_tasks = [t for t in tasks if _is_topic_item(t)]
        topic_questions = [q for q in (open_questions or []) if _is_topic_item(q)]

        # Build Haiku prompt
        prompt = _build_topic_state_prompt(
            topic_name=thread.get("topic_name", ""),
            previous_state=prev_state,
            meeting=meeting,
            decisions=topic_decisions,
            tasks=topic_tasks,
            open_questions=topic_questions,
        )

        response, _usage = call_llm(
            prompt=prompt,
            model=settings.model_simple,  # Haiku — incremental update, cheap
            max_tokens=1500,  # post-initial-deploy fix: 600 truncated real payloads
            call_site="topic_state_update",
        )

        # Parse + validate
        new_state_dict = _parse_topic_state_json(response)
        if not new_state_dict:
            # Log the raw head of the response so future parse failures can be
            # diagnosed without a diagnostic reproduction.
            logger.warning(
                f"[topic_state] malformed Haiku JSON for {topic_id}; keeping previous state. "
                f"Raw response head: {response[:200]!r}"
            )
            return None

        # Bump version
        new_state_dict["version"] = int(prev_state.get("version", 0)) + 1

        # Validate via Pydantic — rejects malformed shapes
        try:
            validated = TopicState(**new_state_dict).model_dump(mode="json")
        except Exception as e:
            logger.warning(
                f"[topic_state] schema validation failed for {topic_id}: {e}; keeping previous"
            )
            return None

        # Write back
        supabase_client.client.table("topic_threads").update({
            "state_json": validated,
            "state_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", topic_id).execute()

        logger.info(
            f"[topic_state] updated {topic_id} ({thread.get('topic_name')}) "
            f"v{validated.get('version')} status={validated.get('current_status')}"
        )
        return validated

    except Exception as e:
        logger.warning(f"[topic_state] update_topic_state failed for {topic_id}: {e}")
        return None


def _build_topic_state_prompt(
    topic_name: str,
    previous_state: dict,
    meeting: dict,
    decisions: list[dict],
    tasks: list[dict],
    open_questions: list[dict],
) -> str:
    """Build the Haiku prompt for incremental topic-state updates."""
    import json

    def _summarize(items: list[dict], fields: list[str]) -> str:
        if not items:
            return "(none)"
        lines = []
        for item in items[:10]:
            parts = [f"{f}={item.get(f, '')}" for f in fields if item.get(f)]
            lines.append(" | ".join(parts))
        return "\n".join(f"  - {ln}" for ln in lines) if lines else "(none)"

    prev_json = json.dumps(previous_state, indent=2) if previous_state else "(empty — new topic or first update)"
    meeting_date = str(meeting.get("date", ""))[:10]
    meeting_title = meeting.get("title", "Unknown")

    return f"""You maintain structured state for a CropSight topic thread.

Topic: {topic_name}

Previous state (may be empty for a new topic):
{prev_json}

New meeting just happened:
- Date: {meeting_date}
- Title: {meeting_title}
- Decisions on this topic:
{_summarize(decisions, ["description", "rationale"])}
- Tasks on this topic:
{_summarize(tasks, ["title", "assignee", "deadline", "priority"])}
- Open questions on this topic:
{_summarize(open_questions, ["question", "raised_by"])}

Update the topic state. Return ONLY valid JSON matching this shape:

{{
  "current_status": "active" | "blocked" | "pending_decision" | "stale" | "closed",
  "summary": "2-3 sentence current-state narrative",
  "stakeholders": ["names of people actively involved"],
  "open_items": [
    {{"kind": "task"|"question"|"blocker", "description": "...", "owner": "name or null", "source_meeting_id": "uuid or null"}}
  ],
  "last_decision": {{"text": "...", "date": "YYYY-MM-DD", "meeting_id": "...", "meeting_title": "..."}} or null,
  "key_facts": ["durable facts about this topic — milestones, targets, structural decisions"],
  "last_activity_date": "YYYY-MM-DD"
}}

Rules:
- Preserve key_facts from previous state unless explicitly contradicted by the new meeting.
- Replace last_decision only if this meeting made a new decision on this topic.
- Remove open_items from previous state that were resolved in this meeting.
- Add new open_items from this meeting's tasks and open questions.
- Set current_status = 'blocked' if a blocker was explicitly mentioned, 'pending_decision' if an open question dominates, 'closed' if the topic was explicitly resolved, else 'active'.
- Keep summary to 2-3 sentences, focus on current state not history.
- Set last_activity_date to this meeting's date.
- Return ONLY the JSON object. No prose, no code fences, no explanation."""


def _parse_topic_state_json(response: str) -> dict | None:
    """Extract JSON from the Haiku response, tolerating code fences."""
    import json
    import re

    if not response:
        return None
    # Try direct parse first
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass
    # Strip code fences
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    # Last resort: find the outermost JSON object
    obj_match = re.search(r"\{[\s\S]*\}", response)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except json.JSONDecodeError:
            pass
    return None


async def generate_topic_evolution(topic_id: str, max_sensitivity_level: int = 4) -> str:
    """
    Generate a chronological narrative of how a topic evolved across meetings.

    Uses Sonnet to create a paragraph summarizing the topic's journey.
    Default CEO level (MCP-facing tool).

    Args:
        topic_id: UUID of the topic thread.
        max_sensitivity_level: Max tier level (default 4=CEO, MCP is Eyal-only).

    Returns:
        Narrative string.
    """
    from core.llm import call_llm

    thread = _get_thread_with_mentions(topic_id)
    if not thread:
        return "Topic thread not found."

    mentions = thread.get("mentions", [])
    # Filter mentions by their linked meeting sensitivity
    mentions = [
        m for m in mentions
        if filter_by_sensitivity([m.get("meetings", {}) or {}], max_sensitivity_level)
    ]
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
