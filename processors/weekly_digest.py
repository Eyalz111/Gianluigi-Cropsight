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

    # v0.3: Gather cross-reference activity for the week
    cross_ref_summary = await get_cross_reference_summary(week_start, week_end)

    # v0.3 Tier 2: Commitment scorecard
    commitment_scorecard = await get_commitment_scorecard()

    # v0.3 Tier 2: Proactive alerts snapshot
    from processors.proactive_alerts import generate_alerts
    operational_alerts = []
    try:
        operational_alerts = generate_alerts()
    except Exception as e:
        logger.error(f"Error generating alerts for digest: {e}")

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
        cross_ref_summary=cross_ref_summary,
        commitment_scorecard=commitment_scorecard,
        operational_alerts=operational_alerts,
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


async def get_cross_reference_summary(
    week_start: datetime,
    week_end: datetime,
) -> dict:
    """
    Get cross-reference activity summary for the week.

    Fetches task mentions created during the week to summarize
    deduplication, status changes, and question resolutions.

    Args:
        week_start: Start of the week (Monday).
        week_end: End of the week (Sunday).

    Returns:
        Dict with:
        - total_mentions: Total task mentions created this week.
        - status_changes: List of status changes with task titles.
        - duplicates_prevented: Count of duplicates caught.
        - questions_resolved: Count of questions resolved.
    """
    result = {
        "total_mentions": 0,
        "status_changes": [],
        "duplicates_prevented": 0,
        "questions_resolved": 0,
    }

    try:
        # Get all task mentions (we fetch recent and filter by date)
        mentions = supabase_client.get_task_mentions(limit=200)

        # Filter to this week's mentions
        week_mentions = []
        for m in mentions:
            created = m.get("created_at", "")
            if created:
                try:
                    created_dt = datetime.fromisoformat(
                        str(created).replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    if week_start <= created_dt <= week_end:
                        week_mentions.append(m)
                except (ValueError, TypeError):
                    pass

        result["total_mentions"] = len(week_mentions)

        # Count status changes vs duplicates
        for m in week_mentions:
            if m.get("implied_status"):
                task_info = m.get("tasks", {}) or {}
                result["status_changes"].append({
                    "task_title": task_info.get("title", "Unknown"),
                    "assignee": task_info.get("assignee", ""),
                    "new_status": m.get("implied_status"),
                })
            else:
                result["duplicates_prevented"] += 1

        # Count resolved questions this week
        resolved_qs = supabase_client.get_open_questions(status="resolved")
        for q in resolved_qs:
            created = q.get("created_at", "")
            if created:
                try:
                    created_dt = datetime.fromisoformat(
                        str(created).replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                    if week_start <= created_dt <= week_end:
                        result["questions_resolved"] += 1
                except (ValueError, TypeError):
                    pass

    except Exception as e:
        logger.error(f"Error fetching cross-reference summary: {e}")

    return result


async def get_commitment_scorecard() -> dict:
    """
    Build a commitment scorecard for the weekly digest.

    Summarises open commitments per speaker and recently fulfilled ones.

    Returns:
        Dict with:
        - open_by_speaker: {speaker: [commitment_text, ...]}
        - open_count: Total open commitments.
        - fulfilled_count: Total fulfilled commitments.
    """
    result = {
        "open_by_speaker": {},
        "open_count": 0,
        "fulfilled_count": 0,
    }

    try:
        open_commitments = supabase_client.get_commitments(status="open")
        result["open_count"] = len(open_commitments)

        for c in open_commitments:
            speaker = c.get("speaker", "Unknown")
            text = c.get("commitment_text", "")
            if speaker not in result["open_by_speaker"]:
                result["open_by_speaker"][speaker] = []
            result["open_by_speaker"][speaker].append(text)

        fulfilled = supabase_client.get_commitments(status="fulfilled")
        result["fulfilled_count"] = len(fulfilled)

    except Exception as e:
        logger.error(f"Error building commitment scorecard: {e}")

    return result


def format_digest_document(
    week_of: str,
    meetings: list[dict],
    decisions: list[dict],
    tasks_completed: list[dict],
    tasks_overdue: list[dict],
    tasks_upcoming: list[dict],
    open_questions: list[dict],
    upcoming_meetings: list[dict],
    cross_ref_summary: dict | None = None,
    commitment_scorecard: dict | None = None,
    operational_alerts: list[dict] | None = None,
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
        cross_ref_summary: v0.3 cross-reference activity summary (optional).
        commitment_scorecard: v0.3 Tier 2 commitment scorecard (optional).
        operational_alerts: v0.3 Tier 2 proactive alerts snapshot (optional).

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
        lines.append("| Task | Category | Assignee |")
        lines.append("|------|----------|----------|")
        for t in tasks_completed:
            title = t.get("title", "Untitled")
            category = t.get("category", "")
            assignee = t.get("assignee", "Unassigned")
            lines.append(f"| {title} | {category} | {assignee} |")
    else:
        lines.append("_No tasks completed this week._")
    lines.append("")

    # Overdue tasks
    lines.append("### Overdue")
    lines.append("")
    if tasks_overdue:
        lines.append("| Task | Category | Assignee | Deadline |")
        lines.append("|------|----------|----------|----------|")
        for t in tasks_overdue:
            title = t.get("title", "Untitled")
            category = t.get("category", "")
            assignee = t.get("assignee", "Unassigned")
            deadline = t.get("deadline", "N/A")
            if deadline and len(str(deadline)) > 10:
                deadline = str(deadline)[:10]
            lines.append(f"| {title} | {category} | {assignee} | {deadline} |")
    else:
        lines.append("_No overdue tasks._")
    lines.append("")

    # Due next week
    lines.append("### Due Next Week")
    lines.append("")
    if tasks_upcoming:
        lines.append("| Task | Category | Assignee | Deadline | Priority |")
        lines.append("|------|----------|----------|----------|----------|")
        for t in tasks_upcoming:
            title = t.get("title", "Untitled")
            category = t.get("category", "")
            assignee = t.get("assignee", "Unassigned")
            deadline = t.get("deadline", "N/A")
            if deadline and len(str(deadline)) > 10:
                deadline = str(deadline)[:10]
            priority = t.get("priority", "M")
            lines.append(f"| {title} | {category} | {assignee} | {deadline} | {priority} |")
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

    # ---- Commitment Scorecard (v0.3 Tier 2) ----
    if commitment_scorecard:
        open_count = commitment_scorecard.get("open_count", 0)
        fulfilled_count = commitment_scorecard.get("fulfilled_count", 0)
        open_by_speaker = commitment_scorecard.get("open_by_speaker", {})

        if open_count > 0 or fulfilled_count > 0:
            lines.append("## Commitment Scorecard")
            lines.append("")
            lines.append(
                f"**{open_count}** open commitment(s) | "
                f"**{fulfilled_count}** fulfilled"
            )
            lines.append("")

            if open_by_speaker:
                lines.append("### Outstanding Commitments by Speaker")
                lines.append("")
                for speaker, texts in sorted(open_by_speaker.items()):
                    lines.append(f"**{speaker}** ({len(texts)}):")
                    for text in texts[:5]:
                        lines.append(f"  - {text}")
                    if len(texts) > 5:
                        lines.append(f"  - ... and {len(texts) - 5} more")
                lines.append("")

    # ---- Cross-Meeting Intelligence (v0.3) ----
    if cross_ref_summary:
        total = cross_ref_summary.get("total_mentions", 0)
        dedup_count = cross_ref_summary.get("duplicates_prevented", 0)
        status_changes = cross_ref_summary.get("status_changes", [])
        questions_resolved = cross_ref_summary.get("questions_resolved", 0)

        if total > 0 or questions_resolved > 0:
            lines.append("## Cross-Meeting Intelligence This Week")
            lines.append("")

            if dedup_count > 0:
                lines.append(
                    f"- **{dedup_count} task(s)** automatically deduplicated "
                    f"(prevented duplicates)"
                )

            if status_changes:
                lines.append(
                    f"- **{len(status_changes)} task status change(s)** "
                    f"inferred and approved"
                )
                for sc in status_changes:
                    title = sc.get("task_title", "Unknown")
                    assignee = sc.get("assignee", "")
                    new_status = sc.get("new_status", "")
                    assignee_str = f" ({assignee})" if assignee else ""
                    lines.append(
                        f"  - \"{title}\"{assignee_str}: -> {new_status}"
                    )

            if questions_resolved > 0:
                lines.append(
                    f"- **{questions_resolved} open question(s)** resolved"
                )

            lines.append("")

    # ---- Operational Alerts (v0.3 Tier 2) ----
    if operational_alerts:
        lines.append("## Operational Alerts")
        lines.append("")

        severity_label = {"high": "!!!", "medium": "!!", "low": "!"}
        for alert in operational_alerts:
            sev = alert.get("severity", "low")
            label = severity_label.get(sev, "!")
            lines.append(f"- {label} {alert.get('title', '')}")
        lines.append("")

    # ---- Footer ----
    lines.append("---")
    lines.append(
        "_This digest was generated by Gianluigi, "
        "CropSight's AI Operations Assistant._"
    )

    return "\n".join(lines)
