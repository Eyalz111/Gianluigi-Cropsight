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
from config.meeting_prep_templates import get_template
from core.llm import call_llm
from services.google_calendar import calendar_service
from services.google_sheets import sheets_service
from services.supabase_client import supabase_client
from models.schemas import filter_by_sensitivity
from services.embeddings import embedding_service

logger = logging.getLogger(__name__)


async def generate_meeting_prep(calendar_event_id: str, max_sensitivity_level: int = 3) -> dict:
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

    # 3. Search for related past meetings (filtered by sensitivity)
    related_meetings = await find_related_meetings(topic, participant_names, max_sensitivity_level=max_sensitivity_level)

    # 4. Find relevant decisions (hybrid: semantic + keyword, filtered)
    relevant_decisions = await find_relevant_decisions(topic, max_sensitivity_level=max_sensitivity_level)

    # 5. Get open questions from Supabase (filtered)
    open_questions = _find_open_questions(topic, max_sensitivity_level=max_sensitivity_level)

    # 6. Get stakeholder context for external participants
    stakeholder_info = await get_stakeholder_context(participant_names, topic)

    # 7. Find open tasks for each participant
    participant_tasks = await find_participant_tasks(participant_names, max_sensitivity_level=max_sensitivity_level)

    # 7b. Find related documents
    from processors.document_processor import find_related_documents
    related_documents = find_related_documents(topic)

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
        related_documents=related_documents,
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
        "related_documents": related_documents,
    }


async def find_related_meetings(
    topic: str,
    participants: list[str],
    limit: int = 5,
    max_sensitivity_level: int = 3,
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

            # Skip meetings above the sensitivity threshold
            if not filter_by_sensitivity([meeting], max_sensitivity_level):
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
    limit: int = 10,
    max_sensitivity_level: int = 3,
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
        keyword_decisions = filter_by_sensitivity(
            supabase_client.list_decisions(topic=topic, limit=limit), max_sensitivity_level
        )

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
    participants: list[str],
    max_sensitivity_level: int = 3,
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
            tasks = filter_by_sensitivity(
                supabase_client.get_tasks(assignee=participant, status="pending"),
                max_sensitivity_level,
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
    related_documents: list[dict] | None = None,
) -> str:
    """
    Format all prep information into a Markdown document.

    Builds a structured document with sections for meeting details,
    stakeholder context, strategic briefing, related meetings, decisions,
    open questions, related documents, and participant tasks.

    Args:
        event: Calendar event details.
        related_meetings: Past related meetings.
        relevant_decisions: Relevant past decisions.
        open_questions: Open questions that might be addressed.
        participant_tasks: Tasks by participant.
        stakeholder_info: Stakeholder context.
        synthesis: LLM-generated strategic insights.
        related_documents: Documents related to the meeting topic.

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

    # Related documents
    if related_documents:
        lines.extend([
            "## Related Documents",
            "",
        ])
        for doc in related_documents:
            doc_type = doc.get("document_type", "other")
            ingested = doc.get("ingested_at", "")[:10] if doc.get("ingested_at") else ""
            summary = doc.get("summary", "")
            summary_preview = summary[:150] + "..." if len(summary) > 150 else summary
            lines.append(f"- **{doc.get('title', 'Untitled')}** ({doc_type}, {ingested})")
            if summary_preview:
                lines.append(f"  {summary_preview}")
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


def _find_open_questions(topic: str, limit: int = 5, max_sensitivity_level: int = 3) -> list[dict]:
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
        questions = filter_by_sensitivity(
            supabase_client.get_open_questions(limit=limit * 2), max_sensitivity_level
        )

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


# =============================================================================
# Phase 5 — Template-Driven Outline Generation
# =============================================================================


async def _generate_outline_narrative(sections: list[dict], template_name: str) -> str:
    """Generate a 3-5 line narrative summary of outline data via Haiku.

    Instead of 'Data: 5 tasks, 3 decisions', produces something like:
    'Roye has 3 open tasks (1 overdue). 2 new decisions since last sync.
     Gantt Product & Tech on track except pipeline delay.'
    """
    # Build context from section data
    context_parts = []
    for s in sections:
        if s["status"] == "ok" and s.get("item_count", 0) > 0:
            context_parts.append(f"Section '{s['name']}': {s['item_count']} items")
            # Add specific data highlights if available
            if isinstance(s.get("data"), dict):
                for key, val in s["data"].items():
                    if isinstance(val, list) and val:
                        context_parts.append(f"  {key}: {len(val)} items")
            elif isinstance(s.get("data"), list) and s["data"]:
                for item in s["data"][:3]:
                    if isinstance(item, dict):
                        title = item.get("title") or item.get("description") or item.get("commitment", "")
                        if title:
                            context_parts.append(f"  - {title[:60]}")
        elif s["status"] != "ok":
            context_parts.append(f"Section '{s['name']}': unavailable")

    if not context_parts:
        return "No data gathered yet."

    prompt = f"""You are an executive assistant writing a brief status update for a CEO.
Summarize this meeting prep data in 3-5 short lines. Be specific — mention names, numbers, and what's overdue or notable. No headers, no bullets, just flowing text. Keep it under 200 characters per line.

CRITICAL: ONLY use information from the data below. Do NOT invent names, companies, numbers, or details. If the data is sparse, say so briefly — never fabricate.

Meeting type: {template_name}
Data:
{chr(10).join(context_parts)}"""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,  # Haiku
            max_tokens=300,
            call_site="outline_narrative",
        )
        return response_text.strip()
    except Exception as e:
        # Fallback to simple data inventory
        logger.warning(f"Narrative generation failed: {e}")
        parts = []
        for s in sections:
            if s["status"] == "ok" and s.get("item_count", 0) > 0:
                parts.append(f"{s['item_count']} {s['name'].lower()}")
        return "Data: " + ", ".join(parts) if parts else "No data gathered."


async def generate_prep_outline(event: dict, meeting_type: str) -> dict:
    """
    Generate a structured prep outline from template-driven data queries.

    Each data query degrades gracefully — failed queries show as unavailable
    sections rather than crashing the entire outline.

    Args:
        event: Calendar event dict.
        meeting_type: Template key (e.g. 'founders_technical').

    Returns:
        Dict with meeting_type, template_name, event, sections, suggested_agenda,
        event_start_time.
    """
    template = get_template(meeting_type)
    topic = event.get("title", "Untitled Meeting")
    attendees = event.get("attendees", [])
    participant_names = [
        a.get("displayName") or a.get("email", "").split("@")[0]
        for a in attendees
    ]

    sections = []
    for query in template.get("data_queries", []):
        try:
            result = await _execute_data_query(query, event, participant_names)
            item_count = len(result) if isinstance(result, (list, dict)) else 0
            if isinstance(result, dict):
                item_count = sum(len(v) for v in result.values() if isinstance(v, list))
            sections.append({
                "name": _query_to_section_name(query),
                "data": result,
                "status": "ok",
                "item_count": item_count,
            })
        except Exception as e:
            logger.warning(f"Prep data query failed: {query.get('type')} — {e}")
            sections.append({
                "name": _query_to_section_name(query),
                "data": None,
                "status": f"unavailable: {e}",
                "item_count": 0,
            })

    # Generate narrative summary for Telegram briefing card
    narrative = await _generate_outline_narrative(sections, template.get("display_name", meeting_type))

    # Generate suggested agenda from gathered data via Haiku
    suggested_agenda = await _generate_suggested_agenda(topic, sections, template)

    return {
        "meeting_type": meeting_type,
        "template_name": template.get("display_name", meeting_type),
        "event": event,
        "sections": sections,
        "narrative": narrative,
        "suggested_agenda": suggested_agenda,
        "event_start_time": event.get("start", ""),
    }


async def _execute_data_query(
    query: dict, event: dict, participant_names: list[str]
) -> Any:
    """
    Execute a single data query from a template.

    Args:
        query: Query dict with 'type' and optional filters.
        event: Calendar event dict.
        participant_names: List of attendee names.

    Returns:
        Query result (list or dict).
    """
    query_type = query.get("type", "")
    query_filter = query.get("filter", {})

    if query_type == "tasks":
        assignee = query_filter.get("assignee")
        status = query_filter.get("status", "pending")
        if assignee:
            return await find_participant_tasks([assignee])
        else:
            return await find_participant_tasks(participant_names)

    elif query_type == "decisions":
        topic = event.get("title", "")
        return await find_relevant_decisions(topic)

    elif query_type == "open_questions":
        topic = event.get("title", "")
        return _find_open_questions(topic)

    elif query_type == "commitments":
        return supabase_client.get_commitments(status="open")

    elif query_type == "gantt_section":
        section = query.get("section", "")
        from services.gantt_manager import gantt_manager
        return await gantt_manager.get_gantt_section(section)

    elif query_type == "since_last_meeting":
        last_meeting = supabase_client.get_last_meeting_of_type(
            query.get("meeting_type", "")
        )
        if not last_meeting:
            return {"note": "First meeting of this type — no prior data"}
        since_date = last_meeting.get("meeting_date", "")
        if not since_date:
            return {"note": "Previous meeting has no date"}
        changes = supabase_client.get_changes_since(since_date)
        changes["last_meeting_date"] = since_date
        changes["last_meeting_title"] = last_meeting.get("title", "Unknown")
        return changes

    elif query_type == "entity_timeline":
        entity_type = query.get("entity_type", "organization")
        return supabase_client.list_entities(entity_type=entity_type, limit=20)

    else:
        logger.warning(f"Unknown data query type: {query_type}")
        return []


def _query_to_section_name(query: dict) -> str:
    """Convert a data query dict into a human-readable section name."""
    query_type = query.get("type", "unknown")
    query_filter = query.get("filter", {})

    if query_type == "tasks":
        assignee = query_filter.get("assignee")
        if assignee:
            return f"{assignee}'s Open Tasks"
        return "All Open Tasks"
    elif query_type == "decisions":
        return "Recent Decisions"
    elif query_type == "open_questions":
        return "Open Questions"
    elif query_type == "commitments":
        return "Open Commitments"
    elif query_type == "gantt_section":
        section = query.get("section", "")
        return f"Gantt Status: {section}"
    elif query_type == "since_last_meeting":
        return "Since Last Meeting"
    elif query_type == "entity_timeline":
        return "Stakeholder Updates"
    else:
        return query_type.replace("_", " ").title()


async def _generate_suggested_agenda(
    topic: str, sections: list[dict], template: dict
) -> list[str]:
    """
    Generate a suggested agenda from gathered data via Haiku.

    Args:
        topic: Meeting title/topic.
        sections: Gathered data sections.
        template: Template dict with focus_areas.

    Returns:
        List of agenda item strings.
    """
    # Build context from sections
    context_parts = [f"Meeting: {topic}"]
    context_parts.append(f"Meeting type: {template.get('display_name', 'General')}")
    context_parts.append(f"Focus areas: {template.get('focus_areas', 'General')}")

    for section in sections:
        if section["status"] == "ok" and section["item_count"] > 0:
            context_parts.append(f"- {section['name']}: {section['item_count']} items")
        elif section["status"] != "ok":
            context_parts.append(f"- {section['name']}: unavailable")

    prompt = f"""Based on this meeting context, suggest 4-6 concise agenda items.
The agenda should reflect the meeting type's priorities.
Meeting type guidance: {template.get('focus_areas', 'General meeting')}

Return a JSON array of strings only. No explanation.

{chr(10).join(context_parts)}"""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=256,
            call_site="prep_agenda_generation",
        )
        return json.loads(response_text)
    except Exception as e:
        logger.warning(f"Agenda generation failed (non-fatal): {e}")
        return [f"Review {s['name']}" for s in sections if s["status"] == "ok"]


def _html_escape(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_outline_for_telegram(outline: dict, confidence: str = "auto") -> str:
    """
    Format a prep outline as a Telegram briefing card (HTML).

    Designed for phone scanning: 10-15 lines, plain text with minimal
    HTML formatting. No Markdown, no walls of text.

    Args:
        outline: Outline dict from generate_prep_outline().
        confidence: 'auto', 'ask', or 'none' — affects header.

    Returns:
        Formatted Telegram HTML text.
    """
    event = outline.get("event", {})
    title = _html_escape(event.get("title", "Untitled"))
    start = _html_escape(_format_time_short(event.get("start", "")))
    template_name = _html_escape(outline.get("template_name", "General Meeting"))
    sections = outline.get("sections", [])
    agenda = outline.get("suggested_agenda", [])

    attendees = event.get("attendees", [])
    attendee_names = [
        _html_escape(a.get("displayName") or a.get("email", "").split("@")[0])
        for a in attendees
    ]

    lines = []

    # Confidence note if uncertain
    if confidence == "ask":
        signals = outline.get("signals", [])
        signals_str = ", ".join(signals) if signals else "context clues"
        lines.append(
            f"I think this is a <i>{template_name}</i> "
            f"(matched by: {_html_escape(signals_str)})."
        )
        lines.append("")

    # Header — concise, scannable
    participants = ", ".join(attendee_names) if attendee_names else "TBD"
    lines.append(f"<b>Prep: {title}</b>")
    lines.append(f"{start} ({participants})")
    lines.append("")

    # "Since last meeting" narrative — lead with this
    since_section = next((s for s in sections if s["name"] == "Since Last Meeting" and s["status"] == "ok"), None)
    if since_section and since_section.get("data") and "note" not in since_section.get("data", {}):
        data = since_section["data"]
        completed_count = len(data.get("tasks_completed", []))
        overdue_count = len(data.get("tasks_newly_overdue", []))
        decision_count = len(data.get("new_decisions", []))
        last_date = data.get("last_meeting_date", "")[:10]
        parts = []
        if completed_count:
            parts.append(f"{completed_count} tasks completed")
        if overdue_count:
            parts.append(f"{overdue_count} newly overdue")
        if decision_count:
            parts.append(f"{decision_count} new decisions")
        if parts:
            lines.append(f"Since {last_date}: {', '.join(parts)}")
            lines.append("")

    # Narrative summary (generated by Haiku during outline creation)
    ok_sections = [s for s in sections if s["status"] == "ok" and s.get("item_count", 0) > 0]
    unavailable = [s for s in sections if s["status"] != "ok"]

    narrative = outline.get("narrative", "")
    if narrative:
        for line in narrative.split("\n"):
            lines.append(line)
    else:
        # Fallback to data inventory if no narrative
        if ok_sections:
            summary_parts = []
            for s in ok_sections:
                count = s.get("item_count", 0)
                name = s["name"].lower()
                summary_parts.append(f"{count} {name}")
            lines.append("Data: " + ", ".join(summary_parts))
    if unavailable:
        lines.append(f"Unavailable: {', '.join(s['name'].lower() for s in unavailable)}")
    lines.append("")

    # Suggested focus — the most useful part
    if agenda:
        lines.append("<b>Suggested focus:</b>")
        for item in agenda[:4]:
            lines.append(f"  - {_html_escape(item)}")

    return "\n".join(lines)


def format_prep_approval_card(
    title: str,
    start_time: str,
    sections: list[str] | None = None,
    sensitivity: str = "founders",
) -> str:
    """
    Format a minimal approval message for a generated prep doc (HTML).

    No Drive link — document is uploaded only after approval.

    Args:
        title: Meeting title.
        start_time: Meeting start time.
        sections: List of section names included in the doc.
        sensitivity: Sensitivity level.

    Returns:
        Formatted Telegram HTML text.
    """
    t = _html_escape(title)
    time_str = _html_escape(_format_time_short(start_time))

    lines = [f"<b>Prep doc ready: {t}</b>", f"{time_str}"]

    if sections:
        lines.append(f"Sections: {', '.join(sections)}")

    if sensitivity not in ("founders", "normal"):
        lines.append(f"Sensitivity: {_html_escape(sensitivity)}")

    return "\n".join(lines)


def _format_time_short(iso_time: str) -> str:
    """Format an ISO datetime string to a short readable form."""
    if not iso_time:
        return "TBD"
    try:
        from datetime import datetime
        # Handle both datetime and date-only formats
        if "T" in iso_time:
            dt = datetime.fromisoformat(iso_time)
            return dt.strftime("%b %d, %H:%M")
        return iso_time
    except (ValueError, TypeError):
        return str(iso_time)


def format_prep_document_v2(
    event: dict,
    template: dict,
    gathered_data: list[dict],
    focus_instructions: list[str] | None = None,
    gantt_snapshot: dict | None = None,
) -> str:
    """
    Template-aware Markdown generation for prep documents.

    Uses template 'structure' to order sections. Inserts Gantt snapshot
    as formatted table. Appends focus area notes if provided.

    Args:
        event: Calendar event dict.
        template: Template dict.
        gathered_data: List of section dicts from outline.
        focus_instructions: Optional focus instructions from Eyal.
        gantt_snapshot: Optional Gantt data for table rendering.

    Returns:
        Formatted Markdown prep document.
    """
    title = event.get("title", "Untitled Meeting")
    start = event.get("start", "")
    attendees = event.get("attendees", [])
    attendee_names = [
        a.get("displayName") or a.get("email", "")
        for a in attendees
    ]
    template_name = template.get("display_name", "General Meeting")

    lines = [
        f"# Meeting Prep: {title}",
        "",
        f"**When:** {start}",
        f"**Attendees:** {', '.join(attendee_names) or 'Not specified'}",
        f"**Meeting Type:** {template_name}",
        "",
    ]

    # Focus instructions
    if focus_instructions:
        lines.append("## Focus Areas (Eyal's Input)")
        lines.append("")
        for fi in focus_instructions:
            lines.append(f"- {fi}")
        lines.append("")

    # Build sections dict for lookup by name
    data_by_name = {s["name"]: s for s in gathered_data}

    # Use template structure to order sections
    for section_name in template.get("structure", []):
        section = data_by_name.get(section_name)
        if section_name == "Suggested Agenda":
            continue  # handled separately below

        lines.append(f"## {section_name}")
        lines.append("")

        if not section or section.get("status") != "ok":
            lines.append("*Data unavailable for this section.*")
            lines.append("")
            continue

        data = section.get("data")
        if section_name == "Since Last Meeting":
            lines.append(format_since_last_meeting(data))
        else:
            _format_section_data(lines, section_name, data)
        lines.append("")

    # Gantt snapshot as table
    if gantt_snapshot:
        lines.append("## Gantt Snapshot")
        lines.append("")
        gantt_rows = format_gantt_for_document(gantt_snapshot)
        if gantt_rows:
            lines.append("| Section | Item | Status | Owner | Week |")
            lines.append("|---------|------|--------|-------|------|")
            for row in gantt_rows:
                lines.append(f"| {' | '.join(row)} |")
        lines.append("")

    # Footer
    lines.extend([
        "---",
        f"*Generated by Gianluigi on {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
    ])

    return "\n".join(lines)


def _format_section_data(lines: list[str], section_name: str, data: Any) -> None:
    """Format section data into Markdown lines based on data type."""
    if data is None:
        lines.append("*No data available.*")
        return

    if isinstance(data, dict):
        # Tasks by participant
        for key, items in data.items():
            if isinstance(items, list):
                lines.append(f"**{key}:**")
                for item in items[:10]:
                    task_title = item.get("title", item.get("task", str(item)))
                    deadline = item.get("deadline", "")
                    priority = item.get("priority", "")
                    line = f"- {task_title}"
                    if deadline:
                        line += f" (due: {deadline})"
                    if priority:
                        line += f" [{priority}]"
                    lines.append(line)
    elif isinstance(data, list):
        for item in data[:15]:
            if isinstance(item, dict):
                # Decision, question, commitment, or entity
                desc = (
                    item.get("description", "")
                    or item.get("question", "")
                    or item.get("canonical_name", "")
                    or item.get("commitment", "")
                    or str(item)
                )
                source = item.get("source_meeting", item.get("raised_by", ""))
                line = f"- {desc[:200]}"
                if source:
                    line += f" ({source})"
                lines.append(line)
            else:
                lines.append(f"- {str(item)[:200]}")
    else:
        lines.append(str(data)[:500])


def format_gantt_for_document(gantt_data: dict) -> list[list[str]]:
    """
    Convert get_gantt_section() output to table rows.

    Args:
        gantt_data: Dict from gantt_manager.get_gantt_section().

    Returns:
        List of [section, item, status, owner, week] rows.
    """
    rows = []
    try:
        section_name = gantt_data.get("section", "")
        items = gantt_data.get("items", [])
        for item in items[:20]:
            rows.append([
                section_name,
                item.get("subsection", ""),
                item.get("status", ""),
                item.get("owner", ""),
                str(item.get("week", "")),
            ])
    except Exception as e:
        logger.warning(f"Error formatting Gantt for document: {e}")
    return rows


def format_since_last_meeting(data: dict) -> str:
    """Format 'since last meeting' data for the prep document."""
    if not data or "note" in data:
        return data.get("note", "No prior meeting data available.")

    last_date = data.get("last_meeting_date", "")[:10]
    lines = [f"Changes since {last_date}:"]

    completed = data.get("tasks_completed", [])
    if completed:
        lines.append(f"\nCompleted tasks ({len(completed)}):")
        for t in completed[:5]:
            lines.append(f"  - {t.get('title', 'Untitled')} ({t.get('assignee', 'unassigned')})")

    overdue = data.get("tasks_newly_overdue", [])
    if overdue:
        lines.append(f"\nNewly overdue ({len(overdue)}):")
        for t in overdue[:5]:
            lines.append(f"  - {t.get('title', 'Untitled')} ({t.get('assignee', 'unassigned')})")

    decisions = data.get("new_decisions", [])
    if decisions:
        lines.append(f"\nNew decisions ({len(decisions)}):")
        for d in decisions[:5]:
            lines.append(f"  - {d.get('description', 'No description')}")

    fulfilled = data.get("commitments_fulfilled", [])
    if fulfilled:
        lines.append(f"\nCommitments fulfilled ({len(fulfilled)}):")
        for c in fulfilled[:5]:
            lines.append(f"  - {c.get('commitment', 'No description')}")

    if len(lines) == 1:
        lines.append("No significant changes since last meeting.")

    return "\n".join(lines)


def calculate_timeline_mode(hours_until: float) -> str:
    """
    Determine the prep timeline mode based on hours until meeting.

    Args:
        hours_until: Hours until meeting start.

    Returns:
        One of: 'normal', 'compressed', 'urgent', 'emergency', 'skip'.
    """
    if hours_until > 24:
        return "normal"
    elif hours_until > 12:
        return "compressed"
    elif hours_until > 6:
        return "urgent"
    elif hours_until > 2:
        return "emergency"
    else:
        return "skip"


async def generate_meeting_prep_from_outline(approval_id: str) -> dict:
    """
    Generate a full prep document from a stored outline.

    Loads the outline and focus instructions from pending_approvals,
    generates the full document, saves to Drive, and submits for approval.

    Args:
        approval_id: The prep_outline approval ID.

    Returns:
        Dict with status and details.
    """
    row = supabase_client.get_pending_approval(approval_id)
    if not row:
        return {"status": "error", "error": "Outline not found"}

    content = row.get("content", {})
    outline = content.get("outline", {})
    event = outline.get("event", content.get("event", {}))
    meeting_type = content.get("meeting_type", "generic")
    focus_instructions = content.get("focus_instructions", [])
    gathered_data = outline.get("sections", [])

    template = get_template(meeting_type)
    title = event.get("title", "Untitled Meeting")
    start_time = event.get("start", "")

    # Generate the full prep document
    prep_document = format_prep_document_v2(
        event=event,
        template=template,
        gathered_data=gathered_data,
        focus_instructions=focus_instructions,
    )

    # Mark outline as generated
    supabase_client.update_pending_approval(approval_id, status="generated")

    # Submit the full prep as a standard meeting_prep approval.
    # Drive upload happens AFTER Eyal approves (in distribute_approved_prep).
    from guardrails.approval_flow import submit_for_approval
    from guardrails.sensitivity_classifier import classify_sensitivity

    sensitivity = classify_sensitivity({"title": title})
    prep_approval_id = f"prep-{event.get('id', approval_id.replace('outline-', ''))}"

    await submit_for_approval(
        content_type="meeting_prep",
        content={
            "title": title,
            "summary": prep_document,
            "start_time": start_time,
            "sensitivity": sensitivity,
            "meeting_type": meeting_type,
            "focus_instructions": focus_instructions,
            "sections": gathered_data,
            "attendees": event.get("attendees", []),
        },
        meeting_id=prep_approval_id,
    )

    supabase_client.log_action(
        action="meeting_prep_generated_from_outline",
        details={
            "outline_approval_id": approval_id,
            "prep_approval_id": prep_approval_id,
            "meeting_type": meeting_type,
            "focus_count": len(focus_instructions),
        },
        triggered_by="auto",
    )

    return {
        "status": "success",
        "prep_approval_id": prep_approval_id,
    }
