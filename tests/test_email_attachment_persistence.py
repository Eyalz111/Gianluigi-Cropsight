"""Tests for Phase 13 B3: Email attachment persistence to Drive.

Tests cover:
- EMAIL_ATTACHMENTS_FOLDER_ID setting exists
- Drive upload happens before document processing
- Drive upload failure is non-fatal
- No upload when folder ID is empty
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# =========================================================================
# Setting exists
# =========================================================================

class TestAttachmentsSetting:

    def test_setting_exists_default_empty(self):
        from config.settings import Settings
        s = Settings(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test-key",
            ANTHROPIC_API_KEY="test-key",
            OPENAI_API_KEY="test-key",
            TELEGRAM_BOT_TOKEN="test-token",
        )
        assert s.EMAIL_ATTACHMENTS_FOLDER_ID == ""

    def test_setting_can_be_configured(self):
        from config.settings import Settings
        s = Settings(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test-key",
            ANTHROPIC_API_KEY="test-key",
            OPENAI_API_KEY="test-key",
            TELEGRAM_BOT_TOKEN="test-token",
            EMAIL_ATTACHMENTS_FOLDER_ID="folder-123",
        )
        assert s.EMAIL_ATTACHMENTS_FOLDER_ID == "folder-123"


# =========================================================================
# Source code verification
# =========================================================================

class TestAttachmentPersistenceSource:

    def test_source_includes_drive_upload(self):
        """Verify email_watcher uploads attachments to Drive."""
        import inspect
        import schedulers.email_watcher as module
        source = inspect.getsource(module)
        assert "_upload_bytes_file" in source
        assert "EMAIL_ATTACHMENTS_FOLDER_ID" in source

    def test_source_upload_is_non_fatal(self):
        """Drive upload failure should be caught and logged, not raised."""
        import inspect
        import schedulers.email_watcher as module
        source = inspect.getsource(module)
        assert "Drive persistence failed" in source
        assert "non-fatal" in source

    def test_source_prefixes_filename_with_msg_id(self):
        """Uploaded filename should be prefixed with email msg_id for traceability."""
        import inspect
        import schedulers.email_watcher as module
        source = inspect.getsource(module)
        assert 'f"email_{msg_id}_{filename}"' in source


# =========================================================================
# Functional: _handle_attachments with Drive upload
# =========================================================================

class TestHandleAttachmentsWithDrive:

    @pytest.mark.asyncio
    async def test_uploads_to_drive_when_folder_configured(self):
        """When EMAIL_ATTACHMENTS_FOLDER_ID is set, attachments are uploaded."""
        mock_settings = MagicMock()
        mock_settings.EMAIL_ATTACHMENTS_FOLDER_ID = "folder-abc"
        mock_settings.GMAIL_ADDRESS = "test@test.com"
        mock_settings.EYAL_CHAT_ID = "123"

        mock_drive = AsyncMock()
        mock_drive._upload_bytes_file.return_value = {"id": "drive-file-1"}

        mock_gmail = AsyncMock()
        mock_gmail.download_attachment.return_value = b"file content here"

        mock_sc = MagicMock()
        mock_sc.create_document.return_value = {"id": "doc-1"}

        mock_embeddings = AsyncMock()
        mock_embeddings.chunk_and_embed_document.return_value = []

        with patch("schedulers.email_watcher.gmail_service", mock_gmail), \
             patch("schedulers.email_watcher.supabase_client", mock_sc), \
             patch("services.google_drive.drive_service", mock_drive), \
             patch("config.settings.settings", mock_settings), \
             patch("services.embeddings.embedding_service", mock_embeddings):

            from schedulers.email_watcher import EmailWatcher
            watcher = EmailWatcher.__new__(EmailWatcher)

            msg = {
                "attachments": [
                    {"filename": "report.txt", "attachmentId": "att-1", "size": 1000},
                ],
            }

            await watcher._handle_attachments("msg-1", msg, "Roye")

            # Verify Drive upload was attempted
            mock_drive._upload_bytes_file.assert_called_once()
            call_kwargs = mock_drive._upload_bytes_file.call_args[1]
            assert call_kwargs["folder_id"] == "folder-abc"
            assert "email_msg-1_report.txt" in call_kwargs["filename"]

    @pytest.mark.asyncio
    async def test_no_upload_when_folder_empty(self):
        """When EMAIL_ATTACHMENTS_FOLDER_ID is empty, skip Drive upload."""
        mock_settings = MagicMock()
        mock_settings.EMAIL_ATTACHMENTS_FOLDER_ID = ""

        mock_gmail = AsyncMock()
        mock_gmail.download_attachment.return_value = b"file content"

        mock_sc = MagicMock()
        mock_sc.create_document.return_value = {"id": "doc-1"}

        mock_embeddings = AsyncMock()
        mock_embeddings.chunk_and_embed_document.return_value = []

        with patch("schedulers.email_watcher.gmail_service", mock_gmail), \
             patch("schedulers.email_watcher.supabase_client", mock_sc), \
             patch("config.settings.settings", mock_settings), \
             patch("services.embeddings.embedding_service", mock_embeddings):

            from schedulers.email_watcher import EmailWatcher
            watcher = EmailWatcher.__new__(EmailWatcher)

            msg = {
                "attachments": [
                    {"filename": "doc.txt", "attachmentId": "att-1"},
                ],
            }

            # Should proceed without error (no Drive upload)
            await watcher._handle_attachments("msg-2", msg, "Paolo")

            # Document should still be created
            mock_sc.create_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_drive_failure_non_fatal(self):
        """Drive upload failure should not prevent document ingestion."""
        mock_settings = MagicMock()
        mock_settings.EMAIL_ATTACHMENTS_FOLDER_ID = "folder-abc"

        mock_drive = AsyncMock()
        mock_drive._upload_bytes_file.side_effect = Exception("Drive API error")

        mock_gmail = AsyncMock()
        mock_gmail.download_attachment.return_value = b"file content"

        mock_sc = MagicMock()
        mock_sc.create_document.return_value = {"id": "doc-1"}

        mock_embeddings = AsyncMock()
        mock_embeddings.chunk_and_embed_document.return_value = []

        with patch("schedulers.email_watcher.gmail_service", mock_gmail), \
             patch("schedulers.email_watcher.supabase_client", mock_sc), \
             patch("services.google_drive.drive_service", mock_drive), \
             patch("config.settings.settings", mock_settings), \
             patch("services.embeddings.embedding_service", mock_embeddings):

            from schedulers.email_watcher import EmailWatcher
            watcher = EmailWatcher.__new__(EmailWatcher)

            msg = {
                "attachments": [
                    {"filename": "data.txt", "attachmentId": "att-1"},
                ],
            }

            # Should not raise
            await watcher._handle_attachments("msg-3", msg, "Eyal")

            # Document should still be created despite Drive failure
            mock_sc.create_document.assert_called_once()
