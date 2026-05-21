"""
Tasks reconcile scheduler (v3 outputs re-architecture).

Runs the Sheet<->DB reconcile twice daily — midday and pre-nightly (the latter
strictly before the knowledge nightly, so the DB is correct before nightly reads
tasks). On-demand reconcile is via the `/sync` MCP tool. Honors
RECONCILE_SHADOW_MODE (the engine writes nothing while shadow is on).

Mirrors the class-based async pattern of the knowledge schedulers.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class ReconcileScheduler:
    """Twice-daily Tasks reconcile (midday + pre-nightly)."""

    def __init__(self):
        self._running = False
        self._last_slot: str | None = None  # "YYYY-MM-DD:midday" | ":prenightly"

    async def start(self) -> None:
        if self._running:
            logger.warning("Reconcile scheduler already running")
            return
        self._running = True
        logger.info(
            f"Reconcile scheduler started "
            f"(midday={settings.RECONCILE_MIDDAY_HOUR}, "
            f"pre-nightly={settings.RECONCILE_PRENIGHTLY_HOUR} IST)"
        )
        while self._running:
            try:
                slot = await self._sleep_until_next()
                if not self._running:
                    break
                if slot == self._last_slot:
                    await asyncio.sleep(3600)  # already ran this slot; avoid tight loop
                    continue
                await self._run(slot)
                self._last_slot = slot
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Reconcile scheduler error: {e}")
                await asyncio.sleep(300)

    def stop(self) -> None:
        self._running = False
        logger.info("Reconcile scheduler stopped")

    async def _sleep_until_next(self) -> str:
        now = datetime.now(_ISRAEL_TZ)
        candidates = []
        for hour, name in (
            (settings.RECONCILE_MIDDAY_HOUR, "midday"),
            (settings.RECONCILE_PRENIGHTLY_HOUR, "prenightly"),
        ):
            trig = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if now >= trig:
                trig += timedelta(days=1)
            candidates.append((trig, name))
        trig, name = min(candidates, key=lambda c: c[0])
        sleep_s = max(0, (trig - now).total_seconds())
        logger.info(
            f"Next reconcile ({name}): {trig.strftime('%a %Y-%m-%d %H:%M IST')} "
            f"({sleep_s / 3600:.1f}h from now)"
        )
        await asyncio.sleep(sleep_s)
        return f"{trig.strftime('%Y-%m-%d')}:{name}"

    async def _run(self, slot: str) -> None:
        logger.info(f"Reconcile triggering ({slot})")
        try:
            from processors.sheets_sync import reconcile_tasks

            summary = await reconcile_tasks()

            from services.supabase_client import supabase_client
            supabase_client.log_action(
                action="scheduler_heartbeat",
                details={"scheduler": "reconcile", "slot": slot, **summary},
                triggered_by="auto",
            )
        except Exception as e:
            logger.error(f"Reconcile failed: {e}")
            try:
                from core.health_monitor import check_and_alert
                await check_and_alert("reconcile", e)
            except Exception:
                pass


# Singleton instance
reconcile_scheduler = ReconcileScheduler()
