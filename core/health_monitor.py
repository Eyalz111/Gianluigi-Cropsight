"""
Health monitoring for Gianluigi.

Collects system health data and formats it for daily reports.
Sends real-time alerts for critical failures via error_alerting.

Usage:
    from core.health_monitor import collect_health_data, format_daily_health_report

    data = collect_health_data()
    report = format_daily_health_report(data)
"""

import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings

logger = logging.getLogger(__name__)


# Expected heartbeat interval per scheduler (seconds). A heartbeat older than 2x
# its interval is flagged stale. The daily/weekly sleep-until schedulers were
# previously omitted, so a wedged knowledge/reconcile/brief loop defaulted to the
# 1h fallback and was either invisible or falsely-stale. [audit P4-01]
_EXPECTED_INTERVALS = {
    # poll-interval
    "transcript_watcher": 300,      # 5 min
    "document_watcher": 300,        # 5 min
    "email_watcher": 300,           # 5 min
    "task_sync": 3600,              # ~1 hour
    "meeting_prep": 14400,          # 4 hours
    "prep_ping": 3600,              # 1 hour check
    "weekly_digest": 3600,          # 1 hour check
    "weekly_review": 3600,          # 1 hour check
    "weekly_pulse": 3600,           # 1 hour check
    "task_reminder": 3600,          # 1 hour
    "orphan_cleanup": 86400,        # 24 hours
    "alert_scheduler": 43200,       # 12 hours
    "rollout": 86400,               # daily
    # daily/weekly sleep-until (now heartbeat to the right table, P4-01)
    "morning_brief": 86400,         # daily
    "debrief_prompt": 86400,        # daily
    "intelligence_signal": 604800,  # weekly
    "knowledge_nightly": 86400,     # daily
    "knowledge_weekly": 604800,     # weekly
    "reconcile": 86400,             # runs midday + pre-digest daily
}


def _heartbeat_stale(last_run: str, name: str, now_utc: datetime | None = None) -> bool:
    """True if a scheduler's last heartbeat is older than 2x its expected interval.

    UTC-aware throughout: heartbeats store UTC timestamptz and the container may
    boot in Asia/Jerusalem, so a naive compare drifted by hours. [audit P4-01/P6-06]
    """
    if not last_run:
        return True
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    try:
        last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age = (now_utc - last_dt).total_seconds()
        return age > _EXPECTED_INTERVALS.get(name, 3600) * 2
    except (ValueError, TypeError):
        return False


def collect_health_data() -> dict:
    """
    Collect health metrics from all system components.

    Returns:
        Dict with component health status and metrics.
    """
    from services.supabase_client import supabase_client

    data = {
        # tz-aware UTC, consistent with the cutoffs below — a naive local stamp
        # on a non-UTC container mislabels the report time. [audit P4-08]
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": {},
        "metrics": {},
    }

    # 1. Supabase connectivity
    try:
        result = supabase_client.client.table("audit_log").select("id").limit(1).execute()
        data["components"]["supabase"] = "healthy"
    except Exception as e:
        data["components"]["supabase"] = f"error: {str(e)[:100]}"

    # 2. Pending approvals count
    try:
        pending = supabase_client.get_pending_approval_summary()
        data["metrics"]["pending_approvals"] = len(pending)
    except Exception:
        data["metrics"]["pending_approvals"] = -1

    # 3. Error count from action_log (last 24h)
    try:
        # UTC-aware cutoff: the rows store UTC timestamptz, and the container can
        # boot in Asia/Jerusalem — a naive datetime.now() put the cutoff hours in
        # the future and silently dropped the most recent errors. [audit P6-06]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        result = (
            supabase_client.client.table("audit_log")
            .select("id", count="exact")
            .eq("action", "critical_error")
            .gte("created_at", cutoff)
            .execute()
        )
        data["metrics"]["errors_24h"] = result.count or 0
    except Exception:
        data["metrics"]["errors_24h"] = -1

    # 4. Google OAuth token validity
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
        )
        data["components"]["google_oauth"] = "configured" if settings.GOOGLE_REFRESH_TOKEN else "not configured"
    except Exception as e:
        data["components"]["google_oauth"] = f"error: {str(e)[:100]}"

    # 5. Telegram bot status
    try:
        data["components"]["telegram"] = "configured" if settings.TELEGRAM_BOT_TOKEN else "not configured"
    except Exception:
        data["components"]["telegram"] = "unknown"

    # 6. Recent meetings processed (last 7 days)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        meetings = supabase_client.list_meetings(limit=100)
        recent = [m for m in meetings if m.get("created_at", "") > cutoff]
        data["metrics"]["meetings_7d"] = len(recent)
    except Exception:
        data["metrics"]["meetings_7d"] = -1

    # 7. Scheduler heartbeats
    try:
        heartbeats = supabase_client.get_scheduler_heartbeats()
        now_utc = datetime.now(timezone.utc)
        scheduler_status = []

        for hb in heartbeats:
            name = hb.get("scheduler_name", "?")
            last_run = hb.get("last_run_at", "")
            scheduler_status.append({
                "name": name,
                "last_run": last_run,
                "status": hb.get("status", "ok"),
                "stale": _heartbeat_stale(last_run, name, now_utc),
                "details": hb.get("details"),
            })

        data["schedulers"] = scheduler_status
    except Exception:
        data["schedulers"] = []

    return data


def format_daily_health_report(data: dict) -> str:
    """
    Format health data into a Telegram-friendly message.

    Args:
        data: Health data dict from collect_health_data().

    Returns:
        Formatted health report string.
    """
    lines = ["*Daily Health Report*\n"]

    # Components
    components = data.get("components", {})
    all_healthy = all(
        v in ("healthy", "configured")
        for v in components.values()
    )

    if all_healthy:
        lines.append("All systems operational.\n")
    else:
        for name, status in components.items():
            icon = "OK" if status in ("healthy", "configured") else "WARN"
            lines.append(f"  {icon}: {name} — {status}")
        lines.append("")

    # Metrics
    metrics = data.get("metrics", {})
    pending = metrics.get("pending_approvals", 0)
    errors = metrics.get("errors_24h", 0)
    meetings_7d = metrics.get("meetings_7d", 0)

    if pending > 0:
        lines.append(f"Pending approvals: {pending}")
    if errors > 0:
        lines.append(f"Errors (24h): {errors}")
    lines.append(f"Meetings processed (7d): {meetings_7d}")

    return "\n".join(lines)


async def send_daily_health_report() -> bool:
    """
    Collect health data and send report to Eyal.

    Returns:
        True if report was sent successfully.
    """
    if not settings.DAILY_HEALTH_REPORT_ENABLED:
        return False

    try:
        data = collect_health_data()
        report = format_daily_health_report(data)

        from services.orchestrator.spine import comms_spine
        await comms_spine.send_to_eyal(report)
        logger.info("Daily health report sent")
        return True
    except Exception as e:
        logger.error(f"Failed to send daily health report: {e}")
        return False


async def check_and_alert(component: str, error: Exception) -> None:
    """
    Convenience wrapper for scheduler error alerting.

    Args:
        component: Name of the component/scheduler that failed.
        error: The exception that occurred.
    """
    from core.error_alerting import alert_critical_error
    await alert_critical_error(component, str(error))
