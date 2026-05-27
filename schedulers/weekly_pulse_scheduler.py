"""
Weekly Pulse scheduler (v2.5 Phase 3, chunk 4).

Pushes ONE deterministic weekly report to Eyal on the configured day/hour window
(reuses WEEKLY_DIGEST_DAY for Friday; WEEKLY_PULSE_HOUR default 15:00 IST — just
after the digest slot). Gated by WEEKLY_PULSE_ENABLED in main.py.

Restart-safe fire-once: each sent pulse is logged to audit_log (weekly_pulse_sent
{week_key, week_of}); on boot the _sent_weeks set is rebuilt from those rows
(bounded to the last ~5 weeks). The fire-once key is the ISO week
(weekly_pulse:{iso_year}-W{iso_week}), so a restart anywhere inside the Friday
window does not double-send. The audit row is written BEFORE the send call —
a missed pulse is better than a duplicate.

NOT a sleep-until-next-Friday: a CHECK_INTERVAL day/hour-window loop (the digest
scheduler's pattern), so a mid-week restart can't lose the run.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

from config.settings import settings
from services.supabase_client import supabase_client
from services.orchestrator.spine import comms_spine
from processors.weekly_pulse import assemble_pulse

logger = logging.getLogger(__name__)


class WeeklyPulseScheduler:
    """Fires one weekly Pulse per ISO week within the Friday afternoon window."""

    def __init__(self, check_interval: int | None = None):
        self.check_interval = check_interval or settings.WEEKLY_PULSE_CHECK_INTERVAL
        self._running = False
        self._sent_weeks: set[str] = set()  # ISO-week fire-once keys

    @staticmethod
    def _week_key(now: datetime) -> str:
        iso = now.isocalendar()
        return f"weekly_pulse:{iso[0]}-W{iso[1]:02d}"

    async def start(self) -> None:
        if self._running:
            logger.warning("Weekly pulse scheduler already running")
            return
        self._running = True
        logger.info(
            f"Starting weekly pulse scheduler (interval {self.check_interval}s, "
            f"day {settings.WEEKLY_DIGEST_DAY}, hour {settings.WEEKLY_PULSE_HOUR}, "
            f"window {settings.WEEKLY_PULSE_WINDOW_HOURS}h)"
        )
        try:
            from services.telegram_bot import telegram_bot
            await telegram_bot.wait_until_ready(timeout=30)
        except Exception:
            pass
        try:
            n = await self.reconstruct_sent_weeks()
            logger.info(f"Weekly pulse: reconstructed {n} sent-week(s) from audit_log on startup")
        except Exception as e:
            logger.error(f"Weekly pulse reconstruct failed: {e}")

        while self._running:
            try:
                await self._check_and_send()
                try:
                    supabase_client.upsert_scheduler_heartbeat("weekly_pulse")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error in weekly pulse scheduler: {e}")
                try:
                    from core.health_monitor import check_and_alert
                    await check_and_alert("weekly_pulse_scheduler", e)
                    supabase_client.upsert_scheduler_heartbeat("weekly_pulse", status="error", details={"error": str(e)})
                except Exception:
                    pass
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Weekly pulse scheduler stopped")

    async def reconstruct_sent_weeks(self) -> int:
        """Rebuild the fire-once set from recent weekly_pulse_sent rows (bounded ~5 weeks)."""
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=5)
        try:
            rows = supabase_client.get_audit_log(action="weekly_pulse_sent", limit=20) or []
        except Exception as e:
            logger.warning(f"reconstruct_sent_weeks query failed: {e}")
            rows = []
        for r in rows:
            created = r.get("created_at")
            try:
                dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
            key = (r.get("details") or {}).get("week_key")
            if key:
                self._sent_weeks.add(key)
        return len(self._sent_weeks)

    async def _check_and_send(self) -> bool:
        now = datetime.now(_ISRAEL_TZ)
        day = settings.WEEKLY_DIGEST_DAY
        hour = settings.WEEKLY_PULSE_HOUR
        window = settings.WEEKLY_PULSE_WINDOW_HOURS
        if now.weekday() != day or not (hour <= now.hour < hour + window):
            return False
        key = self._week_key(now)
        if key in self._sent_weeks:
            return False
        week_start = now - timedelta(days=now.weekday())
        await self._send_pulse(week_start, key)
        return True

    async def _send_pulse(self, week_start: datetime, week_key: str) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        result = await assemble_pulse(week_start)
        week_of = result["week_of"]

        # Record fire-once BEFORE sending (missed pulse > duplicate pulse).
        self._sent_weeks.add(week_key)
        try:
            supabase_client.log_action(
                action="weekly_pulse_sent",
                details={"week_key": week_key, "week_of": week_of, "stale_count": result.get("stale_count")},
                triggered_by="auto",
            )
        except Exception as e:
            logger.warning(f"weekly_pulse_sent log failed (continuing): {e}")

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f4e4 Send to team", callback_data=f"weekly_pkg:{week_of}")],
            [InlineKeyboardButton("\U0001f50a Listen", callback_data="listen:1")],
        ])
        await comms_spine.send_to_eyal(result["text"], parse_mode="HTML", reply_markup=markup)

    async def generate_now(self, week_start: datetime | None = None) -> dict:
        """Manually build + send the Pulse (testing / ad-hoc); bypasses the window."""
        if week_start is None:
            now = datetime.now(_ISRAEL_TZ)
            week_start = now - timedelta(days=now.weekday())
        key = self._week_key(week_start if week_start.tzinfo else week_start.replace(tzinfo=_ISRAEL_TZ))
        await self._send_pulse(week_start, key)
        return {"week_key": key}


# Singleton instance
weekly_pulse_scheduler = WeeklyPulseScheduler()
