"""
Dropbox sync scheduler — Phase 13 B1.

Periodically syncs files from a configured Dropbox folder to Google Drive.
Disabled by default (DROPBOX_SYNC_ENABLED=False).

Usage:
    from schedulers.dropbox_sync_scheduler import dropbox_sync_scheduler
    await dropbox_sync_scheduler.start()
"""

import asyncio
import logging

from config.settings import settings

logger = logging.getLogger(__name__)


class DropboxSyncScheduler:
    """Periodic scheduler for Dropbox → Drive sync."""

    def __init__(self):
        self._running = False

    async def start(self) -> None:
        """Start the sync scheduler loop."""
        if not settings.DROPBOX_SYNC_ENABLED:
            logger.info("Dropbox sync scheduler disabled (DROPBOX_SYNC_ENABLED=False)")
            return

        if not settings.DROPBOX_APP_KEY or not settings.DROPBOX_REFRESH_TOKEN:
            logger.warning("Dropbox sync enabled but credentials not configured — skipping")
            return

        self._running = True
        logger.info(
            f"Dropbox sync scheduler started (interval: {settings.DROPBOX_SYNC_INTERVAL}s)"
        )

        while self._running:
            try:
                await self._run_sync()
            except Exception as e:
                logger.error(f"Dropbox sync cycle failed: {e}")

            await asyncio.sleep(settings.DROPBOX_SYNC_INTERVAL)

    async def _run_sync(self) -> None:
        """Execute one sync cycle."""
        from services.dropbox_sync import dropbox_sync_service

        result = await dropbox_sync_service.sync_folder()
        if result.get("synced", 0) > 0 or result.get("conflicts", 0) > 0:
            logger.info(f"Dropbox sync result: {result}")

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("Dropbox sync scheduler stopped")


dropbox_sync_scheduler = DropboxSyncScheduler()
