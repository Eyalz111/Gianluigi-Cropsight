"""Tests for Phase 13 B1/B5: Dropbox sync + dedup/conflict handling.

Tests cover:
- Settings configuration
- DropboxSyncService initialization
- Sync logic (mocked — no real Dropbox SDK required)
- Hash-based skip (dedup)
- Scheduler enable/disable
- Sync record CRUD
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# =========================================================================
# Settings
# =========================================================================

class TestDropboxSettings:

    def test_defaults_disabled(self):
        from config.settings import Settings
        s = Settings(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test-key",
            ANTHROPIC_API_KEY="test-key",
            OPENAI_API_KEY="test-key",
            TELEGRAM_BOT_TOKEN="test-token",
        )
        assert s.DROPBOX_SYNC_ENABLED is False
        assert s.DROPBOX_APP_KEY == ""
        assert s.DROPBOX_REFRESH_TOKEN == ""
        assert s.DROPBOX_SYNC_INTERVAL == 7200

    def test_can_be_configured(self):
        from config.settings import Settings
        s = Settings(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test-key",
            ANTHROPIC_API_KEY="test-key",
            OPENAI_API_KEY="test-key",
            TELEGRAM_BOT_TOKEN="test-token",
            DROPBOX_SYNC_ENABLED=True,
            DROPBOX_APP_KEY="app-key",
            DROPBOX_REFRESH_TOKEN="refresh-token",
            DROPBOX_SYNC_FOLDER="/CropSight BD",
            DROPBOX_MIRROR_DRIVE_FOLDER_ID="drive-folder-id",
        )
        assert s.DROPBOX_SYNC_ENABLED is True
        assert s.DROPBOX_APP_KEY == "app-key"


# =========================================================================
# DropboxSyncService
# =========================================================================

class TestDropboxSyncService:

    @patch("services.dropbox_sync.settings")
    def test_no_credentials_raises(self, mock_settings):
        mock_settings.DROPBOX_APP_KEY = ""
        mock_settings.DROPBOX_REFRESH_TOKEN = ""

        from services.dropbox_sync import DropboxSyncService
        svc = DropboxSyncService()

        with pytest.raises(RuntimeError, match="credentials not configured"):
            svc._get_client()

    @patch("services.dropbox_sync.settings")
    async def test_sync_no_folder_returns_zeros(self, mock_settings):
        mock_settings.DROPBOX_SYNC_FOLDER = ""
        mock_settings.DROPBOX_MIRROR_DRIVE_FOLDER_ID = ""

        from services.dropbox_sync import DropboxSyncService
        svc = DropboxSyncService()

        result = await svc.sync_folder()
        assert result["synced"] == 0
        assert result["skipped"] == 0

    @patch("services.dropbox_sync.supabase_client")
    @patch("services.dropbox_sync.settings")
    def test_get_sync_record(self, mock_settings, mock_sc):
        mock_sc.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"dropbox_file_id": "dbx-1", "content_hash": "abc"}]
        )

        from services.dropbox_sync import DropboxSyncService
        svc = DropboxSyncService()

        record = svc._get_sync_record("dbx-1")
        assert record is not None
        assert record["content_hash"] == "abc"

    @patch("services.dropbox_sync.supabase_client")
    @patch("services.dropbox_sync.settings")
    def test_get_sync_record_not_found(self, mock_settings, mock_sc):
        mock_sc.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[]
        )

        from services.dropbox_sync import DropboxSyncService
        svc = DropboxSyncService()

        record = svc._get_sync_record("nonexistent")
        assert record is None

    @patch("services.dropbox_sync.supabase_client")
    @patch("services.dropbox_sync.settings")
    def test_upsert_sync_record(self, mock_settings, mock_sc):
        mock_sc.client.table.return_value.upsert.return_value.execute.return_value = MagicMock()

        from services.dropbox_sync import DropboxSyncService
        svc = DropboxSyncService()

        # Should not raise
        svc._upsert_sync_record(
            dropbox_file_id="dbx-1",
            drive_file_id="drv-1",
            dropbox_path="/test/file.pdf",
            drive_path="file.pdf",
            content_hash="hash123",
        )

    @pytest.mark.asyncio
    @patch("services.dropbox_sync.supabase_client")
    @patch("services.dropbox_sync.settings")
    async def test_sync_file_skips_same_hash(self, mock_settings, mock_sc):
        """File with same content hash should be skipped."""
        from services.dropbox_sync import DropboxSyncService
        svc = DropboxSyncService()

        # Mock existing sync record with matching hash
        svc._get_sync_record = MagicMock(return_value={
            "dropbox_file_id": "dbx-1",
            "content_hash": "same-hash",
        })

        entry = {"id": "dbx-1", "name": "file.pdf", "path": "/test/file.pdf", "content_hash": "same-hash"}
        result = await svc._sync_file(MagicMock(), entry, "folder-id")
        assert result == "skipped"

    @pytest.mark.asyncio
    @patch("services.google_drive.drive_service")
    @patch("services.dropbox_sync.supabase_client")
    @patch("services.dropbox_sync.settings")
    async def test_sync_file_downloads_and_uploads(self, mock_settings, mock_sc, mock_drive):
        """New file should be downloaded from Dropbox and uploaded to Drive."""
        from services.dropbox_sync import DropboxSyncService
        svc = DropboxSyncService()

        # No existing sync record
        svc._get_sync_record = MagicMock(return_value=None)
        svc._upsert_sync_record = MagicMock()

        # Mock Dropbox download
        mock_dbx = MagicMock()
        mock_response = MagicMock()
        mock_response.content = b"file bytes"
        mock_dbx.files_download.return_value = (MagicMock(), mock_response)

        # Mock Drive upload
        mock_drive._upload_bytes_file = AsyncMock(return_value={"id": "drv-1"})

        entry = {"id": "dbx-1", "name": "report.pdf", "path": "/CropSight/report.pdf", "content_hash": "new-hash"}
        result = await svc._sync_file(mock_dbx, entry, "folder-id")

        assert result == "synced"
        mock_dbx.files_download.assert_called_once_with("/CropSight/report.pdf")
        mock_drive._upload_bytes_file.assert_called_once()
        svc._upsert_sync_record.assert_called_once()


# =========================================================================
# Scheduler
# =========================================================================

class TestDropboxSyncScheduler:

    @pytest.mark.asyncio
    @patch("schedulers.dropbox_sync_scheduler.settings")
    async def test_disabled_by_default(self, mock_settings):
        mock_settings.DROPBOX_SYNC_ENABLED = False

        from schedulers.dropbox_sync_scheduler import DropboxSyncScheduler
        scheduler = DropboxSyncScheduler()

        # Should return immediately without running
        await scheduler.start()
        assert not scheduler._running

    @pytest.mark.asyncio
    @patch("schedulers.dropbox_sync_scheduler.settings")
    async def test_no_credentials_skips(self, mock_settings):
        mock_settings.DROPBOX_SYNC_ENABLED = True
        mock_settings.DROPBOX_APP_KEY = ""
        mock_settings.DROPBOX_REFRESH_TOKEN = ""

        from schedulers.dropbox_sync_scheduler import DropboxSyncScheduler
        scheduler = DropboxSyncScheduler()

        await scheduler.start()
        assert not scheduler._running

    def test_stop_sets_flag(self):
        from schedulers.dropbox_sync_scheduler import DropboxSyncScheduler
        scheduler = DropboxSyncScheduler()
        scheduler._running = True
        scheduler.stop()
        assert not scheduler._running


# =========================================================================
# Migration: dropbox_drive_sync table
# =========================================================================

class TestDropboxMigration:

    def test_migration_includes_sync_table(self):
        with open("scripts/migrate_v2_phase13.sql") as f:
            content = f.read()
        assert "dropbox_drive_sync" in content
        assert "dropbox_file_id" in content
        assert "drive_file_id" in content
        assert "sync_status" in content
