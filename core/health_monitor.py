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
from datetime import datetime, timedelta

from config.settings import settings

logger = logging.getLogger(__name__)


def collect_health_data() -> dict:
    """
    Collect health metrics from all system components.

    Returns:
        Dict with component health status and metrics.
    """
    from services.supabase_client import supabase_client

    data = {
        "timestamp": datetime.now().isoformat(),
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
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
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
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        meetings = supabase_client.list_meetings(limit=100)
        recent = [m for m in meetings if m.get("created_at", "") > cutoff]
        data["metrics"]["meetings_7d"] = len(recent)
    except Exception:
        data["metrics"]["meetings_7d"] = -1

    # 7. Scheduler heartbeats
    try:
        heartbeats = supabase_client.get_scheduler_heartbeats()
        now = datetime.now()
        scheduler_status = []

        # Expected intervals per scheduler (seconds)
        expected_intervals = {
            "transcript_watcher": 300,      # 5 min
            "document_watcher": 300,        # 5 min
            "email_watcher": 300,           # 5 min
            "meeting_prep": 14400,          # 4 hours
            "weekly_digest": 3600,          # 1 hour check
            "weekly_review": 3600,          # 1 hour check
            "orphan_cleanup": 86400,        # 24 hours
            "alert_scheduler": 43200,       # 12 hours
            "task_reminder": 3600,          # 1 hour
        }

        for hb in heartbeats:
            name = hb.get("scheduler_name", "?")
            last_run = hb.get("last_run_at", "")
            status = hb.get("status", "ok")
            stale = False

            if last_run:
                try:
                    last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                    if last_dt.tzinfo:
                        from datetime import timezone
                        last_dt = last_dt.replace(tzinfo=None)
                        now_compare = datetime.utcnow()
                    else:
                        now_compare = now
                    age_seconds = (now_compare - last_dt).total_seconds()
                    expected = expected_intervals.get(name, 3600)
                    stale = age_seconds > expected * 2
                except (ValueError, TypeError):
                    pass

            scheduler_status.append({
                "name": name,
                "last_run": last_run,
                "status": status,
                "stale": stale,
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

        from services.telegram_bot import telegram_bot
        await telegram_bot.send_to_eyal(report)
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
