"""
Calendar-driven weekly review scheduler.

Timeline:
- T-3h: Detect calendar event, compile data, generate outputs, create session
- T-30min: Send Telegram notification with report link
- T-0: Eyal starts session via /review or natural message

Manual /review without calendar event: works without scheduler trigger.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

from config.settings import settings

logger = logging.getLogger(__name__)


class WeeklyReviewScheduler:
    """Schedules weekly review prep based on calendar events."""

    def __init__(
        self,
        check_interval: int | None = None,
    ):
        self.check_interval = check_interval or settings.WEEKLY_REVIEW_SCHEDULER_INTERVAL
        self._running = False
        self._prepped_events: set[str] = set()  # event IDs already prepped
        self._notified_events: set[str] = set()  # event IDs already notified

    async def start(self) -> None:
        """Start the scheduler loop."""
        if self._running:
            logger.warning("Weekly review scheduler already running")
            return

        self._running = True
        logger.info(
            f"Starting weekly review scheduler "
            f"(interval: {self.check_interval}s)"
        )

        # Wait for Telegram bot readiness (Phase 5 lesson)
        try:
            from services.telegram_bot import telegram_bot
            await telegram_bot.wait_until_ready()
        except Exception as e:
            logger.warning(f"Telegram readiness wait failed: {e}")

        while self._running:
            try:
                await self._check_cycle()
            except Exception as e:
                logger.error(f"Weekly review scheduler cycle error: {e}")

            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Weekly review scheduler stopped")

    async def _check_cycle(self) -> None:
        """Run one scheduler cycle: check calendar for weekly review events."""
        event = await self._find_review_event()
        if not event:
            await self._check_fallback_needed()
            return

        event_id = event.get("id", "")
        event_start = self._parse_event_time(event)
        if not event_start:
            return

        now = datetime.now(timezone.utc)
        time_until = event_start - now

        # T-3h: Prep window
        prep_threshold = timedelta(hours=settings.WEEKLY_REVIEW_PREP_HOURS)
        if time_until <= prep_threshold and event_id not in self._prepped_events:
            await self._trigger_prep(event)
            self._prepped_events.add(event_id)

        # T-30min: Notification window
        notify_threshold = timedelta(minutes=settings.WEEKLY_REVIEW_NOTIFY_MINUTES)
        if time_until <= notify_threshold and event_id not in self._notified_events:
            await self._send_notification(event_id)
            self._notified_events.add(event_id)

    async def _find_review_event(self) -> dict | None:
        """Find the next weekly review calendar event."""
        try:
            from services.google_calendar import calendar_service
            events = await calendar_service.get_upcoming_events(days=7)
        except Exception as e:
            logger.debug(f"Calendar check failed: {e}")
            return None

        for event in events:
            # Skip cancelled events
            status = event.get("status", "confirmed")
            if status == "cancelled":
                continue

            title = event.get("title", "")
            if self._is_review_event(title):
                return event

        return None

    def _is_review_event(self, title: str) -> bool:
        """
        Check if a calendar event is a weekly review.

        Uses fuzzy word matching first, then Haiku fallback for non-Latin titles.
        """
        if not title:
            return False

        # Exact match
        expected_title = settings.WEEKLY_REVIEW_CALENDAR_TITLE
        if title.lower().strip() == expected_title.lower().strip():
            return True

        # Fuzzy match: compare significant words
        try:
            from guardrails.calendar_filter import _extract_significant_words
            expected_words = _extract_significant_words(expected_title)
            title_words = _extract_significant_words(title)
            # Match if 60%+ of expected words are present
            if expected_words:
                overlap = expected_words & title_words
                if len(overlap) / len(expected_words) >= 0.6:
                    return True
        except Exception:
            pass

        # Haiku fallback for non-Latin titles
        has_non_latin = any(ord(c) > 127 for c in title)
        if has_non_latin:
            try:
                from core.llm import call_llm
                prompt = (
                    f"Is this calendar event a weekly review session?\n"
                    f"Title: {title}\n"
                    f"Expected: {expected_title}\n"
                    f"Answer YES or NO only."
                )
                text, _ = call_llm(
                    prompt=prompt,
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    call_site="weekly_review_title_match",
                )
                return "yes" in text.strip().lower()
            except Exception:
                pass

        return False

    async def _trigger_prep(self, event: dict) -> None:
        """Compile data and create session at T-3h."""
        from services.supabase_client import supabase_client

        now = datetime.now(_ISRAEL_TZ)
        week_number = now.isocalendar()[1]
        year = now.isocalendar()[0]

        event_id = event.get("id", "")

        # Check for existing session
        existing = supabase_client.get_active_weekly_review_session()
        if existing and existing.get("week_number") == week_number:
            logger.info(f"Weekly review session already exists for W{week_number}")
            return

        # Compile data
        try:
            from processors.weekly_review import compile_weekly_review_data
            agenda_data = await compile_weekly_review_data(week_number, year)
        except Exception as e:
            logger.error(f"Weekly review data compilation failed: {e}")
            return

        # Generate outputs early
        outputs = {}
        try:
            from processors.weekly_report import generate_html_report
            report_result = await generate_html_report(
                "", agenda_data, week_number, year
            )
            outputs["html_report"] = report_result
        except Exception as e:
            logger.error(f"HTML report pre-generation failed: {e}")

        try:
            from processors.gantt_slide import generate_gantt_slide
            await generate_gantt_slide(week_number, year)
            outputs["pptx"] = {"generated": True}
        except Exception as e:
            logger.error(f"PPTX pre-generation failed: {e}")

        # Create session
        session = supabase_client.create_weekly_review_session(
            week_number=week_number,
            year=year,
            status="ready",
            trigger_type="calendar",
            calendar_event_id=event_id,
            agenda_data=agenda_data,
        )

        # Link report to session
        if outputs.get("html_report", {}).get("report_id"):
            supabase_client.update_weekly_review_session(
                session["id"],
                report_id=outputs["html_report"]["report_id"],
            )

        logger.info(
            f"Weekly review prepped for W{week_number}: "
            f"session={session['id']}, event={event_id}"
        )

    async def _send_notification(self, event_id: str) -> None:
        """Send Telegram notification at T-30min, re-verifying calendar first."""
        # Re-verify calendar event still exists
        try:
            event = await self._find_review_event()
            if not event:
                from services.supabase_client import supabase_client
                session = supabase_client.get_active_weekly_review_session()
                if session:
                    supabase_client.update_weekly_review_session(
                        session["id"], status="cancelled"
                    )
                    from services.telegram_bot import telegram_bot
                    await telegram_bot.send_to_eyal(
                        "Weekly review event was removed from calendar. Session cancelled."
                    )
                return
        except Exception as e:
            logger.warning(f"Calendar re-verify failed, proceeding: {e}")

        from services.supabase_client import supabase_client
        from services.telegram_bot import telegram_bot

        session = supabase_client.get_active_weekly_review_session()
        if not session:
            return

        week_number = session.get("week_number", 0)
        report_url = ""

        # Try to get report URL
        report_id = session.get("report_id")
        if report_id:
            try:
                report = supabase_client.get_weekly_report(
                    session.get("week_number", 0),
                    session.get("year", 0),
                )
                if report and report.get("access_token"):
                    base_url = settings.REPORTS_BASE_URL.rstrip("/") if settings.REPORTS_BASE_URL else ""
                    report_url = f"{base_url}/reports/weekly/{report['access_token']}"
            except Exception:
                pass

        message = f"Weekly review for W{week_number} starts in 30 minutes."
        if report_url:
            message += f"\n\nPreview report: {report_url}"
        message += "\n\nUse /review when you're ready."

        await telegram_bot.send_to_eyal(message, parse_mode=None)
        logger.info(f"Weekly review notification sent for W{week_number}")

    async def _check_fallback_needed(self) -> None:
        """If it's review day with no event, prompt Eyal once."""
        now = datetime.now(_ISRAEL_TZ)
        if now.weekday() != settings.WEEKLY_REVIEW_DAY:
            return
        if now.hour < 10 or now.hour > 16:
            return

        from services.supabase_client import supabase_client

        # Check if already prompted today (persisted via audit_log)
        today_str = now.strftime("%Y-%m-%d")
        existing = supabase_client.get_audit_log(
            action="review_fallback_prompt", limit=1
        )
        if existing:
            for entry in existing:
                details = entry.get("details") or {}
                if details.get("date") == today_str:
                    return

        if supabase_client.get_active_weekly_review_session():
            return

        # Persist that we prompted today
        supabase_client.log_action(
            action="review_fallback_prompt",
            details={"date": today_str, "prompted_at": now.isoformat()},
            triggered_by="auto",
        )

        from services.telegram_bot import telegram_bot
        await telegram_bot.send_to_eyal(
            "No weekly review event found on your calendar today.\n"
            "Use /review to start manually, or I'll send a basic digest at end of day.",
            parse_mode=None,
        )

    def _parse_event_time(self, event: dict) -> datetime | None:
        """Parse event start time to timezone-aware datetime."""
        start = event.get("start")
        if not start:
            return None

        if isinstance(start, datetime):
            if start.tzinfo is None:
                return start.replace(tzinfo=timezone.utc)
            return start

        if isinstance(start, str):
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                return None

        return None


# Singleton instance
weekly_review_scheduler = WeeklyReviewScheduler()
