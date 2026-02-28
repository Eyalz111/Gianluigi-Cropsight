"""
Proactive alert scheduler for v0.3 Tier 2.

Runs every 12 hours, sends operational alerts once per day to Eyal
via Telegram DM. Follows the TaskReminderScheduler pattern.

Usage:
    from schedulers.alert_scheduler import alert_scheduler

    await alert_scheduler.start()
"""

import asyncio
import logging
from datetime import datetime

from processors.proactive_alerts import generate_alerts, format_alerts_message
from services.supabase_client import supabase_client
from services.telegram_bot import telegram_bot

logger = logging.getLogger(__name__)

# Default check interval: 12 hours
DEFAULT_CHECK_INTERVAL = 43200


class AlertScheduler:
    """
    Schedules and sends proactive operational alerts to Eyal.

    Runs on a 12-hour cycle. Sends at most one alert batch per day
    to avoid noise.
    """

    def __init__(self, check_interval: int = DEFAULT_CHECK_INTERVAL):
        """
        Initialize the alert scheduler.

        Args:
            check_interval: Seconds between checks (default 12 hours).
        """
        self.check_interval = check_interval
        self._running = False
        self._last_alert_date: str = ""

    async def start(self) -> None:
        """Start the alert scheduler loop."""
        if self._running:
            logger.warning("Alert scheduler already running")
            return

        self._running = True
        logger.info(f"Starting alert scheduler (interval: {self.check_interval}s)")

        while self._running:
            try:
                today = datetime.now().strftime("%Y-%m-%d")

                # Only send once per day
                if today != self._last_alert_date:
                    await self._check_and_send_alerts()
                    self._last_alert_date = today
                else:
                    logger.debug("Already sent alerts today, skipping")

            except Exception as e:
                logger.error(f"Error in alert scheduler: {e}")
                supabase_client.log_action(
                    action="alert_scheduler_error",
                    details={"error": str(e)},
                    triggered_by="auto",
                )

            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Alert scheduler stopped")

    async def _check_and_send_alerts(self) -> int:
        """
        Generate alerts and send to Eyal if any exist.

        Returns:
            Number of alerts sent.
        """
        logger.info("Checking for operational alerts...")

        alerts = generate_alerts()

        if not alerts:
            logger.info("No operational alerts to send")
            return 0

        message = format_alerts_message(alerts)
        if message:
            await telegram_bot.send_to_eyal(message)

            supabase_client.log_action(
                action="proactive_alerts_sent",
                details={
                    "alert_count": len(alerts),
                    "types": list(set(a.get("type", "") for a in alerts)),
                },
                triggered_by="auto",
            )

            logger.info(f"Sent {len(alerts)} operational alerts to Eyal")

        return len(alerts)


# Singleton instance
alert_scheduler = AlertScheduler()
