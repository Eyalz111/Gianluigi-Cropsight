"""
Weekly review data compilation.

Compiles structured data for the 5 data sections of the weekly review:
1. Week in Review — stats, meetings, decisions, tasks
2. Gantt Proposals — pending proposals with summaries
3. Attention Needed — alerts, stale tasks, approaching milestones
4. Next Week Preview — calendar, deadlines, priorities
5. Horizon Check — strategic milestones, red flags

These map to the full 7-part V1_DESIGN agenda for Phase 7 MCP,
but Telegram presents them as 3 condensed parts (Sub-Phase 6.2).
"""

import logging
from datetime import datetime, timedelta

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


async def compile_weekly_review_data(
    week_number: int,
    year: int,
    week_start: datetime | None = None,
) -> dict:
    """
    Master orchestrator — compile all weekly review data.

    Args:
        week_number: ISO week number.
        year: Year.
        week_start: Optional explicit week start (Monday).
                    Defaults to current week's Monday.

    Returns:
        Structured dict with keys: week_in_review, gantt_proposals,
        attention_needed, next_week_preview, horizon_check, meta.
    """
    if week_start is None:
        now = datetime.now()
        week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)

    data = {
        "week_in_review": {},
        "gantt_proposals": {},
        "attention_needed": {},
        "next_week_preview": {},
        "horizon_check": {},
        "meta": {
            "week_number": week_number,
            "year": year,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "compiled_at": datetime.utcnow().isoformat(),
        },
    }

    # Compile each section independently — one failure doesn't block others
    data["week_in_review"] = await _compile_week_in_review(week_start, week_end)
    data["gantt_proposals"] = await _compile_gantt_proposals()
    data["attention_needed"] = await _compile_attention_needed()
    data["next_week_preview"] = await _compile_next_week_preview()
    data["horizon_check"] = await _compile_horizon_check()

    logger.info(
        f"Weekly review data compiled for W{week_number}/{year}: "
        f"{data['week_in_review'].get('meetings_count', 0)} meetings, "
        f"{len(data['gantt_proposals'].get('proposals', []))} Gantt proposals"
    )
    return data


# =========================================================================
# Per-Section Compilers
# =========================================================================

async def _compile_week_in_review(
    week_start: datetime,
    week_end: datetime,
) -> dict:
    """Compile week stats: meetings, decisions, tasks, activity counts."""
    result = {
        "meetings": [],
        "meetings_count": 0,
        "decisions": [],
        "decisions_count": 0,
        "task_summary": {},
        "cross_reference_summary": {},
        "debrief_count": 0,
        "email_scan_count": 0,
        "meeting_cadence": {},
    }

    # Meetings
    try:
        from processors.weekly_digest import get_meetings_for_week
        meetings = await get_meetings_for_week(week_start, week_end)
        result["meetings"] = meetings
        result["meetings_count"] = len(meetings)
    except Exception as e:
        logger.error(f"Week-in-review meetings failed: {e}")

    # Decisions
    try:
        from processors.weekly_digest import get_decisions_for_week
        decisions = await get_decisions_for_week(week_start, week_end)
        result["decisions"] = decisions
        result["decisions_count"] = len(decisions)
    except Exception as e:
        logger.error(f"Week-in-review decisions failed: {e}")

    # Task summary
    try:
        from processors.weekly_digest import get_task_summary
        result["task_summary"] = await get_task_summary()
    except Exception as e:
        logger.error(f"Week-in-review task summary failed: {e}")

    # DEPRECATED: Commitment scorecard removed — commitments merged into tasks.
    # Previously: result["commitment_scorecard"] = await get_commitment_scorecard()

    # Cross-reference summary
    try:
        from processors.weekly_digest import get_cross_reference_summary
        result["cross_reference_summary"] = await get_cross_reference_summary(
            week_start, week_end
        )
    except Exception as e:
        logger.error(f"Week-in-review cross-reference failed: {e}")

    # Meeting cadence (expected vs actual)
    try:
        from services.gantt_manager import gantt_manager
        result["meeting_cadence"] = await gantt_manager.get_meeting_cadence()
    except Exception as e:
        logger.debug(f"Meeting cadence failed (Gantt may not be set up): {e}")

    # Debrief count
    try:
        week_start_str = week_start.strftime("%Y-%m-%d")
        week_end_str = week_end.strftime("%Y-%m-%d")
        debriefs = supabase_client.get_debrief_sessions_for_week(
            week_start_str, week_end_str
        )
        result["debrief_count"] = len([
            d for d in debriefs if d.get("status") == "approved"
        ])
    except Exception as e:
        logger.debug(f"Debrief count failed: {e}")

    # Email scan count
    try:
        week_start_str = week_start.strftime("%Y-%m-%d")
        week_end_str = week_end.strftime("%Y-%m-%d")
        scans = supabase_client.get_email_scans_for_week(
            week_start_str, week_end_str
        )
        result["email_scan_count"] = len(scans)
    except Exception as e:
        logger.debug(f"Email scan count failed: {e}")

    # Cost summary (7-day window)
    try:
        from core.cost_calculator import compute_cost_summary
        usage = supabase_client.get_token_usage_summary(days=7)
        result["cost_summary"] = compute_cost_summary(usage)
    except Exception as e:
        logger.debug(f"Cost summary failed: {e}")

    return result


async def _compile_gantt_proposals() -> dict:
    """Compile pending Gantt proposals with summaries."""
    result = {
        "proposals": [],
        "count": 0,
    }

    try:
        proposals = supabase_client.get_pending_gantt_proposals()
        result["proposals"] = proposals
        result["count"] = len(proposals)
    except Exception as e:
        logger.error(f"Gantt proposals compilation failed: {e}")

    return result


async def _compile_attention_needed() -> dict:
    """Compile alerts, stale tasks, approaching milestones, and task hygiene items."""
    result = {
        "alerts": [],
        "stale_tasks": [],
        "approaching_milestones": [],
        "escalation_items": [],
        "tasks_no_assignee": [],
        "tasks_no_deadline": [],
    }

    # Proactive alerts
    try:
        from processors.proactive_alerts import generate_alerts
        result["alerts"] = generate_alerts()
    except Exception as e:
        logger.error(f"Attention-needed alerts failed: {e}")

    # Stale tasks (>14 days pending)
    try:
        stale = supabase_client.get_stale_tasks(days=14)
        result["stale_tasks"] = stale
    except Exception as e:
        logger.error(f"Stale tasks failed: {e}")

    # Approaching milestones from Gantt
    try:
        from services.gantt_manager import gantt_manager
        horizon = await gantt_manager.get_gantt_horizon(weeks_ahead=4)
        milestones = horizon.get("milestones", [])
        result["approaching_milestones"] = milestones
    except Exception as e:
        logger.debug(f"Approaching milestones failed: {e}")

    # Escalation items (priority-aware graduated tiers)
    try:
        from processors.proactive_alerts import get_escalation_items
        result["escalation_items"] = get_escalation_items()
    except Exception as e:
        logger.debug(f"Escalation items failed: {e}")

    # Task hygiene: unassigned and no-deadline tasks
    try:
        result["tasks_no_assignee"] = supabase_client.get_tasks_without_assignee()
    except Exception as e:
        logger.debug(f"Tasks without assignee query failed: {e}")

    try:
        result["tasks_no_deadline"] = supabase_client.get_tasks_without_deadline()
    except Exception as e:
        logger.debug(f"Tasks without deadline query failed: {e}")

    return result


async def _compile_next_week_preview() -> dict:
    """Compile next week calendar events, deadlines, and priorities."""
    result = {
        "upcoming_meetings": [],
        "deadlines": [],
        "gantt_status": {},
    }

    # Upcoming meetings
    try:
        from processors.weekly_digest import get_upcoming_meetings
        result["upcoming_meetings"] = await get_upcoming_meetings(days=7)
    except Exception as e:
        logger.error(f"Next-week meetings failed: {e}")

    # Gantt status for next week
    try:
        from services.gantt_manager import gantt_manager
        result["gantt_status"] = await gantt_manager.get_gantt_status()
    except Exception as e:
        logger.debug(f"Gantt status for next week failed: {e}")

    # Deadlines from tasks due next week
    try:
        from processors.weekly_digest import get_task_summary
        task_summary = await get_task_summary()
        result["deadlines"] = task_summary.get("due_next_week", [])
    except Exception as e:
        logger.debug(f"Next-week deadlines failed: {e}")

    return result


async def _compile_horizon_check() -> dict:
    """Compile strategic milestones and red flags from Gantt horizon."""
    result = {
        "milestones": [],
        "sections": [],
        "red_flags": [],
    }

    try:
        from services.gantt_manager import gantt_manager
        horizon = await gantt_manager.get_gantt_horizon(weeks_ahead=12)
        result["milestones"] = horizon.get("milestones", [])
        result["sections"] = horizon.get("sections", [])
    except Exception as e:
        logger.debug(f"Horizon check failed: {e}")

    return result


def parse_milestone_markers(text: str) -> dict:
    """
    Parse milestone markers from Gantt cell text.

    Markers:
    - ★ tech milestone
    - ● commercial milestone
    - ◆ funding milestone

    Returns:
        Dict with is_milestone, marker_type, clean_text.
    """
    markers = {
        "★": "tech",
        "●": "commercial",
        "◆": "funding",
    }

    for marker, marker_type in markers.items():
        if marker in text:
            clean = text.replace(marker, "").strip()
            return {
                "is_milestone": True,
                "marker_type": marker_type,
                "marker": marker,
                "clean_text": clean,
            }

    return {
        "is_milestone": False,
        "marker_type": None,
        "marker": None,
        "clean_text": text,
    }
