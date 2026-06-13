"""
Reconcile scheduler (v3 outputs re-architecture).

Two cadences, each independently flag-gated:
- TASKS (RECONCILE_ENABLED): midday + pre-nightly Sheet<->DB reconcile.
- GANTT (GANTT_RECONCILE_ENABLED): a weekly pre-digest pass (status rollup +
  timeframe read-back) just before the weekly digest, so the digest reads a
  fresh Gantt.

On-demand reconcile is via the MCP tools. Honors the *_SHADOW_MODE flags (the
engines write nothing while shadow is on).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_GANTT_TIMEOUT_S = 1800  # hard 30-min cap on the pre-digest Gantt pass


class ReconcileScheduler:
    """Tasks reconcile (midday + pre-nightly) + weekly pre-digest Gantt pass."""

    def __init__(self):
        self._running = False
        self._last_slot: str | None = None

    async def start(self) -> None:
        if self._running:
            logger.warning("Reconcile scheduler already running")
            return
        self._running = True
        logger.info(
            f"Reconcile scheduler started (tasks={settings.RECONCILE_ENABLED} "
            f"midday={settings.RECONCILE_MIDDAY_HOUR}/pre-nightly={settings.RECONCILE_PRENIGHTLY_HOUR}; "
            f"gantt={settings.GANTT_RECONCILE_ENABLED} pre-digest={settings.GANTT_PREDIGEST_HOUR} "
            f"day={settings.WEEKLY_DIGEST_DAY} IST)"
        )
        while self._running:
            try:
                slot = await self._sleep_until_next()
                if not self._running or slot is None:
                    if slot is None:
                        await asyncio.sleep(3600)  # nothing enabled; idle
                    continue
                if slot == self._last_slot:
                    await asyncio.sleep(3600)
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

    def _next_daily(self, now: datetime, hour: int) -> datetime:
        trig = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= trig:
            trig += timedelta(days=1)
        return trig

    def _next_weekly(self, now: datetime, weekday: int, hour: int) -> datetime:
        # weekday: Mon=0 .. Sun=6 (Python). Settings WEEKLY_DIGEST_DAY uses the same.
        days_ahead = (weekday - now.weekday()) % 7
        trig = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if trig <= now:
            trig += timedelta(days=7)
        return trig

    async def _sleep_until_next(self) -> str | None:
        now = datetime.now(_ISRAEL_TZ)
        candidates = []
        if settings.RECONCILE_ENABLED:
            candidates.append((self._next_daily(now, settings.RECONCILE_MIDDAY_HOUR), "midday"))
            candidates.append((self._next_daily(now, settings.RECONCILE_PRENIGHTLY_HOUR), "prenightly"))
        if settings.GANTT_RECONCILE_ENABLED:
            candidates.append((
                self._next_weekly(now, settings.WEEKLY_DIGEST_DAY, settings.GANTT_PREDIGEST_HOUR),
                "predigest",
            ))
        if not candidates:
            return None
        trig, name = min(candidates, key=lambda c: c[0])
        sleep_s = max(0, (trig - now).total_seconds())
        logger.info(f"Next reconcile ({name}): {trig.strftime('%a %Y-%m-%d %H:%M IST')} ({sleep_s/3600:.1f}h)")
        await asyncio.sleep(sleep_s)
        return f"{trig.strftime('%Y-%m-%d')}:{name}"

    async def _run(self, slot: str) -> None:
        name = slot.split(":", 1)[1]
        logger.info(f"Reconcile triggering ({slot})")
        from services.supabase_client import supabase_client
        try:
            # Heartbeat to scheduler_heartbeats (what the health checks read),
            # not audit_log. Both slots share the "reconcile" heartbeat row so the
            # loop is visible regardless of which slot last fired. [audit P4-01]
            if name == "predigest":
                await self._run_gantt()
                supabase_client.upsert_scheduler_heartbeat(
                    "reconcile", details={"slot": slot, "kind": "gantt"}
                )
            else:
                from processors.sheets_sync import reconcile_tasks
                summary = await reconcile_tasks()
                supabase_client.upsert_scheduler_heartbeat(
                    "reconcile",
                    details={"slot": slot, **(summary if isinstance(summary, dict) else {})},
                )
        except Exception as e:
            logger.error(f"Reconcile failed ({slot}): {e}")
            try:
                supabase_client.upsert_scheduler_heartbeat(
                    "reconcile", status="error", details={"slot": slot, "error": str(e)}
                )
            except Exception:
                pass
            try:
                from core.health_monitor import check_and_alert
                await check_and_alert("reconcile", e)
            except Exception:
                pass

    async def _run_gantt(self) -> None:
        """Pre-digest Gantt pass: read-back (board -> knowledge, DB-only) + nudges. Never paints the board."""
        async def _work():
            from processors.gantt_readback import reconcile_gantt_lanes
            from processors.gantt_nudge import compute_gantt_nudges
            await reconcile_gantt_lanes()      # board -> knowledge (DB-only, manual-wins)
            compute_gantt_nudges()             # brief<->board divergence -> nudges
        await asyncio.wait_for(_work(), timeout=_GANTT_TIMEOUT_S)


# Singleton instance
reconcile_scheduler = ReconcileScheduler()
