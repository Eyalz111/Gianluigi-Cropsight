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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

# DST-aware Israel time. A hardcoded UTC+2 offset fired the brief 1h late for the
# ~7 months/year Israel is on DST (UTC+3) and nudged the Shabbat skip boundary off
# by an hour near Fri/Sat midnight. [audit P4-02]
_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# How long after the trigger a restart will still "catch up" a missed brief. Past
# this, the day is marked handled (no stale night-time "morning" brief). [audit P4-03]
_CATCHUP_GRACE_HOURS = 4


class MorningBriefScheduler:
    """Triggers morning brief at MORNING_BRIEF_HOUR IST daily."""

    def __init__(self):
        self._running = False
        # YYYY-MM-DD (IST) of the last brief cycle, reconstructed on boot — the
        # restart-safe fire-once guard. [audit P4-03]
        self._last_run_date: str | None = None

    async def start(self) -> None:
        """Loop: sleep until next trigger time, then run."""
        if self._running:
            logger.warning("Morning brief scheduler already running")
            return
        self._running = True
        # Restart-safe fire-once: rebuild the "ran today" guard from the last
        # successful heartbeat so a Cloud Run cycle neither double-fires nor
        # (via the catch-up below) silently skips today's brief. [audit P4-03]
        try:
            from schedulers.fire_once import last_ok_day_key
            self._last_run_date = last_ok_day_key("morning_brief") or self._last_run_date
            if self._last_run_date:
                logger.info(f"Morning brief: reconstructed last-run date {self._last_run_date} on boot")
        except Exception as e:
            logger.warning(f"Morning brief fire-once reconstruct failed (non-fatal): {e}")
        logger.info(
            f"Morning brief scheduler started (hour: {settings.MORNING_BRIEF_HOUR} IST)"
        )

        while self._running:
            try:
                now_ist = datetime.now(_ISRAEL_TZ)
                today = now_ist.strftime("%Y-%m-%d")
                trigger_today = now_ist.replace(
                    hour=settings.MORNING_BRIEF_HOUR, minute=0, second=0, microsecond=0
                )
                # CATCH-UP: if a restart landed just after today's trigger and the
                # brief hasn't run, fire it NOW — the old code's `now >= trigger →
                # +1day` silently SKIPPED that day. Bounded to a grace window so a
                # restart late in the day doesn't push a stale "morning" brief at
                # night; past the window we mark today handled and wait for
                # tomorrow. [audit P4-03]
                if now_ist >= trigger_today and self._last_run_date != today:
                    if now_ist < trigger_today + timedelta(hours=_CATCHUP_GRACE_HOURS):
                        await self._run_brief()
                        self._heartbeat()
                    else:
                        logger.info(
                            f"Morning brief: past the {_CATCHUP_GRACE_HOURS}h catch-up window "
                            f"for {today} — skipping to tomorrow"
                        )
                    self._last_run_date = today
                    continue

                await self._sleep_until_trigger()
                if not self._running:
                    break

                # Guard against a double-run (e.g. the catch-up already fired today).
                today = datetime.now(_ISRAEL_TZ).strftime("%Y-%m-%d")
                if self._last_run_date == today:
                    await asyncio.sleep(3600)
                    continue

                await self._run_brief()
                self._last_run_date = today
                # Heartbeat AFTER a completed cycle so a wedged sleep-until loop
                # is visible to /status and the QA scheduler, AND so the fire-once
                # guard can be reconstructed from it on the next boot. [audit P4-01/P4-03]
                self._heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Morning brief scheduler error: {e}")
                self._heartbeat(status="error", details={"error": str(e)})
                # Sleep a bit before retrying to avoid tight error loops
                await asyncio.sleep(300)

    @staticmethod
    def _heartbeat(status: str = "ok", details: dict | None = None) -> None:
        """Record a heartbeat so a stalled loop is detectable. Best-effort."""
        try:
            from services.supabase_client import supabase_client
            supabase_client.upsert_scheduler_heartbeat(
                "morning_brief", status=status, details=details
            )
        except Exception as e:
            logger.debug(f"morning_brief heartbeat write failed: {e}")

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Morning brief scheduler stopped")

    async def _sleep_until_trigger(self) -> None:
        """Calculate and sleep until next MORNING_BRIEF_HOUR IST."""
        now_ist = datetime.now(_ISRAEL_TZ)

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

        # ZoneInfo-aware subtraction yields the correct wall-clock delta across
        # a DST boundary.
        sleep_seconds = max(0, (trigger_ist - now_ist).total_seconds())

        logger.info(
            f"Morning brief: next trigger in {sleep_seconds / 3600:.1f} hours "
            f"({trigger_ist.strftime('%Y-%m-%d %H:%M')} IST)"
        )
        await asyncio.sleep(sleep_seconds)

    def _should_skip_today(self) -> bool:
        """Check if today is a skip day (e.g. Shabbat)."""
        import calendar
        now_ist = datetime.now(_ISRAEL_TZ)
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
