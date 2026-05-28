"""
Rollout orchestrator (v2.5 Phase 3, chunk 5).

Daily 09:00 IST tick. Walks `processors/rollout_plan.ROLLOUT_PLAN`, finds the
FIRST unapplied stage whose target_date <= today, and pings Eyal in Telegram
with a short shadow-diff summary + [✅ Apply] button. On tap, the telegram
handler calls services.cloud_run_admin.apply_env_changes → logs `rollout_applied`
→ confirms back.

Restart-safe: applied state lives in `audit_log` (action='rollout_applied' with
`details.stage_id`). Persistent: a stage's reminder fires every morning until
applied (so a missed day re-fires the next day; no Skip button by design).
Once-per-day guard via `_last_fire_date` (in-memory; conservative — the
audit_log query already prevents double-applying).

NOT a sleep-until: hourly tick + hour-of-day gate, matching the digest/pulse
schedulers' pattern.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

from config.settings import settings
from services.supabase_client import supabase_client
from services.orchestrator.spine import comms_spine
from processors.rollout_plan import ROLLOUT_PLAN

logger = logging.getLogger(__name__)


class RolloutScheduler:
    """Daily reminder for the next due rollout stage."""

    def __init__(self, check_interval: int | None = None):
        self.check_interval = check_interval or settings.ROLLOUT_CHECK_INTERVAL
        self._running = False
        self._last_fire_date: str | None = None

    async def start(self) -> None:
        if self._running:
            logger.warning("Rollout scheduler already running")
            return
        self._running = True
        logger.info(
            f"Starting rollout scheduler (interval {self.check_interval}s, "
            f"hour {settings.ROLLOUT_CHECK_HOUR} IST, {len(ROLLOUT_PLAN)} stages in plan)"
        )
        try:
            from services.telegram_bot import telegram_bot
            await telegram_bot.wait_until_ready(timeout=30)
        except Exception:
            pass

        while self._running:
            try:
                await self._check_and_remind()
                try:
                    supabase_client.upsert_scheduler_heartbeat("rollout")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error in rollout scheduler: {e}")
                try:
                    from core.health_monitor import check_and_alert
                    await check_and_alert("rollout_scheduler", e)
                    supabase_client.upsert_scheduler_heartbeat(
                        "rollout", status="error", details={"error": str(e)},
                    )
                except Exception:
                    pass
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Rollout scheduler stopped")

    # =====================================================================
    # Plan walk
    # =====================================================================

    @staticmethod
    def _applied_stage_ids() -> set[str]:
        try:
            rows = supabase_client.get_audit_log(action="rollout_applied", limit=100) or []
        except Exception as e:
            logger.warning(f"rollout_applied audit query failed: {e}")
            return set()
        out = set()
        for r in rows:
            sid = (r.get("details") or {}).get("stage_id")
            if sid:
                out.add(sid)
        return out

    def _next_due_stage(self, today: datetime) -> dict | None:
        applied = self._applied_stage_ids()
        for stage in ROLLOUT_PLAN:
            if stage.get("stage_id") in applied:
                continue
            try:
                target = datetime.strptime(stage["target_date"], "%Y-%m-%d").date()
            except (ValueError, TypeError, KeyError):
                continue
            if target > today.date():
                continue
            return stage
        return None

    @staticmethod
    def _shadow_summary(action: str | None) -> str:
        """A compact context line from audit_log for the reminder. Best-effort."""
        if not action:
            return ""
        try:
            rows = supabase_client.get_audit_log(action=action, limit=200) or []
        except Exception:
            return ""
        if not rows:
            return f"Shadow audit (<i>{action}</i>): no events yet."
        from datetime import timezone, timedelta
        now = datetime.now(timezone.utc)
        d1 = now - timedelta(days=1)
        d5 = now - timedelta(days=5)
        c24h = c5d = 0
        for r in rows:
            t = r.get("created_at")
            try:
                dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if dt >= d1:
                c24h += 1
            if dt >= d5:
                c5d += 1
        return f"Shadow audit (<i>{action}</i>): {c24h} events last 24h · {c5d} last 5 days."

    # =====================================================================
    # Tick + reminder
    # =====================================================================

    async def _check_and_remind(self) -> dict | None:
        now = datetime.now(_ISRAEL_TZ)
        if now.hour != settings.ROLLOUT_CHECK_HOUR:
            return None
        today_str = now.strftime("%Y-%m-%d")
        if self._last_fire_date == today_str:
            return None  # already fired today

        stage = self._next_due_stage(now)
        if not stage:
            self._last_fire_date = today_str  # nothing due — don't keep scanning today
            return None

        await self._send_reminder(stage)
        self._last_fire_date = today_str
        return stage

    async def _send_reminder(self, stage: dict) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        summary = self._shadow_summary(stage.get("audit_action_summary"))
        env_preview = "\n".join(
            f"  <code>{k}={v}</code>" for k, v in (stage.get("env_changes") or {}).items()
        )
        body = stage.get("description") or ""
        text = (
            f"🚦 <b>Rollout reminder — {stage['stage_id']}</b>\n"
            f"Target date: {stage.get('target_date')}\n\n"
            f"{body}\n\n"
            f"<b>Env changes on apply:</b>\n{env_preview}\n"
        )
        if summary:
            text += f"\n{summary}"

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ Apply", callback_data=f"rollout_apply:{stage['stage_id']}",
            )],
        ])
        await comms_spine.send_to_eyal(text, parse_mode="HTML", reply_markup=markup)
        try:
            supabase_client.log_action(
                action="rollout_reminder_sent",
                details={"stage_id": stage["stage_id"], "target_date": stage.get("target_date")},
                triggered_by="auto",
            )
        except Exception as e:
            logger.warning(f"rollout_reminder_sent log failed: {e}")


# Singleton
rollout_scheduler = RolloutScheduler()
