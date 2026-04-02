"""
Intelligence Signal scheduler.

Triggers weekly intelligence signal generation on the configured day and hour
(default: Thursday 18:00 IST). Uses ZoneInfo("Asia/Jerusalem") for DST-aware
scheduling.

Class-based async pattern matching MorningBriefScheduler.

Usage:
    from schedulers.intelligence_signal_scheduler import intelligence_signal_scheduler

    # In main.py start_services():
    task = asyncio.create_task(intelligence_signal_scheduler.start())

    # In main.py stop_services():
    intelligence_signal_scheduler.stop()
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class IntelligenceSignalScheduler:
    """
    Triggers weekly intelligence signal generation.

    Runs as an async loop: sleep until the next trigger time, then generate.
    Skips if a signal has already been generated for the current week.
    """

    def __init__(self):
        self._running = False
        self._last_generated_week: str | None = None

    async def start(self) -> None:
        """
        Main scheduler loop.

        Sleeps until the configured day/hour, generates, then sleeps again.
        """
        if self._running:
            logger.warning("Intelligence signal scheduler already running")
            return

        self._running = True
        logger.info(
            f"Intelligence signal scheduler started "
            f"(day={settings.INTELLIGENCE_SIGNAL_DAY}, "
            f"hour={settings.INTELLIGENCE_SIGNAL_HOUR} IST)"
        )

        while self._running:
            try:
                await self._sleep_until_trigger()
                if not self._running:
                    break

                # Check if already generated this week
                now_ist = datetime.now(_ISRAEL_TZ)
                week_key = f"w{now_ist.isocalendar()[1]}-{now_ist.isocalendar()[0]}"

                if week_key == self._last_generated_week:
                    logger.info(
                        f"Signal already generated for {week_key}, skipping"
                    )
                    # Sleep 1 hour to avoid tight loop
                    await asyncio.sleep(3600)
                    continue

                # Generate signal
                await self._run_generation()
                self._last_generated_week = week_key

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Intelligence signal scheduler error: {e}")
                await asyncio.sleep(300)  # 5 min cooldown on error

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Intelligence signal scheduler stopped")

    async def _sleep_until_trigger(self) -> None:
        """Calculate and sleep until the next configured day/hour in IST."""
        now_ist = datetime.now(_ISRAEL_TZ)
        target_day = settings.INTELLIGENCE_SIGNAL_DAY  # 0=Mon, 3=Thu
        target_hour = settings.INTELLIGENCE_SIGNAL_HOUR

        # Find next occurrence of target day/hour
        trigger_ist = now_ist.replace(
            hour=target_hour, minute=0, second=0, microsecond=0
        )

        # Calculate days until target day
        days_ahead = target_day - now_ist.weekday()
        if days_ahead < 0:
            days_ahead += 7
        elif days_ahead == 0 and now_ist >= trigger_ist:
            days_ahead += 7

        trigger_ist += timedelta(days=days_ahead)

        sleep_seconds = (trigger_ist - now_ist).total_seconds()
        if sleep_seconds < 0:
            sleep_seconds = 0

        logger.info(
            f"Next intelligence signal: {trigger_ist.strftime('%a %Y-%m-%d %H:%M IST')} "
            f"({sleep_seconds / 3600:.1f}h from now)"
        )
        await asyncio.sleep(sleep_seconds)

    async def _run_generation(self) -> None:
        """Execute the intelligence signal generation pipeline."""
        logger.info("Intelligence signal scheduler triggering generation")
        try:
            from processors.intelligence_signal_agent import (
                generate_intelligence_signal,
            )

            result = await generate_intelligence_signal()
            status = result.get("status", "unknown")
            signal_id = result.get("signal_id", "unknown")

            logger.info(
                f"Intelligence signal generation complete: "
                f"{signal_id} — {status}"
            )

            # Heartbeat logging
            from services.supabase_client import supabase_client

            supabase_client.log_action(
                action="scheduler_heartbeat",
                details={
                    "scheduler": "intelligence_signal",
                    "signal_id": signal_id,
                    "status": status,
                },
                triggered_by="auto",
            )

        except Exception as e:
            logger.error(f"Intelligence signal generation failed: {e}")
            # Alert via health monitor
            try:
                from core.health_monitor import check_and_alert

                await check_and_alert("intelligence_signal", e)
            except Exception:
                pass


# Singleton instance
intelligence_signal_scheduler = IntelligenceSignalScheduler()
