"""
Meeting-to-meeting continuity — cross-meeting context for extraction.

Before extracting from a new transcript, fetches summaries of 2-3 recent
meetings with overlapping participants. This gives the extraction LLM
awareness of what was discussed previously, enabling smarter task status
inference and deduplication.

Usage:
    from processors.meeting_continuity import build_meeting_continuity_context
    context = build_meeting_continuity_context(participants, meeting_id)
"""

import logging

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Max tokens for meeting history context (keeps extraction prompt manageable)
_MAX_CONTEXT_CHARS = 3000


def build_meeting_continuity_context(
    participants: list[str],
    current_meeting_id: str | None = None,
) -> str | None:
    """
    Build a compressed context block from recent meetings with overlapping participants.

    Args:
        participants: Participant names from the current meeting.
        current_meeting_id: UUID of the current meeting (to exclude).

    Returns:
        Formatted context string or None if no relevant history found.
    """
    if not participants:
        return None

    try:
        recent_meetings = supabase_client.get_meetings_by_participant_overlap(
            participants=participants,
            exclude_meeting_id=current_meeting_id,
            limit=3,
        )
    except Exception as e:
        logger.warning(f"Could not fetch meeting history for continuity: {e}")
        return None

    if not recent_meetings:
        return None

    context_parts = []

    for meeting in recent_meetings:
        title = meeting.get("title", "Untitled")
        date = str(meeting.get("date", ""))[:10]
        meeting_id = meeting.get("id", "")

        # Get decisions and open tasks from this meeting
        try:
            decisions = supabase_client.list_decisions(meeting_id=meeting_id)
            tasks_all = supabase_client.get_tasks(status=None)
            tasks = [t for t in tasks_all if t.get("meeting_id") == meeting_id]
            open_tasks = [t for t in tasks if t.get("status") in ("pending", "in_progress")]
            questions = supabase_client.get_open_questions(meeting_id=meeting_id)
            open_qs = [q for q in questions if q.get("status") == "open"]
        except Exception as e:
            logger.debug(f"Could not fetch details for meeting {meeting_id}: {e}")
            decisions = []
            open_tasks = []
            open_qs = []

        parts = [f"Meeting: \"{title}\" ({date})"]

        if decisions:
            decision_lines = [
                f"  - {d.get('description', '')[:80]}"
                for d in decisions[:3]
            ]
            parts.append("  Decisions: " + "; ".join(
                d.get("description", "")[:60] for d in decisions[:3]
            ))

        if open_tasks:
            task_lines = [
                f"{t.get('assignee', '?')}: {t.get('title', '')[:50]}"
                for t in open_tasks[:3]
            ]
            parts.append(f"  Open tasks: {'; '.join(task_lines)}")

        if open_qs:
            parts.append(f"  Open questions: {'; '.join(q.get('question', '')[:50] for q in open_qs[:2])}")

        context_parts.append("\n".join(parts))

    if not context_parts:
        return None

    full_context = "\n\n".join(context_parts)

    # Truncate if too long
    if len(full_context) > _MAX_CONTEXT_CHARS:
        full_context = full_context[:_MAX_CONTEXT_CHARS] + "\n  ..."

    logger.info(
        f"Built meeting continuity context: {len(recent_meetings)} meetings, "
        f"{len(full_context)} chars"
    )

    return full_context
