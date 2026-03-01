"""
Meeting preparation document generator.

This module generates prep documents for upcoming meetings by:
1. Reading calendar event details
2. Searching past meetings for related discussions
3. Finding relevant decisions and open questions
4. Checking stakeholder tracker for context
5. Identifying overdue tasks for participants
6. Compiling into a prep document

This is the shared logic used by both the scheduler and ad-hoc prep generation.

Usage:
    from processors.meeting_prep import generate_meeting_prep

    prep = await generate_meeting_prep(calendar_event_id="...")
"""

import json
import logging
from datetime import datetime
from typing import Any

from config.settings import settings
from core.llm import call_llm
from services.google_calendar import calendar_service
from services.google_sheets import sheets_service
from services.supabase_client import supabase_client
from services.embeddings import embedding_service

logger = logging.getLogger(__name__)


async def generate_meeting_prep(calendar_event_id: str) -> dict:
    """
    Generate a meeting preparation document for an upcoming meeting.

    This is the orchestrator function that calls all helper functions
    and assembles the final prep document.

    Args:
        calendar_event_id: Google Calendar event ID.

    Returns:
        Dict containing:
        - event: Calendar event details
        - prep_document: Formatted prep document (Markdown)
        - related_meetings: Past meetings on similar topics
        - relevant_decisions: Decisions relevant to the topic
        - open_questions: Unresolved questions to address
        - participant_tasks: Tasks assigned to participants
        - stakeholder_info: Relevant stakeholder context
    """
    # 1. Fetch calendar event details
    event = await calendar_service.get_event(calendar_event_id)
    if not event:
        logger.error(f"Event not found: {calendar_event_id}")
        return {"error": "Event not found", "event_id": calendar_event_id}

    # 2. Extract topic and participants
    topic = event.get("title", "Untitled Meeting")
    attendees = event.get("attendees", [])
    participant_names = [
        a.get("displayName") or a.get("email", "").split("@")[0]
        for a in attendees
    ]

    # 3. Search for related past meetings
    related_meetings = await find_related_meetings(topic, participant_names)

    # 4. Find relevant decisions (hybrid: semantic + keyword)
    relevant_decisions = await find_relevant_decisions(topic)

    # 5. Get open questions from Supabase
    open_questions = _find_open_questions(topic)

    # 6. Get stakeholder context for external participants
    stakeholder_info = await get_stakeholder_context(participant_names, topic)

    # 7. Find open tasks for each participant
    participant_tasks = await find_participant_tasks(participant_names)

    # 8. Synthesize strategic insights from all gathered context
    synthesis = await synthesize_prep_insights(
        topic=topic,
        participant_names=participant_names,
        related_meetings=related_meetings,
        relevant_decisions=relevant_decisions,
        open_questions=open_questions,
        participant_tasks=participant_tasks,
    )

    # 9. Build the Markdown prep document
    prep_document = format_prep_document(
        event=event,
        related_meetings=related_meetings,
        relevant_decisions=relevant_decisions,
        open_questions=open_questions,
        participant_tasks=participant_tasks,
        stakeholder_info=stakeholder_info,
        synthesis=synthesis,
    )

    return {
        "event": event,
        "prep_document": prep_document,
        "related_meetings": related_meetings,
        "relevant_decisions": relevant_decisions,
        "open_questions": open_questions,
        "participant_tasks": participant_tasks,
        "stakeholder_info": stakeholder_info,
        "synthesis": synthesis,
    }


async def find_related_meetings(
    topic: str,
    participants: list[str],
    limit: int = 5
) -> list[dict]:
    """
    Find past meetings related to an upcoming meeting topic.

    Uses semantic search (embed the topic, then search embeddings table)
    and deduplicates by meeting ID so we get unique meetings.

    Args:
        topic: Meeting title/topic for semantic search.
        participants: Meeting participants (for future participant filtering).
        limit: Maximum number of related meetings to return.

    Returns:
        List of related meeting dicts with title, date, summary snippet.
    """
    try:
        # Embed the topic for semantic search
        query_embedding = await embedding_service.embed_text(topic)

        # Search for similar chunks in the embeddings table
        # Get extra results so we have enough after deduplication
        similar = supabase_client.search_embeddings(
            query_embedding=query_embedding,
            limit=limit * 2,
            source_type="meeting",
        )

        # Extract unique meeting IDs from the chunks
        seen_meeting_ids: set[str] = set()
        related: list[dict] = []

        for item in similar:
            meeting_id = item.get("source_id")
            if not meeting_id or meeting_id in seen_meeting_ids:
                continue
            seen_meeting_ids.add(meeting_id)

            # Look up the full meeting record
            meeting = supabase_client.get_meeting(meeting_id)
            if not meeting:
                continue

            # Build a summary snippet (first 200 chars)
            summary = meeting.get("summary", "") or ""
            snippet = (summary[:200] + "...") if len(summary) > 200 else summary

            related.append({
                "meeting_id": meeting_id,
                "title": meeting.get("title", ""),
                "date": meeting.get("date", ""),
                "summary": snippet,
                "similarity": item.get("similarity", 0),
            })

            if len(related) >= limit:
                break

        return related

    except Exception as e:
        logger.error(f"Error finding related meetings: {e}")
        return []


async def find_relevant_decisions(
    topic: str,
    limit: int = 10
) -> list[dict]:
    """
    Find past decisions relevant to a meeting topic.

    Uses a hybrid approach:
    1. Semantic search via embeddings (finds conceptually similar decisions)
    2. Keyword ILIKE search via supabase_client.list_decisions (exact matches)
    Results are combined and deduplicated by decision ID.

    Args:
        topic: Meeting topic for semantic search.
        limit: Maximum number of decisions to return.

    Returns:
        List of relevant decision dicts with description, context, source.
    """
    seen_ids: set[str] = set()
    combined: list[dict] = []

    # --- Strategy 1: Semantic search on embeddings ---
    try:
        query_embedding = await embedding_service.embed_text(topic)
        similar = supabase_client.search_embeddings(
            query_embedding=query_embedding,
            limit=limit,
            source_type="decision",
        )

        for item in similar:
            decision_id = item.get("source_id") or item.get("id", "")
            if decision_id in seen_ids:
                continue
            seen_ids.add(decision_id)

            combined.append({
                "id": decision_id,
                "description": item.get("chunk_text", ""),
                "context": (item.get("metadata") or {}).get("context", ""),
                "source_meeting": (item.get("metadata") or {}).get("meeting_title", ""),
                "date": (item.get("metadata") or {}).get("date", ""),
            })
    except Exception as e:
        logger.warning(f"Semantic decision search failed: {e}")

    # --- Strategy 2: Keyword ILIKE search ---
    try:
        keyword_decisions = supabase_client.list_decisions(topic=topic, limit=limit)

        for d in keyword_decisions:
            decision_id = d.get("id", "")
            if decision_id in seen_ids:
                continue
            seen_ids.add(decision_id)

            # Extract meeting info from the joined relation
            meetings_data = d.get("meetings") or {}
            combined.append({
                "id": decision_id,
                "description": d.get("description", ""),
                "context": d.get("context", ""),
                "source_meeting": meetings_data.get("title", ""),
                "date": meetings_data.get("date", ""),
            })
    except Exception as e:
        logger.warning(f"Keyword decision search failed: {e}")

    return combined[:limit]


async def find_participant_tasks(
    participants: list[str]
) -> dict[str, list[dict]]:
    """
    Find open and overdue tasks for meeting participants.

    Queries Supabase for each participant's pending tasks.

    Args:
        participants: List of participant names.

    Returns:
        Dict mapping participant names to their task lists.
    """
    tasks_by_participant: dict[str, list[dict]] = {}

    for participant in participants:
        try:
            tasks = supabase_client.get_tasks(
                assignee=participant,
                status="pending",
            )
            if tasks:
                tasks_by_participant[participant] = tasks
        except Exception as e:
            logger.warning(f"Error getting tasks for {participant}: {e}")

    return tasks_by_participant


async def get_stakeholder_context(
    participant_names: list[str],
    meeting_title: str
) -> list[dict]:
    """
    Get stakeholder tracker info for external participants or topic.

    Looks up each participant name in the Google Sheets stakeholder tracker
    to provide context about external contacts.

    Args:
        participant_names: Names to look up.
        meeting_title: Meeting title for topic-based lookup.

    Returns:
        List of relevant stakeholder entries.
    """
    stakeholders: list[dict] = []

    for name in participant_names:
        try:
            matches = await sheets_service.get_stakeholder_info(name=name)
            stakeholders.extend(matches)
        except Exception as e:
            logger.warning(f"Error looking up stakeholder {name}: {e}")

    return stakeholders


async def synthesize_prep_insights(
    topic: str,
    participant_names: list[str],
    related_meetings: list[dict],
    relevant_decisions: list[dict],
    open_questions: list[dict],
    participant_tasks: dict[str, list[dict]],
) -> dict:
    """
    Use LLM to synthesize gathered context into strategic meeting insights.

    Produces conversation starters, decision relevance assessment, and
    diplomatically flags overdue tasks.

    Args:
        topic: Meeting topic.
        participant_names: Attendee names.
        related_meetings: Past related meetings.
        relevant_decisions: Relevant past decisions.
        open_questions: Unresolved questions.
        participant_tasks: Tasks by participant.

    Returns:
        Dict with keys: decision_notes, conversation_starters, overdue_flags.
        Returns empty dict on error.
    """
    # Skip synthesis if there's no meaningful context to work with
    if not related_meetings and not relevant_decisions and not open_questions:
        return {}

    # Build context summary for the LLM
    context_parts = [f"Meeting topic: {topic}", f"Attendees: {', '.join(participant_names)}"]

    if relevant_decisions:
        decision_list = "\n".join(
            f"- {d.get('description', '')} (from {d.get('source_meeting', '?')}, {d.get('date', '?')})"
            for d in relevant_decisions[:10]
        )
        context_parts.append(f"Past decisions:\n{decision_list}")

    if open_questions:
        q_list = "\n".join(
            f"- {q.get('question', '')} (raised by {q.get('raised_by', '?')})"
            for q in open_questions[:5]
        )
        context_parts.append(f"Open questions:\n{q_list}")

    if participant_tasks:
        for name, tasks in participant_tasks.items():
            overdue = [t for t in tasks if t.get("status") == "overdue" or (
                t.get("deadline") and t["deadline"] < datetime.now().strftime("%Y-%m-%d")
                and t.get("status") != "completed"
            )]
            if overdue:
                task_list = "\n".join(f"  - {t.get('title', '')} (due: {t.get('deadline', '?')})" for t in overdue)
                context_parts.append(f"{name}'s overdue tasks:\n{task_list}")

    if related_meetings:
        meeting_list = "\n".join(
            f"- {m.get('title', '')} ({m.get('date', '?')}): {m.get('summary', '')[:150]}"
            for m in related_meetings[:5]
        )
        context_parts.append(f"Related past meetings:\n{meeting_list}")

    prompt = f"""Given this context for an upcoming meeting, provide strategic preparation insights.

{chr(10).join(context_parts)}

Return valid JSON with this structure:
{{
    "decision_notes": ["For each past decision, one sentence on whether it's still relevant or needs revisiting"],
    "conversation_starters": ["2-3 specific questions that could move stuck items forward or clarify open issues"],
    "overdue_flags": ["Diplomatic notes about any overdue items that should be addressed — frame constructively, not critically"]
}}

Rules:
- Be specific and actionable, not generic
- Conversation starters should reference actual open questions or decisions
- Overdue flags should be framed as "opportunities to unblock" not blame
- If nothing is overdue, return an empty list for overdue_flags
- Keep each item to 1-2 sentences"""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=1024,
            call_site="meeting_prep_synthesis",
        )
        return json.loads(response_text)
    except Exception as e:
        logger.warning(f"Meeting prep synthesis failed (non-fatal): {e}")
        return {}


def format_prep_document(
    event: dict,
    related_meetings: list[dict],
    relevant_decisions: list[dict],
    open_questions: list[dict],
    participant_tasks: dict[str, list[dict]],
    stakeholder_info: list[dict],
    synthesis: dict | None = None,
) -> str:
    """
    Format all prep information into a Markdown document.

    Builds a structured document with sections for meeting details,
    stakeholder context, strategic briefing, related meetings, decisions,
    open questions, and participant tasks.

    Args:
        event: Calendar event details.
        related_meetings: Past related meetings.
        relevant_decisions: Relevant past decisions.
        open_questions: Open questions that might be addressed.
        participant_tasks: Tasks by participant.
        stakeholder_info: Stakeholder context.
        synthesis: LLM-generated strategic insights.

    Returns:
        Formatted Markdown prep document.
    """
    title = event.get("title", "Untitled Meeting")
    start = event.get("start", "")
    location = event.get("location", "")
    attendees = event.get("attendees", [])
    description = event.get("description", "")

    # Format attendee names
    attendee_names = [
        a.get("displayName") or a.get("email", "")
        for a in attendees
    ]

    lines: list[str] = [
        f"# Meeting Prep: {title}",
        "",
        f"**When:** {start}",
        f"**Where:** {location or 'Not specified'}",
        f"**Attendees:** {', '.join(attendee_names) or 'Not specified'}",
        "",
    ]

    # Agenda from event description
    if description:
        lines.extend([
            "## Agenda",
            "",
            description,
            "",
        ])

    # Strategic briefing (LLM-synthesized insights)
    if synthesis:
        has_content = False
        briefing_lines = ["## Strategic Briefing", ""]

        starters = synthesis.get("conversation_starters", [])
        if starters:
            has_content = True
            briefing_lines.append("**Conversation Starters:**")
            for s in starters:
                briefing_lines.append(f"- {s}")
            briefing_lines.append("")

        decision_notes = synthesis.get("decision_notes", [])
        if decision_notes:
            has_content = True
            briefing_lines.append("**Decision Relevance:**")
            for n in decision_notes:
                briefing_lines.append(f"- {n}")
            briefing_lines.append("")

        overdue_flags = synthesis.get("overdue_flags", [])
        if overdue_flags:
            has_content = True
            briefing_lines.append("**Items to Unblock:**")
            for f in overdue_flags:
                briefing_lines.append(f"- {f}")
            briefing_lines.append("")

        if has_content:
            lines.extend(briefing_lines)

    # Stakeholder context for external participants
    if stakeholder_info:
        lines.extend([
            "## Stakeholder Context",
            "",
        ])
        for s in stakeholder_info:
            lines.append(f"### {s.get('organization_name', 'Unknown')}")
            lines.append(f"- **Type:** {s.get('type', 'N/A')}")
            lines.append(f"- **Description:** {s.get('description', 'N/A')}")
            lines.append(f"- **Desired Outcome:** {s.get('desired_outcome', 'N/A')}")
            lines.append(f"- **Status:** {s.get('status', 'N/A')}")
            if s.get("notes"):
                lines.append(f"- **Notes:** {s.get('notes')}")
            lines.append("")

    # Related past meetings
    if related_meetings:
        lines.extend([
            "## Related Past Meetings",
            "",
        ])
        for m in related_meetings:
            lines.append(f"### {m.get('title')} ({m.get('date', 'N/A')})")
            lines.append(f"{m.get('summary', 'No summary available')}")
            lines.append("")

    # Relevant decisions
    if relevant_decisions:
        lines.extend([
            "## Relevant Past Decisions",
            "",
        ])
        for d in relevant_decisions:
            source = d.get("source_meeting", "Unknown meeting")
            date_val = d.get("date", "")
            lines.append(f"- **{d.get('description')}** ({source}, {date_val})")
        lines.append("")

    # Open questions
    if open_questions:
        lines.extend([
            "## Open Questions",
            "",
        ])
        for q in open_questions:
            raised_by = q.get("raised_by", "Unknown")
            lines.append(f"- {q.get('question')} (raised by {raised_by})")
        lines.append("")

    # Participant tasks
    if participant_tasks:
        lines.extend([
            "## Participant Tasks",
            "",
        ])
        for participant, tasks in participant_tasks.items():
            lines.append(f"### {participant}")
            for t in tasks:
                task_title = t.get("title", t.get("task", ""))
                priority = t.get("priority", "M")
                deadline = t.get("deadline", "No deadline")
                status = t.get("status", "pending")
                lines.append(
                    f"- [{priority}] {task_title} (due: {deadline}, {status})"
                )
            lines.append("")

    # Footer
    lines.extend([
        "---",
        f"*Generated by Gianluigi on {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
    ])

    return "\n".join(lines)


def _find_open_questions(topic: str, limit: int = 5) -> list[dict]:
    """
    Find unresolved questions that might be relevant to the meeting topic.

    Uses simple keyword overlap to filter open questions from Supabase.

    Args:
        topic: Meeting topic for relevance filtering.
        limit: Maximum number of questions to return.

    Returns:
        List of open question dicts.
    """
    try:
        # Get open questions from Supabase (sync call)
        questions = supabase_client.get_open_questions(limit=limit * 2)

        # Filter by keyword overlap with the topic
        topic_words = set(topic.lower().split())
        relevant: list[dict] = []

        for q in questions:
            question_text = q.get("question", "")
            question_words = set(question_text.lower().split())
            if topic_words & question_words:  # Any word overlap
                relevant.append({
                    "question": question_text,
                    "raised_by": q.get("raised_by", ""),
                    "meeting_id": q.get("meeting_id"),
                })

            if len(relevant) >= limit:
                break

        return relevant

    except Exception as e:
        logger.error(f"Error finding open questions: {e}")
        return []
