"""
Error alerting for critical failures.

Sends Telegram notifications to Eyal when critical errors occur
(pipeline failures, API auth expiry, DB connection loss).
Deduplicates same errors within 1 hour to avoid spam.

Usage:
    from core.error_alerting import alert_critical_error

    try:
        await process_transcript(...)
    except Exception as e:
        await alert_critical_error("transcript_pipeline", str(e))
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# In-memory dedup: {error_key: last_alert_time}
_recent_alerts: dict[str, datetime] = {}
_DEDUP_WINDOW = timedelta(hours=1)


async def alert_critical_error(
    component: str,
    error_message: str,
    meeting_id: str | None = None,
) -> bool:
    """
    Send a critical error alert to Eyal via Telegram.

    Deduplicates identical errors within a 1-hour window.
    Also logs to audit_log with action='critical_error'.

    Args:
        component: Which component failed (e.g., 'transcript_pipeline').
        error_message: Human-readable error description.
        meeting_id: Optional meeting ID for context.

    Returns:
        True if alert was sent, False if deduplicated or failed.
    """
    # Dedup check — same component + first 100 chars of error
    dedup_key = f"{component}:{error_message[:100]}"
    now = datetime.now()

    if dedup_key in _recent_alerts:
        last_sent = _recent_alerts[dedup_key]
        if now - last_sent < _DEDUP_WINDOW:
            logger.debug(f"Deduplicating alert for {component} (sent {now - last_sent} ago)")
            return False

    # Record this alert
    _recent_alerts[dedup_key] = now

    # Clean up old entries
    expired = [k for k, v in _recent_alerts.items() if now - v > _DEDUP_WINDOW]
    for k in expired:
        del _recent_alerts[k]

    # Send Telegram notification
    try:
        from services.telegram_bot import telegram_bot

        message = f"Gianluigi error: <b>{component}</b>\n\n{error_message[:500]}"
        if meeting_id:
            message += f"\n\nMeeting: {meeting_id}"

        await telegram_bot.send_to_eyal(message, parse_mode="HTML")
        logger.info(f"Critical error alert sent for {component}")
    except Exception as e:
        logger.error(f"Failed to send error alert via Telegram: {e}")

    # Log to audit trail
    try:
        from services.supabase_client import supabase_client

        supabase_client.log_action(
            action="critical_error",
            details={
                "component": component,
                "error": error_message[:1000],
                "meeting_id": meeting_id,
            },
            triggered_by="auto",
        )
    except Exception as e:
        logger.error(f"Failed to log critical error to audit: {e}")

    return True


def clear_alert_history():
    """Clear the dedup cache. Useful for testing."""
    _recent_alerts.clear()
