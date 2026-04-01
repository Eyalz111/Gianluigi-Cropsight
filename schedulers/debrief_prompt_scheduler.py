"""
Evening debrief prompt scheduler.

Sends a light-touch "End of day" prompt to Eyal at DEBRIEF_EVENING_PROMPT_HOUR IST.
Skips Saturday (Shabbat).

Usage:
    from schedulers.debrief_prompt_scheduler import debrief_prompt_scheduler
    await debrief_prompt_scheduler.start()
"""

import asyncio
import calendar
import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings

logger = logging.getLogger(__name__)

# Israel Standard Time is UTC+2 (or UTC+3 during DST)
IST_OFFSET = timedelta(hours=2)


class DebriefPromptScheduler:
    """Sends evening debrief prompt at DEBRIEF_EVENING_PROMPT_HOUR IST daily."""

    def __init__(self):
        self._running = False

    async def start(self) -> None:
        """Loop: sleep until next trigger time, then send prompt."""
        if self._running:
            logger.warning("Debrief prompt scheduler already running")
            return
        self._running = True
        logger.info(
            f"Debrief prompt scheduler started (hour: {settings.DEBRIEF_EVENING_PROMPT_HOUR} IST)"
        )

        while self._running:
            try:
                await self._sleep_until_trigger()
                if not self._running:
                    break
                await self._send_prompt()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Debrief prompt scheduler error: {e}")
                await asyncio.sleep(300)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Debrief prompt scheduler stopped")

    async def _sleep_until_trigger(self) -> None:
        """Calculate and sleep until next DEBRIEF_EVENING_PROMPT_HOUR IST."""
        now_utc = datetime.now(timezone.utc)
        now_ist = now_utc + IST_OFFSET

        trigger_ist = now_ist.replace(
            hour=settings.DEBRIEF_EVENING_PROMPT_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )

        if now_ist >= trigger_ist:
            trigger_ist += timedelta(days=1)

        trigger_utc = trigger_ist - IST_OFFSET
        sleep_seconds = (trigger_utc - now_utc).total_seconds()

        logger.info(
            f"Debrief prompt: next trigger in {sleep_seconds / 3600:.1f} hours "
            f"({trigger_ist.strftime('%Y-%m-%d %H:%M')} IST)"
        )
        await asyncio.sleep(sleep_seconds)

    def _should_skip_today(self) -> bool:
        """Check if today is Saturday (Shabbat) — skip debrief."""
        now_ist = datetime.now(timezone.utc) + IST_OFFSET
        return calendar.day_name[now_ist.weekday()] == "Saturday"

    async def _send_prompt(self) -> None:
        """Send the evening debrief prompt to Eyal."""
        if self._should_skip_today():
            logger.info("Debrief prompt skipped (Saturday)")
            return

        from services.telegram_bot import telegram_bot
        from services.supabase_client import supabase_client

        message = (
            "<b>End of day</b> \u2014 anything to add?\n\n"
            "Reply with /debrief to start a session, "
            "or just type updates and I'll process them."
        )

        try:
            await telegram_bot.send_to_eyal(message, parse_mode="HTML")
            supabase_client.upsert_scheduler_heartbeat(
                name="debrief_prompt",
                status="ok",
                details={"sent": True},
            )
            logger.info("Debrief prompt sent to Eyal")
        except Exception as e:
            logger.error(f"Failed to send debrief prompt: {e}")
            from core.health_monitor import check_and_alert
            await check_and_alert("debrief_prompt", e)


# Singleton
debrief_prompt_scheduler = DebriefPromptScheduler()
