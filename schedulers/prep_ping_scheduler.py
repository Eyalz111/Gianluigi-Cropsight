"""
Prep-ping scheduler (v2.5 Phase 3, chunk 3).

Push-first meeting prep: fire ONE deterministic ping ~LEAD minutes before each
CropSight meeting Eyal is in, with a [🔎 Prepare me] button for the on-demand
brief. Replaces the old outline/timeline/auto-gen machinery in
meeting_prep_scheduler.py (gated by PREP_PING_ENABLED in main.py — exactly one
runs).

Restart-safe fire-once: each sent ping is logged to audit_log (prep_ping_sent
{event_id, event_start}); on boot the _pinged set is rebuilt from those rows.
The fire-once key is (event_id | event_start), so a RESCHEDULED meeting (new
start) gets a fresh ping. pinged is recorded BEFORE sending — a missed ping is
better than a duplicate.

No timeline modes, no outline, no auto-gen, no timers.
"""

import asyncio
import logging
import math
from datetime import datetime, timezone

from config.settings import settings
from services.google_calendar import calendar_service
from services.supabase_client import supabase_client
from services.telegram_bot import telegram_bot  # for wait_until_ready
from services.orchestrator.spine import comms_spine
from guardrails.calendar_filter import is_cropsight_meeting
from processors.prep_ping import eyal_is_attendee, gather_ping_context, format_ping_text

logger = logging.getLogger(__name__)


class PrepPingScheduler:
    """Fires one prep ping per meeting at ~LEAD minutes before start."""

    def __init__(self, check_interval: int | None = None):
        self.check_interval = check_interval or settings.PREP_PING_CHECK_INTERVAL
        self._running = False
        # Fire-once keys: f"{event_id}|{event_start}" (start in the key → reschedule re-pings).
        self._pinged: set[str] = set()

    @staticmethod
    def _key(event_id: str, event_start: str | None) -> str:
        return f"{event_id}|{event_start or ''}"

    async def start(self) -> None:
        if self._running:
            logger.warning("Prep-ping scheduler already running")
            return
        self._running = True
        logger.info(
            f"Starting prep-ping scheduler (interval {self.check_interval}s, "
            f"lead {settings.PREP_PING_LEAD_MINUTES}m, floor {settings.PREP_PING_MIN_LEAD_MINUTES}m)"
        )
        try:
            await telegram_bot.wait_until_ready(timeout=30)
        except Exception:
            pass
        try:
            n = await self.reconstruct_prep_timers()
            logger.info(f"Prep-ping: reconstructed {n} sent-ping(s) from audit_log on startup")
        except Exception as e:
            logger.error(f"Prep-ping reconstruct failed: {e}")

        while self._running:
            try:
                await self._check_and_ping()
                try:
                    supabase_client.upsert_scheduler_heartbeat("prep_ping")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error in prep-ping scheduler: {e}")
                try:
                    supabase_client.log_action(
                        action="prep_ping_scheduler_error", details={"error": str(e)}, triggered_by="auto"
                    )
                    from core.health_monitor import check_and_alert
                    await check_and_alert("prep_ping_scheduler", e)
                    supabase_client.upsert_scheduler_heartbeat("prep_ping", status="error", details={"error": str(e)})
                except Exception:
                    pass
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Prep-ping scheduler stopped")

    async def reconstruct_prep_timers(self) -> int:
        """Rebuild the fire-once set from recent prep_ping_sent audit rows (restart-safe).

        Named to match the old scheduler so main.py's safety-net call works through
        one helper.
        """
        rows = supabase_client.get_recent_prep_pings(days=2)
        for d in rows:
            eid = d.get("event_id")
            if eid:
                self._pinged.add(self._key(eid, d.get("event_start")))
        return len(self._pinged)

    async def _check_and_ping(self) -> list[dict]:
        # Cover the lead window plus a little slack so meetings are caught as they
        # cross the LEAD line on a CHECK_INTERVAL cadence.
        hours_ahead = max(1, math.ceil(settings.PREP_PING_LEAD_MINUTES / 60) + 1)
        events = await calendar_service.get_events_needing_prep(hours_ahead=hours_ahead)
        if not events:
            return []

        lead = settings.PREP_PING_LEAD_MINUTES
        floor = settings.PREP_PING_MIN_LEAD_MINUTES
        now = datetime.now(timezone.utc)
        sent = []
        for event in events:
            event_id = event.get("id", "")
            start = event.get("start")
            if not event_id or not start:
                continue
            try:
                start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                minutes_until = (start_dt - now).total_seconds() / 60
            except (ValueError, TypeError):
                continue

            # Window: not too early (will catch on a later poll), not too late (floor).
            if minutes_until > lead or minutes_until < floor:
                continue
            if is_cropsight_meeting(event) is not True:
                continue
            if not eyal_is_attendee(event):
                continue
            key = self._key(event_id, start)
            if key in self._pinged:
                continue

            try:
                await self._send_ping(event, key)
                sent.append({"event_id": event_id, "status": "sent"})
            except Exception as e:
                logger.error(f"Prep-ping send failed for {event.get('title')}: {e}")
        return sent

    async def _send_ping(self, event: dict, key: str) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        ctx = gather_ping_context(event)  # deterministic, no LLM
        text = format_ping_text(ctx)

        # Record fire-once BEFORE sending (missed ping > duplicate ping).
        self._pinged.add(key)
        try:
            supabase_client.log_action(
                action="prep_ping_sent",
                details={"event_id": event.get("id"), "event_start": event.get("start"),
                         "give_up": ctx.get("give_up")},
                triggered_by="auto",
            )
        except Exception as e:
            logger.warning(f"prep_ping_sent log failed (continuing): {e}")

        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔎 Prepare me", callback_data=f"prepare_me:{event.get('id')}")]]
        )
        await comms_spine.send_to_eyal(text, parse_mode="HTML", reply_markup=markup)


# Singleton instance
prep_ping_scheduler = PrepPingScheduler()
