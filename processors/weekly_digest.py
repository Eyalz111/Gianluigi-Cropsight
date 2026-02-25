"""
Weekly digest generator.

This module compiles a weekly summary including:
- Meetings held this week with key decisions
- Tasks completed, overdue, and due next week
- Open questions still unresolved
- Upcoming meetings next week

Usage:
    from processors.weekly_digest import generate_weekly_digest

    digest = await generate_weekly_digest()
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from services.supabase_client import supabase_client
from services.google_calendar import calendar_service
from guardrails.calendar_filter import is_cropsight_meeting

logger = logging.getLogger(__name__)


async def generate_weekly_digest(
    week_start: datetime | None = None
) -> dict:
    """
    Generate a weekly digest document.

    Orchestrates all sub-functions to build a complete weekly summary.

    Args:
        week_start: Start of the week (Monday) to summarize.
                    Defaults to the current week's Monday.

    Returns:
        Dict containing:
        - week_of: Week identifier string (YYYY-MM-DD of Monday)
        - digest_document: Formatted digest (Markdown)
        - meetings_count: Number of meetings this week
        - decisions_count: Number of decisions made
        - tasks_completed: Number of tasks completed
        - tasks_overdue: Number of overdue tasks
    """
    # 1. Determine week boundaries (Monday 00:00 to Sunday 23:59)
    if week_start is None:
        now = datetime.now()
        week_start = now - timedelta(days=now.weekday())
    # Normalise to midnight
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    week_of = week_start.strftime("%Y-%m-%d")

    logger.info(f"Generating weekly digest for week of {week_of}")

    # 2. Gather data from all sources
    meetings = await get_meetings_for_week(week_start, week_end)
    decisions = await get_decisions_for_week(week_start, week_end)
    task_summary = await get_task_summary()
    open_questions = await get_open_questions_summary()
    upcoming = await get_upcoming_meetings(days=7)

    # 3. Format the digest document
    digest_document = format_digest_document(
        week_of=week_of,
        meetings=meetings,
        decisions=decisions,
        tasks_completed=task_summary.get("completed_this_week", []),
        tasks_overdue=task_summary.get("overdue", []),
        tasks_upcoming=task_summary.get("due_next_week", []),
        open_questions=open_questions,
        upcoming_meetings=upcoming,
    )

    # 4. Build result dict with counts
    result = {
        "week_of": week_of,
        "digest_document": digest_document,
        "meetings_count": len(meetings),
        "decisions_count": len(decisions),
        "tasks_completed": len(task_summary.get("completed_this_week", [])),
        "tasks_overdue": len(task_summary.get("overdue", [])),
    }

    logger.info(
        f"Digest generated: {result['meetings_count']} meetings, "
        f"{result['decisions_count']} decisions, "
        f"{result['tasks_completed']} tasks completed"
    )
    return result


async def get_meetings_for_week(
    week_start: datetime,
    week_end: datetime
) -> list[dict]:
    """
    Get all meetings held during a week.

    Calls supabase_client.list_meetings with date range filters.
    Note: supabase_client methods are SYNC (never await).

    Args:
        week_start: Start of the week (Monday).
        week_end: End of the week (Sunday).

    Returns:
        List of meeting summaries.
    """
    try:
        meetings = supabase_client.list_meetings(
            date_from=week_start,
            date_to=week_end,
        )
        logger.info(f"Found {len(meetings)} meetings for week")
        return meetings
    except Exception as e:
        logger.error(f"Error fetching meetings for week: {e}")
        return []


async def get_decisions_for_week(
    week_start: datetime,
    week_end: datetime
) -> list[dict]:
    """
    Get all decisions made during a week.

    Gets meetings for the week, then fetches decisions for each meeting.
    Note: supabase_client methods are SYNC (never await).

    Args:
        week_start: Start of the week.
        week_end: End of the week.

    Returns:
        List of decisions with meeting context.
    """
    try:
        meetings = await get_meetings_for_week(week_start, week_end)
        all_decisions = []

        for meeting in meetings:
            meeting_id = meeting.get("id")
            if not meeting_id:
                continue
            decisions = supabase_client.list_decisions(meeting_id=meeting_id)
            # Attach meeting title to each decision for context
            for d in decisions:
                d["_meeting_title"] = meeting.get("title", "Unknown")
                d["_meeting_date"] = meeting.get("date", "")
            all_decisions.extend(decisions)

        logger.info(f"Found {len(all_decisions)} decisions for week")
        return all_decisions
    except Exception as e:
        logger.error(f"Error fetching decisions for week: {e}")
        return []


async def get_task_summary() -> dict:
    """
    Get summary of task statuses.

    Categorises tasks into three buckets:
    - completed_this_week: tasks with status="done"
    - overdue: tasks with status="overdue" or pending past deadline
    - due_next_week: tasks due in the next 7 days

    Note: supabase_client methods are SYNC (never await).

    Returns:
        Dict with completed_this_week, overdue, and due_next_week lists.
    """
    now = datetime.now()
    next_week_end = now + timedelta(days=7)

    result = {
        "completed_this_week": [],
        "overdue": [],
        "due_next_week": [],
    }

    try:
        # Get completed tasks (status="done")
        done_tasks = supabase_client.get_tasks(status="done")
        result["completed_this_week"] = done_tasks

        # Get overdue tasks
        overdue_tasks = supabase_client.get_tasks(status="overdue")
        result["overdue"] = overdue_tasks

        # Get tasks due in next 7 days (pending tasks, filter by deadline)
        pending_tasks = supabase_client.get_tasks(status="pending")
        due_next_week = []
        for task in pending_tasks:
            deadline_str = task.get("deadline")
            if deadline_str:
                try:
                    # Parse deadline — might be date string or datetime string
                    deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
                    # Strip timezone for comparison with naive datetime
                    deadline_naive = deadline.replace(tzinfo=None)
                    if now <= deadline_naive <= next_week_end:
                        due_next_week.append(task)
                except (ValueError, AttributeError):
                    pass
        result["due_next_week"] = due_next_week

        logger.info(
            f"Task summary: {len(result['completed_this_week'])} done, "
            f"{len(result['overdue'])} overdue, "
            f"{len(result['due_next_week'])} due next week"
        )
    except Exception as e:
        logger.error(f"Error fetching task summary: {e}")

    return result


async def get_open_questions_summary() -> list[dict]:
    """
    Get open questions that are still unresolved.

    Note: supabase_client methods are SYNC (never await).

    Returns:
        List of open questions with source meeting info.
    """
    try:
        questions = supabase_client.get_open_questions(status="open")
        logger.info(f"Found {len(questions)} open questions")
        return questions
    except Exception as e:
        logger.error(f"Error fetching open questions: {e}")
        return []


async def get_upcoming_meetings(days: int = 7) -> list[dict]:
    """
    Get upcoming meetings for the next week.

    Uses calendar_service (async) and filters with is_cropsight_meeting.

    Args:
        days: Number of days to look ahead.

    Returns:
        List of upcoming CropSight calendar events.
    """
    try:
        events = await calendar_service.get_upcoming_events(days=days)

        # Filter to CropSight meetings only
        cropsight_events = [
            e for e in events
            if is_cropsight_meeting(e) is True
        ]

        logger.info(
            f"Found {len(cropsight_events)} upcoming CropSight meetings "
            f"(out of {len(events)} total)"
        )
        return cropsight_events
    except Exception as e:
        logger.error(f"Error fetching upcoming meetings: {e}")
        return []


def format_digest_document(
    week_of: str,
    meetings: list[dict],
    decisions: list[dict],
    tasks_completed: list[dict],
    tasks_overdue: list[dict],
    tasks_upcoming: list[dict],
    open_questions: list[dict],
    upcoming_meetings: list[dict]
) -> str:
    """
    Format all digest information into a Markdown document.

    Builds a structured weekly digest with sections for meetings,
    decisions, tasks, open questions, and upcoming meetings.

    Args:
        week_of: Week identifier (e.g., "2026-02-17").
        meetings: Meetings held this week.
        decisions: Decisions made this week.
        tasks_completed: Tasks completed this week.
        tasks_overdue: Currently overdue tasks.
        tasks_upcoming: Tasks due next week.
        open_questions: Unresolved questions.
        upcoming_meetings: Meetings scheduled for next week.

    Returns:
        Formatted Markdown digest document.
    """
    lines = []

    # ---- Header ----
    lines.append(f"# CropSight Weekly Digest — Week of {week_of}")
    lines.append("")
    lines.append(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # ---- Meetings This Week ----
    lines.append("## Meetings This Week")
    lines.append("")
    if meetings:
        for m in meetings:
            title = m.get("title", "Untitled")
            date = m.get("date", "")[:10] if m.get("date") else "N/A"
            # Count decisions for this meeting
            meeting_decisions = [
                d for d in decisions
                if d.get("meeting_id") == m.get("id")
            ]
            lines.append(
                f"- **{title}** ({date}) — "
                f"{len(meeting_decisions)} decision(s)"
            )
    else:
        lines.append("_No meetings this week._")
    lines.append("")

    # ---- Key Decisions Made ----
    lines.append("## Key Decisions Made")
    lines.append("")
    if decisions:
        for i, d in enumerate(decisions, 1):
            desc = d.get("description", "N/A")
            meeting_title = d.get("_meeting_title") or ""
            # Fall back to joined meetings data if _meeting_title not set
            if not meeting_title and d.get("meetings"):
                meeting_title = d["meetings"].get("title", "")
            source = f" _(from: {meeting_title})_" if meeting_title else ""
            lines.append(f"{i}. {desc}{source}")
    else:
        lines.append("_No decisions recorded this week._")
    lines.append("")

    # ---- Task Status ----
    lines.append("## Task Status")
    lines.append("")

    # Completed tasks
    lines.append("### Completed")
    lines.append("")
    if tasks_completed:
        lines.append("| Task | Assignee |")
        lines.append("|------|----------|")
        for t in tasks_completed:
            title = t.get("title", "Untitled")
            assignee = t.get("assignee", "Unassigned")
            lines.append(f"| {title} | {assignee} |")
    else:
        lines.append("_No tasks completed this week._")
    lines.append("")

    # Overdue tasks
    lines.append("### Overdue")
    lines.append("")
    if tasks_overdue:
        lines.append("| Task | Assignee | Deadline |")
        lines.append("|------|----------|----------|")
        for t in tasks_overdue:
            title = t.get("title", "Untitled")
            assignee = t.get("assignee", "Unassigned")
            deadline = t.get("deadline", "N/A")
            if deadline and len(str(deadline)) > 10:
                deadline = str(deadline)[:10]
            lines.append(f"| {title} | {assignee} | {deadline} |")
    else:
        lines.append("_No overdue tasks._")
    lines.append("")

    # Due next week
    lines.append("### Due Next Week")
    lines.append("")
    if tasks_upcoming:
        lines.append("| Task | Assignee | Deadline | Priority |")
        lines.append("|------|----------|----------|----------|")
        for t in tasks_upcoming:
            title = t.get("title", "Untitled")
            assignee = t.get("assignee", "Unassigned")
            deadline = t.get("deadline", "N/A")
            if deadline and len(str(deadline)) > 10:
                deadline = str(deadline)[:10]
            priority = t.get("priority", "M")
            lines.append(f"| {title} | {assignee} | {deadline} | {priority} |")
    else:
        lines.append("_No tasks due next week._")
    lines.append("")

    # ---- Open Questions ----
    lines.append("## Open Questions")
    lines.append("")
    if open_questions:
        for i, q in enumerate(open_questions, 1):
            question = q.get("question", "N/A")
            raised_by = q.get("raised_by", "Unknown")
            lines.append(f"{i}. {question} _(raised by {raised_by})_")
    else:
        lines.append("_No open questions._")
    lines.append("")

    # ---- Upcoming Meetings Next Week ----
    lines.append("## Upcoming Meetings Next Week")
    lines.append("")
    if upcoming_meetings:
        for m in upcoming_meetings:
            title = m.get("title", "Untitled")
            start = m.get("start", "TBD")
            # Show attendee count if available
            attendees = m.get("attendees", [])
            attendee_str = f" ({len(attendees)} attendees)" if attendees else ""
            lines.append(f"- **{title}** — {start}{attendee_str}")
    else:
        lines.append("_No upcoming CropSight meetings._")
    lines.append("")

    # ---- Footer ----
    lines.append("---")
    lines.append(
        "_This digest was generated by Gianluigi, "
        "CropSight's AI Operations Assistant._"
    )

    return "\n".join(lines)
