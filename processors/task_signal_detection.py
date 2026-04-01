"""
Task signal detection — Phase 12 A5.

Detects signals of task completion/progress from external sources:
- Email: email content that references open tasks
- Gantt: status changes in the Gantt chart
- Calendar: events that match task topics

All detection is data collection only — signals are recorded but
never auto-applied. They inform the weekly review and future A3 dedup.

Usage:
    from processors.task_signal_detection import (
        detect_email_task_signals,
        detect_gantt_task_signals,
        detect_calendar_task_signals,
    )
"""

import logging
from typing import Any

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Keywords that suggest task completion
_COMPLETION_KEYWORDS = [
    "completed", "done", "finished", "delivered", "shipped",
    "signed", "approved", "resolved", "closed",
]

# Keywords that suggest impediment
_IMPEDIMENT_KEYWORDS = [
    "blocked", "stuck", "waiting", "delayed", "postponed",
    "on hold", "cancelled",
]


def detect_email_task_signals(
    extracted_items: list[dict],
    msg_id: str,
    sender_email: str,
    subject: str,
) -> list[dict]:
    """
    Check if email-extracted items reference open tasks and record signals.

    Args:
        extracted_items: Items extracted from email intelligence.
        msg_id: Gmail message ID.
        sender_email: Sender email address.
        subject: Email subject line.

    Returns:
        List of created signal records.
    """
    if not extracted_items:
        return []

    # Fetch open tasks for matching
    try:
        pending = supabase_client.get_tasks(status="pending", limit=50)
        in_progress = supabase_client.get_tasks(status="in_progress", limit=50)
        open_tasks = pending + in_progress
    except Exception as e:
        logger.debug(f"Could not fetch tasks for email signal detection: {e}")
        return []

    if not open_tasks:
        return []

    signals = []

    for item in extracted_items:
        item_text = (item.get("text") or "").lower()
        item_type = item.get("type", "")

        if not item_text:
            continue

        # Try to match against open tasks
        for task in open_tasks:
            task_title = (task.get("title") or "").lower()
            task_id = task.get("id")

            if not task_title or not task_id:
                continue

            # Simple keyword overlap matching (conservative)
            title_words = set(task_title.split())
            item_words = set(item_text.split())
            # Require at least 3 overlapping significant words
            overlap = title_words & item_words - {"the", "a", "an", "to", "for", "and", "or", "of", "in", "on", "is", "it", "we"}
            if len(overlap) < 3:
                continue

            # Determine signal type
            signal_type = "mention"
            if any(kw in item_text for kw in _COMPLETION_KEYWORDS):
                signal_type = "completion"
            elif any(kw in item_text for kw in _IMPEDIMENT_KEYWORDS):
                signal_type = "impediment"

            try:
                record = supabase_client.create_task_signal(
                    task_id=task_id,
                    signal_type=signal_type,
                    signal_source="email",
                    confidence="low",  # Email signals are always low confidence
                    details={
                        "msg_id": msg_id,
                        "sender": sender_email,
                        "subject": subject,
                        "matched_text": item_text[:200],
                    },
                )
                if record:
                    signals.append(record)
            except Exception as e:
                logger.debug(f"Failed to create email task signal: {e}")

    if signals:
        logger.info(f"Detected {len(signals)} task signals from email {msg_id}")

    return signals


def detect_gantt_task_signals(
    changes: list[dict],
    proposal_id: str | None = None,
) -> list[dict]:
    """
    Detect task signals from Gantt chart status changes.

    Args:
        changes: List of Gantt change dicts with old_value/new_value.
        proposal_id: UUID of the approved proposal (if any).

    Returns:
        List of created signal records.
    """
    if not changes:
        return []

    # Fetch open tasks
    try:
        pending = supabase_client.get_tasks(status="pending", limit=50)
        in_progress = supabase_client.get_tasks(status="in_progress", limit=50)
        open_tasks = pending + in_progress
    except Exception as e:
        logger.debug(f"Could not fetch tasks for Gantt signal detection: {e}")
        return []

    if not open_tasks:
        return []

    signals = []

    for change in changes:
        old_val = str(change.get("old_value", "")).lower()
        new_val = str(change.get("new_value", "")).lower()
        section = change.get("section", "")
        subsection = change.get("subsection", "")

        # Determine if this is a meaningful status transition
        signal_type = None
        if "completed" in new_val and "completed" not in old_val:
            signal_type = "completion"
        elif "blocked" in new_val and "blocked" not in old_val:
            signal_type = "impediment"
        elif "active" in new_val and "planned" in old_val:
            signal_type = "progress"

        if not signal_type:
            continue

        # Try to match against open tasks by section/subsection keywords
        context = f"{section} {subsection}".lower()
        for task in open_tasks:
            task_title = (task.get("title") or "").lower()
            task_category = (task.get("category") or "").lower()
            task_id = task.get("id")

            if not task_id:
                continue

            # Match by category alignment or keyword overlap
            category_match = (
                ("product" in context and "product" in task_category) or
                ("bd" in context and ("bd" in task_category or "sales" in task_category)) or
                ("legal" in context and "legal" in task_category) or
                ("finance" in context and "finance" in task_category)
            )

            title_words = set(task_title.split())
            context_words = set(context.split())
            keyword_overlap = len(title_words & context_words - {"the", "a", "and", "or", "of"}) >= 2

            if not (category_match or keyword_overlap):
                continue

            try:
                record = supabase_client.create_task_signal(
                    task_id=task_id,
                    signal_type=signal_type,
                    signal_source="gantt",
                    confidence="medium",
                    details={
                        "proposal_id": proposal_id,
                        "section": section,
                        "subsection": subsection,
                        "old_value": old_val[:100],
                        "new_value": new_val[:100],
                    },
                )
                if record:
                    signals.append(record)
            except Exception as e:
                logger.debug(f"Failed to create Gantt task signal: {e}")

    if signals:
        logger.info(f"Detected {len(signals)} task signals from Gantt changes")

    return signals


def detect_calendar_task_signals(
    events: list[dict],
) -> list[dict]:
    """
    Detect task signals from calendar events.

    Checks if event descriptions/titles suggest task completion or progress.

    Args:
        events: List of parsed calendar event dicts.

    Returns:
        List of created signal records.
    """
    if not events:
        return []

    # Fetch open tasks
    try:
        pending = supabase_client.get_tasks(status="pending", limit=50)
        in_progress = supabase_client.get_tasks(status="in_progress", limit=50)
        open_tasks = pending + in_progress
    except Exception as e:
        logger.debug(f"Could not fetch tasks for calendar signal detection: {e}")
        return []

    if not open_tasks:
        return []

    signals = []

    for event in events:
        event_title = (event.get("title") or "").lower()
        event_desc = (event.get("description") or "").lower()
        event_id = event.get("id", "")
        event_text = f"{event_title} {event_desc}"

        for task in open_tasks:
            task_title = (task.get("title") or "").lower()
            task_id = task.get("id")

            if not task_id or not task_title:
                continue

            # Check if event title/description references the task
            title_words = set(task_title.split())
            event_words = set(event_text.split())
            overlap = title_words & event_words - {"the", "a", "an", "to", "for", "and", "or", "of", "in", "on", "is", "it", "we", "meeting"}

            if len(overlap) < 3:
                continue

            # Determine signal type from description
            signal_type = "mention"
            if any(kw in event_desc for kw in _COMPLETION_KEYWORDS):
                signal_type = "completion"

            try:
                record = supabase_client.create_task_signal(
                    task_id=task_id,
                    signal_type=signal_type,
                    signal_source="calendar",
                    confidence="low",
                    details={
                        "event_id": event_id,
                        "event_title": event.get("title", ""),
                    },
                )
                if record:
                    signals.append(record)
            except Exception as e:
                logger.debug(f"Failed to create calendar task signal: {e}")

    if signals:
        logger.info(f"Detected {len(signals)} task signals from calendar events")

    return signals
