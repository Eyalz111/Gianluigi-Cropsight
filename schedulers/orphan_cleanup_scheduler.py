"""
Orphan cleanup scheduler for v0.5.

Runs daily. 4 SQL-driven detectors (no LLM):
1. Stale pending approvals (>7 days) → notify Eyal
2. Orphan embeddings (source missing) → delete + log
3. Stale manual tasks (>30 days, no meeting_id, pending) → notify Eyal
4. Failed auto-publishes (past auto_publish_at, still pending) → notify Eyal

Follows the AlertScheduler pattern.

Usage:
    from schedulers.orphan_cleanup_scheduler import orphan_cleanup_scheduler

    await orphan_cleanup_scheduler.start()
"""

import asyncio
import logging
from datetime import datetime, timedelta

from config.settings import settings
from services.supabase_client import supabase_client
from services.telegram_bot import telegram_bot

logger = logging.getLogger(__name__)


class OrphanCleanupScheduler:
    """
    Schedules daily cleanup of stale/orphan data.

    Runs on a configurable cycle (default 24h). Sends notifications
    to Eyal for items needing attention, and auto-deletes orphan embeddings.
    """

    def __init__(self, check_interval: int | None = None):
        """
        Initialize the cleanup scheduler.

        Args:
            check_interval: Seconds between runs (default from settings).
        """
        self.check_interval = check_interval or settings.ORPHAN_CLEANUP_INTERVAL
        self._running = False

    async def start(self) -> None:
        """Start the cleanup scheduler loop."""
        if self._running:
            logger.warning("Orphan cleanup scheduler already running")
            return

        self._running = True
        logger.info(f"Starting orphan cleanup scheduler (interval: {self.check_interval}s)")

        # Wait 5 minutes before first check to avoid spamming on every restart
        await asyncio.sleep(300)

        while self._running:
            try:
                await self._run_cleanup()
            except Exception as e:
                logger.error(f"Error in orphan cleanup scheduler: {e}")
                supabase_client.log_action(
                    action="orphan_cleanup_error",
                    details={"error": str(e)},
                    triggered_by="auto",
                )

            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Orphan cleanup scheduler stopped")

    async def _run_cleanup(self) -> dict:
        """
        Run all cleanup checks.

        Returns:
            Dict with counts of actions taken per detector.
        """
        logger.info("Running orphan cleanup...")

        results = {}
        notifications = []

        # 1. Stale pending approvals
        stale = self._check_stale_approvals()
        results["stale_approvals"] = len(stale)
        notifications.extend(stale)

        # 2. Orphan embeddings
        orphan_count = self._cleanup_orphan_embeddings()
        results["orphan_embeddings_deleted"] = orphan_count

        # 3. Stale manual tasks
        stale_tasks = self._check_stale_tasks()
        results["stale_tasks"] = len(stale_tasks)
        notifications.extend(stale_tasks)

        # 4. Failed auto-publishes
        failed = self._check_failed_auto_publishes()
        results["failed_auto_publishes"] = len(failed)
        notifications.extend(failed)

        # Send consolidated notification if anything found
        if notifications:
            message = self._format_notification(notifications)
            await telegram_bot.send_to_eyal(message)

        # Log to audit trail
        supabase_client.log_action(
            action="orphan_cleanup_completed",
            details=results,
            triggered_by="auto",
        )

        logger.info(f"Orphan cleanup complete: {results}")
        return results

    def _check_stale_approvals(self) -> list[dict]:
        """
        Find pending approvals older than 7 days.

        Returns:
            List of notification dicts for stale approvals.
        """
        try:
            stale = supabase_client.get_stale_pending_approvals(days=7)
            if not stale:
                return []

            notifications = []
            for approval in stale:
                notifications.append({
                    "type": "stale_approval",
                    "message": (
                        f"Pending approval '{approval.get('content_type', 'unknown')}' "
                        f"({approval.get('approval_id', 'N/A')}) is over 7 days old"
                    ),
                })
            return notifications

        except Exception as e:
            logger.error(f"Error checking stale approvals: {e}")
            return []

    def _cleanup_orphan_embeddings(self) -> int:
        """
        Find and delete embeddings whose source no longer exists.

        Returns:
            Number of orphan embeddings deleted.
        """
        try:
            orphan_ids = supabase_client.get_orphan_embedding_ids()
            if not orphan_ids:
                return 0

            deleted = supabase_client.delete_embeddings_by_ids(orphan_ids)
            logger.info(f"Cleaned up {deleted} orphan embeddings")
            return deleted

        except Exception as e:
            logger.error(f"Error cleaning orphan embeddings: {e}")
            return 0

    def _check_stale_tasks(self) -> list[dict]:
        """
        Find manually-created tasks that are >30 days old and still pending.

        Only checks tasks with no meeting_id (manually created, not extracted).

        Returns:
            List of notification dicts for stale tasks.
        """
        try:
            cutoff = (datetime.now() - timedelta(days=30)).isoformat()
            result = (
                supabase_client.client.table("tasks")
                .select("id, title, assignee, created_at")
                .eq("status", "pending")
                .is_("meeting_id", "null")
                .lt("created_at", cutoff)
                .execute()
            )

            if not result.data:
                return []

            notifications = []
            for task in result.data:
                notifications.append({
                    "type": "stale_task",
                    "message": (
                        f"Manual task '{task.get('title', 'Untitled')}' "
                        f"(assigned to {task.get('assignee', 'unassigned')}) "
                        f"is over 30 days old and still pending"
                    ),
                })
            return notifications

        except Exception as e:
            logger.error(f"Error checking stale tasks: {e}")
            return []

    def _check_failed_auto_publishes(self) -> list[dict]:
        """
        Find pending approvals whose auto_publish_at is in the past.

        These represent failed or missed auto-publishes.

        Returns:
            List of notification dicts for failed auto-publishes.
        """
        try:
            now = datetime.now().isoformat()
            result = (
                supabase_client.client.table("pending_approvals")
                .select("*")
                .eq("status", "pending")
                .not_.is_("auto_publish_at", "null")
                .lt("auto_publish_at", now)
                .execute()
            )

            if not result.data:
                return []

            notifications = []
            for approval in result.data:
                notifications.append({
                    "type": "failed_auto_publish",
                    "message": (
                        f"Auto-publish for '{approval.get('content_type', 'unknown')}' "
                        f"({approval.get('approval_id', 'N/A')}) was scheduled at "
                        f"{approval.get('auto_publish_at', 'unknown')} but is still pending"
                    ),
                })
            return notifications

        except Exception as e:
            logger.error(f"Error checking failed auto-publishes: {e}")
            return []

    @staticmethod
    def _format_notification(notifications: list[dict]) -> str:
        """
        Format cleanup notifications into a Telegram message.

        Args:
            notifications: List of notification dicts with type and message.

        Returns:
            Formatted message string.
        """
        lines = ["*Orphan Cleanup Report*\n"]

        for notif in notifications:
            emoji = {
                "stale_approval": "⏰",
                "stale_task": "📋",
                "failed_auto_publish": "❌",
            }.get(notif.get("type", ""), "⚠️")
            lines.append(f"{emoji} {notif['message']}")

        return "\n".join(lines)


# Singleton instance
orphan_cleanup_scheduler = OrphanCleanupScheduler()
