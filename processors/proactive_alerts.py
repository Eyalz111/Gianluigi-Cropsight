"""
Proactive alerts processor for v0.3 Tier 2.

Generates operational alerts based on SQL-driven pattern detection:
1. Overdue task clusters — Assignee has 3+ overdue tasks
2. Stale commitments — Open commitments not mentioned in 2+ weeks
3. Recurring discussions — Entity mentioned in 3+ meetings
4. Open question pileup — 5+ unresolved questions

No LLM calls — pure SQL aggregations for speed and cost.

Usage:
    from processors.proactive_alerts import generate_alerts, generate_post_meeting_alerts

    alerts = generate_alerts()
    post_alerts = generate_post_meeting_alerts(meeting_id, transcript)
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


def generate_alerts() -> list[dict]:
    """
    Run all alert pattern detectors and return combined alerts.

    Each alert has:
    - type: overdue_cluster | stale_commitment | recurring_discussion | question_pileup
    - severity: high | medium | low
    - title: Short summary
    - details: Longer explanation
    - items: Related items (tasks, commitments, entities, questions)

    Returns:
        List of alert dicts sorted by severity (high first).
    """
    alerts = []

    try:
        alerts.extend(_check_overdue_clusters())
    except Exception as e:
        logger.error(f"Error checking overdue clusters: {e}")

    try:
        alerts.extend(_check_stale_commitments())
    except Exception as e:
        logger.error(f"Error checking stale commitments: {e}")

    try:
        alerts.extend(_check_recurring_discussions())
    except Exception as e:
        logger.error(f"Error checking recurring discussions: {e}")

    try:
        alerts.extend(_check_question_pileup())
    except Exception as e:
        logger.error(f"Error checking question pileup: {e}")

    # Sort by severity: high > medium > low
    severity_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: severity_order.get(a.get("severity", "low"), 3))

    logger.info(f"Generated {len(alerts)} proactive alerts")
    return alerts


def generate_post_meeting_alerts(
    meeting_id: str,
    transcript: str,
) -> list[dict]:
    """
    Generate alerts triggered by a new meeting being processed.

    Checks if any entity from this meeting now appears in 3+ meetings total.

    Args:
        meeting_id: UUID of the just-processed meeting.
        transcript: Transcript text (currently unused, reserved for future patterns).

    Returns:
        List of alert dicts.
    """
    alerts = []

    try:
        # Check entity mentions for this meeting
        mentions = supabase_client.get_entity_mentions(meeting_id=meeting_id)

        # Group by entity
        entity_ids = set()
        for m in mentions:
            eid = m.get("entity_id")
            if eid:
                entity_ids.add(eid)

        # Check each entity's total mention count
        for eid in entity_ids:
            all_mentions = supabase_client.get_entity_mentions(entity_id=eid)
            # Count unique meetings
            meeting_ids = set(m.get("meeting_id") for m in all_mentions if m.get("meeting_id"))
            if len(meeting_ids) >= settings.ALERT_RECURRING_DISCUSSION_MEETINGS:
                entity_info = all_mentions[0].get("entities", {}) or {}
                entity_name = entity_info.get("canonical_name", "Unknown")
                alerts.append({
                    "type": "recurring_discussion",
                    "severity": "low",
                    "title": f"Recurring: {entity_name} discussed in {len(meeting_ids)} meetings",
                    "details": (
                        f"{entity_name} has been discussed across {len(meeting_ids)} meetings. "
                        f"Consider whether a dedicated follow-up or decision is needed."
                    ),
                    "items": [{"entity_id": eid, "entity_name": entity_name, "meeting_count": len(meeting_ids)}],
                })

    except Exception as e:
        logger.error(f"Error generating post-meeting alerts: {e}")

    return alerts


def format_alerts_message(alerts: list[dict]) -> str:
    """
    Format alerts into a Telegram-friendly message.

    Groups alerts by severity level.

    Args:
        alerts: List of alert dicts.

    Returns:
        Formatted message string.
    """
    if not alerts:
        return ""

    lines = ["*Operational Alerts*\n"]

    # Group by severity
    high = [a for a in alerts if a.get("severity") == "high"]
    medium = [a for a in alerts if a.get("severity") == "medium"]
    low = [a for a in alerts if a.get("severity") == "low"]

    if high:
        lines.append("*!!! HIGH PRIORITY*")
        for a in high:
            lines.append(f"  - {a.get('title', '')}")
            if a.get("details"):
                lines.append(f"    {a['details'][:100]}")
        lines.append("")

    if medium:
        lines.append("*!! MEDIUM*")
        for a in medium:
            lines.append(f"  - {a.get('title', '')}")
        lines.append("")

    if low:
        lines.append("*! LOW*")
        for a in low:
            lines.append(f"  - {a.get('title', '')}")
        lines.append("")

    return "\n".join(lines)


# =========================================================================
# Internal Pattern Detectors
# =========================================================================

def _check_overdue_clusters() -> list[dict]:
    """
    Detect assignees with 3+ overdue tasks.

    Returns:
        List of alerts for overdue task clusters.
    """
    alerts = []
    overdue_tasks = supabase_client.get_tasks(status="overdue")

    if not overdue_tasks:
        return alerts

    # Group by assignee
    by_assignee: dict[str, list] = {}
    for task in overdue_tasks:
        assignee = task.get("assignee", "Unknown")
        if assignee not in by_assignee:
            by_assignee[assignee] = []
        by_assignee[assignee].append(task)

    # Alert if 3+ overdue for one assignee
    for assignee, tasks in by_assignee.items():
        if len(tasks) >= settings.ALERT_OVERDUE_CLUSTER_MIN:
            task_titles = [t.get("title", "?")[:50] for t in tasks[:5]]
            alerts.append({
                "type": "overdue_cluster",
                "severity": "high",
                "title": f"{assignee} has {len(tasks)} overdue tasks",
                "details": (
                    f"{assignee} has {len(tasks)} overdue tasks. "
                    f"This may indicate capacity issues or blocked work."
                ),
                "items": [
                    {"assignee": assignee, "count": len(tasks), "tasks": task_titles}
                ],
            })

    return alerts


def _check_stale_commitments() -> list[dict]:
    """
    Detect open commitments older than 2 weeks.

    Returns:
        List of alerts for stale commitments.
    """
    alerts = []
    open_commitments = supabase_client.get_commitments(status="open")

    if not open_commitments:
        return alerts

    two_weeks_ago = datetime.now() - timedelta(days=settings.ALERT_STALE_COMMITMENT_DAYS)
    stale = []

    for c in open_commitments:
        created_str = c.get("created_at", "")
        if created_str:
            try:
                created = datetime.fromisoformat(
                    str(created_str).replace("Z", "+00:00")
                ).replace(tzinfo=None)
                if created < two_weeks_ago:
                    stale.append(c)
            except (ValueError, TypeError):
                pass

    if stale:
        items = []
        for c in stale[:5]:
            items.append({
                "speaker": c.get("speaker", "?"),
                "commitment": c.get("commitment_text", "?")[:80],
            })
        alerts.append({
            "type": "stale_commitment",
            "severity": "medium",
            "title": f"{len(stale)} commitment(s) older than 2 weeks",
            "details": (
                f"There are {len(stale)} open commitments that haven't been "
                f"addressed in over 2 weeks. Consider following up."
            ),
            "items": items,
        })

    return alerts


def _check_recurring_discussions() -> list[dict]:
    """
    Detect entities discussed in 3+ meetings.

    Returns:
        List of alerts for recurring discussions.
    """
    alerts = []

    try:
        entities = supabase_client.list_entities(limit=200)
    except Exception:
        return alerts

    for entity in entities:
        eid = entity.get("id")
        if not eid:
            continue

        try:
            mentions = supabase_client.get_entity_mentions(entity_id=eid)
            meeting_ids = set(m.get("meeting_id") for m in mentions if m.get("meeting_id"))

            if len(meeting_ids) >= settings.ALERT_RECURRING_DISCUSSION_MEETINGS:
                name = entity.get("canonical_name", "Unknown")
                alerts.append({
                    "type": "recurring_discussion",
                    "severity": "low",
                    "title": f"{name} discussed in {len(meeting_ids)} meetings",
                    "details": (
                        f"{name} has come up across {len(meeting_ids)} meetings. "
                        f"If there's no clear resolution or decision, consider scheduling "
                        f"a dedicated discussion."
                    ),
                    "items": [{"entity_name": name, "meeting_count": len(meeting_ids)}],
                })
        except Exception:
            continue

    return alerts


def _check_question_pileup() -> list[dict]:
    """
    Detect when there are 5+ unresolved open questions.

    Returns:
        List of alerts for question pileup.
    """
    alerts = []
    open_questions = supabase_client.get_open_questions(status="open")

    if len(open_questions) >= settings.ALERT_QUESTION_PILEUP_MIN:
        q_summaries = []
        for q in open_questions[:5]:
            q_summaries.append({
                "question": q.get("question", "?")[:80],
                "raised_by": q.get("raised_by", "?"),
            })
        alerts.append({
            "type": "question_pileup",
            "severity": "medium",
            "title": f"{len(open_questions)} unresolved open questions",
            "details": (
                f"There are {len(open_questions)} open questions that haven't been "
                f"resolved. Consider dedicating meeting time to address them."
            ),
            "items": q_summaries,
        })

    return alerts
