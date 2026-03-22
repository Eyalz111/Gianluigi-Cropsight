"""
Tiered system alerting for Gianluigi.

Severity levels:
- CRITICAL: Immediate Telegram DM to Eyal (transcript failure, approval error, etc.)
- WARNING: Buffered, flushed during daily health message
- INFO: Log only

Usage:
    from services.alerting import send_system_alert, AlertSeverity

    await send_system_alert(
        AlertSeverity.CRITICAL,
        "transcript_processor",
        "Failed to process transcript",
        error=e,
    )
"""

import logging
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class AlertSeverity(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# In-memory buffer for warnings (flushed during morning health message)
_warning_buffer: list[dict] = []


async def send_system_alert(
    severity: AlertSeverity,
    component: str,
    message: str,
    error: Exception | None = None,
) -> None:
    """
    Route system alerts based on severity.

    CRITICAL → immediate Telegram DM to Eyal
    WARNING → buffer for daily batch
    INFO → log only
    """
    timestamp = datetime.now(_ISRAEL_TZ).strftime("%H:%M")

    if severity == AlertSeverity.CRITICAL:
        alert_text = (
            f"System Alert ({timestamp})\n"
            f"Component: {component}\n"
            f"Error: {message}"
        )
        if error:
            error_detail = f"{type(error).__name__}: {str(error)[:200]}"
            alert_text += f"\nDetails: {error_detail}"

        # Lazy import to avoid circular imports
        try:
            from services.telegram_bot import telegram_bot

            await telegram_bot.send_to_eyal(alert_text)
        except Exception as send_err:
            logger.critical(
                f"CANNOT SEND ALERT TO TELEGRAM: {send_err} | Original: {message}"
            )

    elif severity == AlertSeverity.WARNING:
        _warning_buffer.append({
            "timestamp": timestamp,
            "component": component,
            "message": message,
        })

    # Always log regardless of severity
    log_msg = f"[{severity.value.upper()}] [{component}] {message}"
    if severity == AlertSeverity.CRITICAL:
        logger.error(log_msg)
    elif severity == AlertSeverity.WARNING:
        logger.warning(log_msg)
    else:
        logger.info(log_msg)


def get_and_flush_warnings() -> list[dict]:
    """
    Called by morning health heartbeat to include warnings in daily report.

    Returns buffered warnings and clears the buffer.
    """
    warnings = _warning_buffer.copy()
    _warning_buffer.clear()
    return warnings
