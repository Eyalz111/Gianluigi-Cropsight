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
from datetime import datetime, timedelta
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

    async def _archive_completed_tasks(self) -> None:
        """Move completed tasks older than N days to Archive tab."""
        from services.supabase_client import supabase_client
        from services.google_sheets import sheets_service

        archival_days = settings.TASK_ARCHIVAL_DAYS

        # Get completed tasks older than threshold
        cutoff = (datetime.now(_ISRAEL_TZ) - timedelta(days=archival_days)).isoformat()
        try:
            all_tasks = supabase_client.get_tasks(status="done")
            old_completed = [
                t for t in all_tasks
                if t.get("updated_at", "") < cutoff
            ]
        except Exception as e:
            logger.debug(f"Could not fetch completed tasks: {e}")
            return

        if not old_completed:
            logger.debug("No completed tasks to archive")
            return

        logger.info(f"Found {len(old_completed)} completed tasks to archive (>{archival_days} days)")

        # Archive to Sheets
        try:
            archived = await sheets_service.archive_completed_tasks(
                [t.get("title", "") for t in old_completed]
            )
            if archived:
                logger.info(f"Archived {archived} tasks to Sheets Archive tab")
        except Exception as e:
            logger.error(f"Sheets archival failed: {e}")


# Singleton
task_sync_scheduler = TaskSyncScheduler()
