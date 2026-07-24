"""
Task sync scheduler — daily archival of completed tasks.

Moves completed tasks (>30 days old) from the active Tasks tab to an
Archive tab in Google Sheets. Keeps the active view clean and scannable.

Usage:
    from schedulers.task_sync_scheduler import task_sync_scheduler
    asyncio.create_task(task_sync_scheduler.start())
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class TaskSyncScheduler:
    """Daily task archival scheduler."""

    def __init__(self):
        # Run once per day (86400 seconds)
        self.check_interval = 86400
        self._running = False

    async def start(self) -> None:
        """Start the scheduler loop."""
        if not settings.TASK_ARCHIVAL_ENABLED:
            logger.info("Task archival scheduler disabled (TASK_ARCHIVAL_ENABLED=false)")
            return

        self._running = True
        logger.info("Task archival scheduler started")

        # Wait 10 minutes before first run (let other services start)
        await asyncio.sleep(600)

        while self._running:
            try:
                await self._archive_completed_tasks()
                try:
                    from services.supabase_client import supabase_client
                    supabase_client.upsert_scheduler_heartbeat("task_sync")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Task archival error: {e}")
                try:
                    from services.supabase_client import supabase_client
                    supabase_client.upsert_scheduler_heartbeat(
                        "task_sync", status="error", details={"error": str(e)}
                    )
                except Exception:
                    pass

            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Task archival scheduler stopped")

    @staticmethod
    def _older_than(iso_value, cutoff) -> bool:
        """True if a DB timestamp string is strictly older than `cutoff` (aware).

        Parses the ISO value into a tz-aware datetime (naive => UTC) so the
        comparison never depends on string offset formatting. A missing/garbage
        value returns False — we never archive on the strength of a null clock.
        """
        if not iso_value:
            return False
        try:
            dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        return dt < cutoff

    async def _archive_completed_tasks(self) -> None:
        """Flip DONE tasks untouched > N days to status 'archived'.

        We set the DB STATUS ONLY — the reconcile engine then moves the sheet row
        to the Archive tab on its next pass, reusing the UUID-keyed,
        oscillation-guarded, snapshot-aware path. This scheduler never touches the
        Sheet itself (the old title-matching version did, and never set the DB
        status, so the row left the sheet while the DB still said 'done' — a
        split-brain). Dropped MEETINGS age out to Past Meetings inside
        reconcile_meetings, which is already UUID-keyed. [2026-07-24]
        """
        from services.supabase_client import supabase_client

        days = settings.TASK_ARCHIVAL_DAYS
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            done = supabase_client.get_tasks(status="done", limit=2000)
        except Exception as e:
            logger.debug(f"Could not fetch done tasks: {e}")
            return
        moved = 0
        for t in done:
            if self._older_than(t.get("updated_at"), cutoff):
                try:
                    supabase_client.update_task(t["id"], status="archived")
                    moved += 1
                except Exception as e:
                    logger.warning(f"auto-archive task {t.get('id')} failed: {e}")
        if moved:
            logger.info(f"Auto-archived {moved} done task(s) >{days}d — reconcile will move the rows")


# Singleton
task_sync_scheduler = TaskSyncScheduler()
