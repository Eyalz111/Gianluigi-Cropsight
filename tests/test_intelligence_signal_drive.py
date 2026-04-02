"""Tests for intelligence signal Drive service methods."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from services.google_drive import GoogleDriveService


@pytest.fixture
def drive():
    svc = GoogleDriveService.__new__(GoogleDriveService)
    svc._service = MagicMock()
    svc._credentials = MagicMock()
    svc._processed_file_ids = set()
    svc._processed_doc_ids = set()
    return svc


class TestCreateSubfolder:
    @pytest.mark.asyncio
    async def test_creates_folder(self, drive):
        drive._service.files.return_value.create.return_value.execute.return_value = {
            "id": "folder-123",
            "name": "Intelligence Signals",
            "webViewLink": "https://drive.google.com/folder/123",
        }

        result = await drive.create_subfolder(
            name="Intelligence Signals",
            parent_folder_id="parent-456",
        )

        assert result["id"] == "folder-123"
        assert result["name"] == "Intelligence Signals"

        # Verify correct mime type was used
        call_args = drive._service.files.return_value.create.call_args
        body = call_args.kwargs.get("body") or call_args[1].get("body")
        assert body["mimeType"] == "application/vnd.google-apps.folder"

    @pytest.mark.asyncio
    async def test_handles_error(self, drive):
        drive._service.files.return_value.create.return_value.execute.side_effect = Exception("API error")

        result = await drive.create_subfolder("test", "parent-id")

        assert result == {}


class TestSaveIntelligenceSignal:
    @pytest.mark.asyncio
    async def test_saves_as_google_doc(self, drive):
        with patch("services.google_drive.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_FOLDER_ID = "signal-folder-123"

            # Mock _upload_as_google_doc directly
            with patch.object(
                drive, "_upload_as_google_doc", new_callable=AsyncMock
            ) as mock_upload:
                mock_upload.return_value = {
                    "id": "doc-789",
                    "name": "CropSight Intelligence Signal W14-2026",
                    "webViewLink": "https://docs.google.com/doc/789",
                }

                result = await drive.save_intelligence_signal(
                    content="## Test Signal\nContent here.",
                    filename="CropSight Intelligence Signal W14-2026",
                )

        assert result["id"] == "doc-789"
        assert "webViewLink" in result

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_folder_id(self, drive):
        with patch("services.google_drive.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_FOLDER_ID = ""

            result = await drive.save_intelligence_signal("content", "filename")

        assert result == {}

    @pytest.mark.asyncio
    async def test_strips_md_extension(self, drive):
        with patch("services.google_drive.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_FOLDER_ID = "folder-id"

            with patch.object(
                drive, "_upload_as_google_doc", new_callable=AsyncMock
            ) as mock_upload:
                mock_upload.return_value = {"id": "doc-123"}

                await drive.save_intelligence_signal("content", "signal.md")

                # Should strip .md
                mock_upload.assert_called_once()
                call_kwargs = mock_upload.call_args.kwargs
                assert call_kwargs["filename"] == "signal"


class TestSaveIntelligenceSignalVideo:
    @pytest.mark.asyncio
    async def test_saves_video(self, drive):
        with patch("services.google_drive.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_FOLDER_ID = "signal-folder-123"

            with patch.object(
                drive, "_upload_bytes_file", new_callable=AsyncMock
            ) as mock_upload:
                mock_upload.return_value = {
                    "id": "video-456",
                    "name": "signal-w14.mp4",
                    "webViewLink": "https://drive.google.com/video/456",
                }

                result = await drive.save_intelligence_signal_video(
                    data=b"fake-video-bytes",
                    filename="signal-w14.mp4",
                )

        assert result["id"] == "video-456"
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        assert call_kwargs["mime_type"] == "video/mp4"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_folder_id(self, drive):
        with patch("services.google_drive.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_FOLDER_ID = ""

            result = await drive.save_intelligence_signal_video(
                data=b"fake", filename="test.mp4"
            )

        assert result == {}
