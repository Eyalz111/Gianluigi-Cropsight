"""
Operational snapshot — compressed daily "State of CropSight Operations."

Generates a 3-5 paragraph prose summary of the current operational state,
distilling tasks, decisions, meetings, Gantt status, and attention items
into CEO-readable text. Stored daily, returned by get_system_context().

Replaces the need for 7+ MCP tool calls to understand "where things stand."

Usage:
    from processors.operational_snapshot import generate_operational_snapshot
    snapshot = await generate_operational_snapshot()
"""

import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Retention: keep last 30 days, prune older
_RETENTION_DAYS = 30


async def generate_operational_snapshot() -> dict:
    """
    Generate a compressed operational snapshot for today.

    Gathers data from multiple sources, calls Sonnet to distill into
    3-5 paragraph prose, stores in operational_snapshots table.

    Returns:
        Dict with snapshot content and metadata.
    """
    from core.llm import call_llm

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Gather raw data
    structured = {}

    try:
        tasks = supabase_client.get_tasks(status="pending", limit=50)
        in_progress = supabase_client.get_tasks(status="in_progress", limit=20)
        done_recent = supabase_client.get_tasks(status="done", limit=10)
        structured["tasks"] = {
            "pending": len(tasks),
            "in_progress": len(in_progress),
            "recently_completed": len(done_recent),
            "top_pending": [
                f"{t.get('assignee', '?')}: {t.get('title', '')[:60]}"
                for t in tasks[:5]
            ],
        }
    except Exception:
        structured["tasks"] = {"error": "unavailable"}

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        meetings = supabase_client.list_meetings(limit=20)
        recent = [m for m in meetings if m.get("created_at", "") > cutoff]
        structured["meetings_this_week"] = len(recent)
        structured["recent_meeting_titles"] = [
            m.get("title", "")[:50] for m in recent[:5]
        ]
    except Exception:
        structured["meetings_this_week"] = 0

    try:
        decisions = supabase_client.list_decisions(limit=10)
        recent_decisions = [
            d for d in decisions
            if d.get("created_at", "") > (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        ]
        structured["decisions_this_week"] = len(recent_decisions)
        structured["top_decisions"] = [
            d.get("description", "")[:60] for d in recent_decisions[:3]
        ]
    except Exception:
        structured["decisions_this_week"] = 0

    try:
        pending_approvals = supabase_client.get_pending_approval_summary()
        structured["pending_approvals"] = len(pending_approvals)
    except Exception:
        structured["pending_approvals"] = 0

    try:
        from processors.proactive_alerts import generate_alerts
        alerts = generate_alerts()
        structured["alerts"] = [
            f"[{a.get('severity', 'info').upper()}] {a.get('title', '')}"
            for a in alerts[:5]
        ]
    except Exception:
        structured["alerts"] = []

    # Build prompt for Sonnet
    data_text = _format_structured_data(structured)

    prompt = f"""You are an AI operations assistant writing a daily operational brief for the CEO of CropSight, an Israeli AgTech startup.

Distill the following operational data into a concise "State of CropSight Operations" brief (3-5 paragraphs). Write for a CEO who needs to understand the current situation in 60 seconds.

Structure:
1. Opening: One sentence on overall momentum (things moving, stalled, or blocked)
2. Key highlights: What happened this week (meetings, decisions, completions)
3. Attention items: What needs the CEO's attention (overdue tasks, pending approvals, alerts)
4. Coming up: What's ahead (upcoming meetings, approaching deadlines)

Keep it professional, specific (use names and project names), and actionable.

OPERATIONAL DATA:
{data_text}

Write the brief now:"""

    try:
        content, usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,  # Sonnet
            max_tokens=1024,
            call_site="operational_snapshot",
        )

        tokens_used = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)

        # Store snapshot
        _store_snapshot(today, content, structured, tokens_used)

        # Prune old snapshots
        _prune_old_snapshots()

        logger.info(f"Generated operational snapshot for {today} ({len(content)} chars)")

        return {
            "date": today,
            "content": content,
            "tokens_used": tokens_used,
        }

    except Exception as e:
        logger.error(f"Failed to generate operational snapshot: {e}")
        return {
            "date": today,
            "content": f"Snapshot generation failed: {e}",
            "tokens_used": 0,
        }


def get_latest_snapshot() -> dict | None:
    """
    Get the most recent operational snapshot.

    Returns:
        Snapshot dict or None if no snapshot exists.
    """
    try:
        result = (
            supabase_client.client.table("operational_snapshots")
            .select("*")
            .order("snapshot_date", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.debug(f"Could not fetch latest snapshot: {e}")
        return None


def _store_snapshot(date: str, content: str, structured: dict, tokens: int) -> None:
    """Store or update today's snapshot."""
    try:
        supabase_client.client.table("operational_snapshots").upsert({
            "workspace_id": "cropsight",
            "snapshot_date": date,
            "content": content,
            "structured_data": structured,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tokens_used": tokens,
        }, on_conflict="workspace_id,snapshot_date").execute()
    except Exception as e:
        logger.error(f"Failed to store snapshot: {e}")


def _prune_old_snapshots() -> None:
    """Delete snapshots older than retention period."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)).strftime("%Y-%m-%d")
        supabase_client.client.table("operational_snapshots").delete().lt(
            "snapshot_date", cutoff
        ).execute()
    except Exception as e:
        logger.debug(f"Snapshot pruning failed (non-fatal): {e}")


def _format_structured_data(data: dict) -> str:
    """Format structured data for the LLM prompt."""
    lines = []

    tasks = data.get("tasks", {})
    if isinstance(tasks, dict) and "error" not in tasks:
        lines.append(f"Tasks: {tasks.get('pending', 0)} pending, {tasks.get('in_progress', 0)} in progress, {tasks.get('recently_completed', 0)} recently completed")
        top = tasks.get("top_pending", [])
        if top:
            lines.append("Top pending tasks:")
            for t in top:
                lines.append(f"  - {t}")

    lines.append(f"Meetings this week: {data.get('meetings_this_week', 0)}")
    titles = data.get("recent_meeting_titles", [])
    if titles:
        lines.append(f"  Recent: {', '.join(titles)}")

    lines.append(f"Decisions this week: {data.get('decisions_this_week', 0)}")
    top_d = data.get("top_decisions", [])
    if top_d:
        for d in top_d:
            lines.append(f"  - {d}")

    lines.append(f"Pending approvals: {data.get('pending_approvals', 0)}")

    alerts = data.get("alerts", [])
    if alerts:
        lines.append("Alerts:")
        for a in alerts:
            lines.append(f"  - {a}")

    return "\n".join(lines)
