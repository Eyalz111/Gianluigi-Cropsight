"""
Nightly knowledge-consolidation scheduler (v2.5 PR7/8).

Runs once per day at KNOWLEDGE_NIGHTLY_HOUR (IST) and sweeps the topic briefs:
recompute staleness, de-duplicate facts, and lightly reconcile recently-touched
briefs. When KNOWLEDGE_SHADOW_MODE is on it logs without applying.

Mirrors the class-based async pattern of IntelligenceSignalScheduler.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class KnowledgeNightlyScheduler:
    """Daily topic-brief consolidation sweep."""

    def __init__(self):
        self._running = False
        self._last_run_date: str | None = None  # YYYY-MM-DD (IST) idempotency

    async def start(self) -> None:
        if self._running:
            logger.warning("Knowledge nightly scheduler already running")
            return
        self._running = True
        # Restart-safe fire-once: rebuild the "ran today" guard from the last
        # successful heartbeat so a Cloud Run cycle can't re-run tonight's
        # consolidation. [audit P4-03]
        try:
            from schedulers.fire_once import last_ok_day_key
            self._last_run_date = last_ok_day_key("knowledge_nightly") or self._last_run_date
            if self._last_run_date:
                logger.info(f"Knowledge nightly: reconstructed last-run date {self._last_run_date} on boot")
        except Exception as e:
            logger.warning(f"Knowledge nightly fire-once reconstruct failed (non-fatal): {e}")
        logger.info(
            f"Knowledge nightly scheduler started (hour={settings.KNOWLEDGE_NIGHTLY_HOUR} IST)"
        )

        while self._running:
            try:
                await self._sleep_until_trigger()
                if not self._running:
                    break

                today = datetime.now(_ISRAEL_TZ).strftime("%Y-%m-%d")
                if today == self._last_run_date:
                    await asyncio.sleep(3600)  # already ran today; avoid tight loop
                    continue

                await self._run()
                self._last_run_date = today

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Knowledge nightly scheduler error: {e}")
                await asyncio.sleep(300)

    def stop(self) -> None:
        self._running = False
        logger.info("Knowledge nightly scheduler stopped")

    async def _sleep_until_trigger(self) -> None:
        now_ist = datetime.now(_ISRAEL_TZ)
        trigger_ist = now_ist.replace(
            hour=settings.KNOWLEDGE_NIGHTLY_HOUR, minute=0, second=0, microsecond=0
        )
        if now_ist >= trigger_ist:
            trigger_ist += timedelta(days=1)
        sleep_seconds = max(0, (trigger_ist - now_ist).total_seconds())
        logger.info(
            f"Next knowledge consolidation: {trigger_ist.strftime('%a %Y-%m-%d %H:%M IST')} "
            f"({sleep_seconds / 3600:.1f}h from now)"
        )
        await asyncio.sleep(sleep_seconds)

    async def _run(self) -> None:
        logger.info("Knowledge nightly consolidation triggering")
        try:
            from processors.knowledge_consolidation import run_consolidation

            summary = await run_consolidation()

            # Heartbeat must go to the scheduler_heartbeats table (what
            # get_scheduler_heartbeats / the health checks read), NOT audit_log.
            # log_action("scheduler_heartbeat") wrote to the wrong table, so this
            # loop was invisible to /status and the QA scheduler. [audit P4-01]
            from services.supabase_client import supabase_client
            supabase_client.upsert_scheduler_heartbeat(
                "knowledge_nightly",
                details=summary if isinstance(summary, dict) else {},
            )
        except Exception as e:
            logger.error(f"Knowledge nightly consolidation failed: {e}")
            try:
                from services.supabase_client import supabase_client
                supabase_client.upsert_scheduler_heartbeat(
                    "knowledge_nightly", status="error", details={"error": str(e)}
                )
            except Exception:
                pass
            try:
                from core.health_monitor import check_and_alert
                await check_and_alert("knowledge_nightly", e)
            except Exception:
                pass


# Singleton instance
knowledge_nightly_scheduler = KnowledgeNightlyScheduler()
