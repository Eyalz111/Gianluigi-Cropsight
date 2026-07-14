"""
Weekly knowledge-synthesis scheduler (v2.5 PR9/10).

Runs once per week (KNOWLEDGE_WEEKLY_DAY / KNOWLEDGE_WEEKLY_HOUR, IST):
re-synthesizes recently-active topic briefs from full history, refreshes the
area briefs, logs a reflection of topics needing attention, and runs the topic
clustering pass to PROPOSE merges / area assignments for Eyal's approval.

Mirrors the class-based async pattern of IntelligenceSignalScheduler.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class KnowledgeWeeklyScheduler:
    """Weekly deep synthesis + clustering-proposal pass."""

    def __init__(self):
        self._running = False
        self._last_generated_week: str | None = None

    async def start(self) -> None:
        if self._running:
            logger.warning("Knowledge weekly scheduler already running")
            return
        self._running = True
        # Restart-safe fire-once: rebuild the "ran this week" guard from the last
        # successful heartbeat so a Cloud Run cycle can't re-run this week's
        # synthesis. [audit P4-03]
        try:
            from schedulers.fire_once import last_ok_week_key
            self._last_generated_week = last_ok_week_key("knowledge_weekly") or self._last_generated_week
            if self._last_generated_week:
                logger.info(f"Knowledge weekly: reconstructed last-run week {self._last_generated_week} on boot")
        except Exception as e:
            logger.warning(f"Knowledge weekly fire-once reconstruct failed (non-fatal): {e}")
        logger.info(
            f"Knowledge weekly scheduler started "
            f"(day={settings.KNOWLEDGE_WEEKLY_DAY}, hour={settings.KNOWLEDGE_WEEKLY_HOUR} IST)"
        )

        while self._running:
            try:
                await self._sleep_until_trigger()
                if not self._running:
                    break

                now_ist = datetime.now(_ISRAEL_TZ)
                week_key = f"w{now_ist.isocalendar()[1]}-{now_ist.isocalendar()[0]}"
                if week_key == self._last_generated_week:
                    await asyncio.sleep(3600)
                    continue

                # Only mark the week done if the run actually succeeded, else
                # retry within the window — a swallowed failure must not silently
                # skip topic + decision synthesis for the week (audit SC-01).
                if await self._run():
                    self._last_generated_week = week_key
                else:
                    await asyncio.sleep(300)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Knowledge weekly scheduler error: {e}")
                await asyncio.sleep(300)

    def stop(self) -> None:
        self._running = False
        logger.info("Knowledge weekly scheduler stopped")

    async def _sleep_until_trigger(self) -> None:
        now_ist = datetime.now(_ISRAEL_TZ)
        target_day = settings.KNOWLEDGE_WEEKLY_DAY
        target_hour = settings.KNOWLEDGE_WEEKLY_HOUR

        trigger_ist = now_ist.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        days_ahead = target_day - now_ist.weekday()
        if days_ahead < 0:
            days_ahead += 7
        elif days_ahead == 0 and now_ist >= trigger_ist:
            days_ahead += 7
        trigger_ist += timedelta(days=days_ahead)

        sleep_seconds = max(0, (trigger_ist - now_ist).total_seconds())
        logger.info(
            f"Next knowledge weekly synthesis: {trigger_ist.strftime('%a %Y-%m-%d %H:%M IST')} "
            f"({sleep_seconds / 3600:.1f}h from now)"
        )
        await asyncio.sleep(sleep_seconds)

    async def _run(self) -> bool:
        """Run weekly synthesis. Returns True on success, False on failure (audit SC-01)."""
        logger.info("Knowledge weekly synthesis triggering")
        try:
            from processors.knowledge_synthesis import run_weekly_synthesis

            summary = await run_weekly_synthesis()

            # Clustering proposals (rate-limited inside the processor).
            try:
                from processors.topic_clustering import propose_topic_consolidation
                proposals = await propose_topic_consolidation()
                summary["proposals"] = proposals
            except Exception as e:
                logger.warning(f"Topic clustering proposals skipped (non-fatal): {e}")

            # Decision synthesis (narratives + cross-decision proposals). Rides this
            # same Sunday run, flag-gated + non-fatal, mirrors the topic pattern.
            if getattr(settings, "DECISION_SYNTHESIS_ENABLED", False):
                try:
                    from processors.decision_synthesis import run_decision_synthesis
                    from processors.decision_clustering import propose_decision_consolidation
                    summary["decision_synthesis"] = await run_decision_synthesis()
                    summary["decision_proposals"] = await propose_decision_consolidation()
                except Exception as e:
                    logger.warning(f"Decision synthesis skipped (non-fatal): {e}")

            # Heartbeat to scheduler_heartbeats (what the health checks read),
            # not audit_log. [audit P4-01]
            from services.supabase_client import supabase_client
            supabase_client.upsert_scheduler_heartbeat(
                "knowledge_weekly",
                details={k: summary.get(k) for k in
                         ("resynthesized_topics", "proposals", "decision_synthesis", "decision_proposals")},
            )
            return True
        except Exception as e:
            logger.error(f"Knowledge weekly synthesis failed: {e}")
            try:
                from services.supabase_client import supabase_client
                supabase_client.upsert_scheduler_heartbeat(
                    "knowledge_weekly", status="error", details={"error": str(e)}
                )
            except Exception:
                pass
            try:
                from core.health_monitor import check_and_alert
                await check_and_alert("knowledge_weekly", e)
            except Exception:
                pass
            return False


# Singleton instance
knowledge_weekly_scheduler = KnowledgeWeeklyScheduler()
