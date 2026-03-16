"""
Morning brief scheduler.

Triggers the morning brief at MORNING_BRIEF_HOUR IST daily.
Phase 8 will absorb this into the unified heartbeat, but for now
it runs independently.

Usage:
    from schedulers.morning_brief_scheduler import morning_brief_scheduler
    await morning_brief_scheduler.start()
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings

logger = logging.getLogger(__name__)

# Israel Standard Time is UTC+2 (or UTC+3 during DST)
IST_OFFSET = timedelta(hours=2)


class MorningBriefScheduler:
    """Triggers morning brief at MORNING_BRIEF_HOUR IST daily."""

    def __init__(self):
        self._running = False

    async def start(self) -> None:
        """Loop: sleep until next trigger time, then run."""
        if self._running:
            logger.warning("Morning brief scheduler already running")
            return
        self._running = True
        logger.info(
            f"Morning brief scheduler started (hour: {settings.MORNING_BRIEF_HOUR} IST)"
        )

        while self._running:
            try:
                await self._sleep_until_trigger()
                if not self._running:
                    break
                await self._run_brief()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Morning brief scheduler error: {e}")
                # Sleep a bit before retrying to avoid tight error loops
                await asyncio.sleep(300)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Morning brief scheduler stopped")

    async def _sleep_until_trigger(self) -> None:
        """Calculate and sleep until next MORNING_BRIEF_HOUR IST."""
        now_utc = datetime.now(timezone.utc)
        now_ist = now_utc + IST_OFFSET

        # Calculate next trigger time in IST
        trigger_ist = now_ist.replace(
            hour=settings.MORNING_BRIEF_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )

        # If we've already passed the trigger time today, schedule for tomorrow
        if now_ist >= trigger_ist:
            trigger_ist += timedelta(days=1)

        # Convert back to UTC for sleep calculation
        trigger_utc = trigger_ist - IST_OFFSET
        sleep_seconds = (trigger_utc - now_utc).total_seconds()

        logger.info(
            f"Morning brief: next trigger in {sleep_seconds / 3600:.1f} hours "
            f"({trigger_ist.strftime('%Y-%m-%d %H:%M')} IST)"
        )
        await asyncio.sleep(sleep_seconds)

    def _should_skip_today(self) -> bool:
        """Check if today is a skip day (e.g. Shabbat)."""
        import calendar
        now_ist = datetime.now(timezone.utc) + IST_OFFSET
        today_name = calendar.day_name[now_ist.weekday()]
        return today_name in settings.morning_brief_skip_days_list

    async def _run_brief(self) -> None:
        """Execute the morning brief."""
        if self._should_skip_today():
            logger.info(f"Morning brief skipped (skip day)")
            return

        from processors.morning_brief import trigger_morning_brief

        logger.info("Running morning brief...")
        try:
            result = await trigger_morning_brief()
            if result:
                logger.info(f"Morning brief sent: {result.get('stats', {})}")
            else:
                logger.info("Morning brief: nothing to report")
        except Exception as e:
            logger.error(f"Morning brief execution failed: {e}")
            from core.health_monitor import check_and_alert
            await check_and_alert("morning_brief", e)

        # Send daily health report after morning brief
        try:
            from core.health_monitor import send_daily_health_report
            await send_daily_health_report()
        except Exception as e:
            logger.error(f"Health report failed: {e}")


# Singleton
morning_brief_scheduler = MorningBriefScheduler()
