"""
Weekly cost-report scheduler.

Sunday morning (COST_REPORT_DAY/HOUR IST), sends Eyal a Claude-spend summary on
Telegram and archives a fuller markdown report to the CropSight Ops Drive folder.
No LLM — reads the token_usage ledger — so it works even when Claude credits are
out (which is exactly when you want a cost report). Restart-safe fire-once via an
audit_log row, mirroring weekly_pulse. Gated by COST_REPORT_ENABLED.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.settings import settings
from services.supabase_client import supabase_client
from services.orchestrator.spine import comms_spine
from processors.cost_report import build_cost_report

logger = logging.getLogger(__name__)
_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class CostReportScheduler:
    def __init__(self, check_interval: int | None = None):
        self.check_interval = check_interval or getattr(settings, "COST_REPORT_CHECK_INTERVAL", 3600)
        self._running = False
        self._sent_weeks: set[str] = set()

    @staticmethod
    def _week_key(now: datetime) -> str:
        iso = now.isocalendar()
        return f"cost_report:{iso[0]}-W{iso[1]:02d}"

    async def start(self) -> None:
        if self._running:
            logger.warning("Cost report scheduler already running")
            return
        self._running = True
        logger.info(
            f"Starting cost report scheduler (interval {self.check_interval}s, "
            f"day {settings.COST_REPORT_DAY}, hour {settings.COST_REPORT_HOUR})"
        )
        try:
            from services.telegram_bot import telegram_bot
            await telegram_bot.wait_until_ready(timeout=30)
        except Exception:
            pass
        try:
            n = await self.reconstruct_sent_weeks()
            logger.info(f"Cost report: reconstructed {n} sent-week(s) from audit_log")
        except Exception as e:
            logger.error(f"Cost report reconstruct failed: {e}")

        while self._running:
            try:
                await self._check_and_send()
                try:
                    supabase_client.upsert_scheduler_heartbeat("cost_report")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error in cost report scheduler: {e}")
                try:
                    supabase_client.upsert_scheduler_heartbeat(
                        "cost_report", status="error", details={"error": str(e)}
                    )
                except Exception:
                    pass
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        self._running = False
        logger.info("Cost report scheduler stopped")

    async def reconstruct_sent_weeks(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=5)
        try:
            rows = supabase_client.get_audit_log(action="cost_report_sent", limit=20) or []
        except Exception as e:
            logger.warning(f"cost_report reconstruct query failed: {e}")
            rows = []
        for r in rows:
            try:
                dt = datetime.fromisoformat(str(r.get("created_at")).replace("Z", "+00:00"))
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
        day = settings.COST_REPORT_DAY
        hour = settings.COST_REPORT_HOUR
        window = getattr(settings, "COST_REPORT_WINDOW_HOURS", 3)
        if now.weekday() != day or not (hour <= now.hour < hour + window):
            return False
        key = self._week_key(now)
        if key in self._sent_weeks:
            return False
        await self._send_report(key)
        return True

    async def _send_report(self, week_key: str) -> None:
        report = build_cost_report()

        # Record fire-once BEFORE sending (a missed report beats a duplicate).
        self._sent_weeks.add(week_key)
        try:
            supabase_client.log_action(
                action="cost_report_sent",
                details={"week_key": week_key, "total_7d": report.get("total_7d")},
                triggered_by="auto",
            )
        except Exception as e:
            logger.warning(f"cost_report_sent log failed (continuing): {e}")

        # Archive the fuller markdown to the CropSight Ops Drive folder.
        drive_link = ""
        try:
            from services.google_drive import drive_service
            if settings.CROPSIGHT_OPS_FOLDER_ID:
                today = datetime.now(_ISRAEL_TZ).strftime("%Y-%m-%d")
                meta = await drive_service._upload_bytes_file(
                    data=report["doc"].encode("utf-8"),
                    filename=f"CropSight Cost Report - {today}.md",
                    folder_id=settings.CROPSIGHT_OPS_FOLDER_ID,
                    mime_type="text/markdown",
                )
                drive_link = (meta or {}).get("webViewLink", "")
            else:
                logger.warning("CROPSIGHT_OPS_FOLDER_ID not set — skipping Drive archive")
        except Exception as e:
            logger.error(f"Cost report Drive archive failed (Telegram still sent): {e}")

        text = report["telegram"]
        if drive_link:
            text += f"\n\n📄 <a href=\"{drive_link}\">Full report in CropSight Ops</a>"
        await comms_spine.send_to_eyal(text, parse_mode="HTML")

    async def generate_now(self) -> dict:
        """Ad-hoc build+send (testing); bypasses the day/hour window."""
        key = self._week_key(datetime.now(_ISRAEL_TZ))
        await self._send_report(key)
        return {"week_key": key}


cost_report_scheduler = CostReportScheduler()
