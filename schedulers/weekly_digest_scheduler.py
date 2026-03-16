"""
Weekly digest scheduler.

Runs hourly, fires on the configured day/hour window (default: Friday 14:00-16:00).
Generates digest, saves to Drive, sends to team via email + Telegram.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from config.settings import settings
from processors.weekly_digest import generate_weekly_digest
from services.google_drive import drive_service
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)



class WeeklyDigestScheduler:
    """
    Scheduler that generates and distributes a weekly digest.

    Checks every hour. On the configured day (default Friday) within the
    configured hour window, it generates the digest, saves it to Google Drive,
    emails the team, and sends a Telegram summary to the group chat.

    Tracks the last generated week to avoid duplicates.
    """

    def __init__(self, check_interval: int | None = None):
        """
        Initialize the weekly digest scheduler.

        Args:
            check_interval: Seconds between checks (default 1 hour).
        """
        self.check_interval = check_interval or settings.WEEKLY_DIGEST_CHECK_INTERVAL
        self._running = False
        self._last_digest_week: str | None = None  # Track to avoid duplicates

    async def start(self) -> None:
        """
        Start the weekly digest scheduler loop.

        Runs indefinitely until stop() is called.
        """
        if self._running:
            logger.warning("Weekly digest scheduler already running")
            return
        self._running = True
        logger.info(
            f"Starting weekly digest scheduler (interval: {self.check_interval}s)"
        )

        while self._running:
            try:
                await self._check_and_generate()
            except Exception as e:
                logger.error(f"Error in weekly digest scheduler: {e}")
                from core.health_monitor import check_and_alert
                await check_and_alert("weekly_digest_scheduler", e)
            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Weekly digest scheduler stopped")

    async def _check_and_generate(self) -> None:
        """
        Check if it's time to generate a digest.

        Fires on the configured day/hour window (default: Friday 14:00-16:00).
        Skips if a digest was already generated for this week.
        """
        now = datetime.now()

        digest_day = settings.WEEKLY_DIGEST_DAY
        digest_hour = settings.WEEKLY_DIGEST_HOUR
        digest_window = settings.WEEKLY_DIGEST_WINDOW_HOURS

        if now.weekday() != digest_day or not (digest_hour <= now.hour < digest_hour + digest_window):
            return

        # Calculate week_of string (Monday of this week)
        week_start = now - timedelta(days=now.weekday())
        week_of = week_start.strftime("%Y-%m-%d")

        # Skip if already generated for this week
        if self._last_digest_week == week_of:
            return

        logger.info(f"Generating weekly digest for week of {week_of}")
        await self._generate_and_distribute(week_start)
        self._last_digest_week = week_of

    async def _generate_and_distribute(self, week_start: datetime) -> dict:
        """
        Generate the digest and distribute via Drive, email, and Telegram.

        Args:
            week_start: Monday of the week to summarize.

        Returns:
            Result dict from generate_weekly_digest(), or empty dict on failure.
        """
        # Generate digest
        result = await generate_weekly_digest(week_start=week_start)
        if not result:
            logger.warning("Weekly digest generation returned empty result")
            return {}

        week_of = result.get("week_of", "unknown")
        digest_doc = result.get("digest_document", "")

        # Save to Drive (as draft — distribution waits for approval)
        filename = f"Weekly Digest - Week of {week_of}.md"
        drive_result = await drive_service.save_weekly_digest(
            content=digest_doc,
            filename=filename,
        )
        drive_link = drive_result.get("webViewLink", "") if drive_result else ""

        # Submit for Eyal's approval (instead of direct distribution)
        from guardrails.approval_flow import submit_for_approval

        digest_approval_id = f"digest-{week_of}"
        await submit_for_approval(
            content_type="weekly_digest",
            content={
                "title": f"Weekly Digest — {week_of}",
                "week_of": week_of,
                "digest_document": digest_doc,
                "drive_link": drive_link,
                "meetings_count": result.get("meetings_count", 0),
                "decisions_count": result.get("decisions_count", 0),
                "tasks_completed": result.get("tasks_completed", 0),
                "tasks_overdue": result.get("tasks_overdue", 0),
            },
            meeting_id=digest_approval_id,
        )

        # Log to audit trail (sync call — never await)
        supabase_client.log_action(
            action="weekly_digest_generated",
            details={
                "week_of": week_of,
                "meetings_count": result.get("meetings_count", 0),
                "decisions_count": result.get("decisions_count", 0),
            },
            triggered_by="auto",
        )

        return result

    async def generate_now(self, week_start: datetime | None = None) -> dict:
        """
        Manually trigger digest generation (for testing or ad-hoc use).

        Args:
            week_start: Monday of the week to summarize.
                        Defaults to the current week's Monday.

        Returns:
            Result dict from generate_weekly_digest().
        """
        if week_start is None:
            now = datetime.now()
            week_start = now - timedelta(days=now.weekday())
        return await self._generate_and_distribute(week_start)


# Singleton instance
weekly_digest_scheduler = WeeklyDigestScheduler()
