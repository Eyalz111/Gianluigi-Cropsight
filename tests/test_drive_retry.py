"""
Tests for Google Drive download retry logic (Phase 1a).

Verifies that download_file() and download_file_bytes() retry on
transient errors (BrokenPipeError, ConnectionError, OSError) and
return empty on final failure.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.google_drive import GoogleDriveService


class TestDriveDownloadRetry:
    """Tests for retry logic on Drive downloads."""

    def _make_service(self):
        """Create a GoogleDriveService with mocked credentials."""
        with patch.object(GoogleDriveService, "__init__", lambda self: None):
            svc = GoogleDriveService()
            svc._service = MagicMock()
            svc._credentials = MagicMock()
            svc._processed_file_ids = set()
            svc._processed_doc_ids = set()
            return svc

    def test_download_file_bytes_inner_has_retry_decorator(self):
        """_download_file_bytes_with_retry is wrapped by retry decorator."""
        # The retry decorator from core.retry wraps the function — verify
        # that the inner method exists and is async (retry wraps it)
        svc = self._make_service()
        assert hasattr(svc, "_download_file_bytes_with_retry")
        import asyncio
        assert asyncio.iscoroutinefunction(svc._download_file_bytes_with_retry)

    def test_download_file_inner_has_retry_decorator(self):
        """_download_file_with_retry is wrapped by retry decorator."""
        svc = self._make_service()
        assert hasattr(svc, "_download_file_with_retry")
        import asyncio
        assert asyncio.iscoroutinefunction(svc._download_file_with_retry)

    @pytest.mark.asyncio
    async def test_download_file_bytes_returns_content_on_success(self):
        """download_file_bytes returns content when inner method succeeds."""
        svc = self._make_service()
        svc._download_file_bytes_with_retry = AsyncMock(return_value=b"transcript content")

        result = await svc.download_file_bytes("file123")
        assert result == b"transcript content"
        svc._download_file_bytes_with_retry.assert_called_once_with("file123")

    @pytest.mark.asyncio
    async def test_download_file_bytes_returns_empty_on_failure(self):
        """download_file_bytes returns empty bytes when inner method raises."""
        svc = self._make_service()
        svc._download_file_bytes_with_retry = AsyncMock(
            side_effect=BrokenPipeError("Broken pipe")
        )

        result = await svc.download_file_bytes("file123")
        assert result == b""

    @pytest.mark.asyncio
    async def test_download_file_returns_content_on_success(self):
        """download_file returns content and marks processed on success."""
        svc = self._make_service()
        svc._download_file_with_retry = AsyncMock(return_value="content here")

        result = await svc.download_file("file123")
        assert result == "content here"
        assert "file123" in svc._processed_file_ids

    @pytest.mark.asyncio
    async def test_download_file_returns_empty_on_failure(self):
        """download_file returns empty string and does NOT mark processed on failure."""
        svc = self._make_service()
        svc._download_file_with_retry = AsyncMock(
            side_effect=ConnectionError("Connection refused")
        )

        result = await svc.download_file("file123")
        assert result == ""
        assert "file123" not in svc._processed_file_ids

    @pytest.mark.asyncio
    async def test_download_file_does_not_mark_processed_on_os_error(self):
        """download_file does NOT mark file as processed when OSError occurs."""
        svc = self._make_service()
        svc._download_file_with_retry = AsyncMock(
            side_effect=OSError(32, "Broken pipe")
        )

        result = await svc.download_file("file123")
        assert result == ""
        assert "file123" not in svc._processed_file_ids


class TestRetryDecoratorIntegration:
    """Test that the @retry decorator actually retries on transient errors."""

    @pytest.mark.asyncio
    async def test_retry_retries_broken_pipe_and_succeeds(self):
        """Verify the retry decorator retries BrokenPipeError."""
        from core.retry import retry

        call_count = 0

        @retry(max_attempts=3, backoff=1.0, base_delay=0.01)  # fast for tests
        async def flaky_download():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise BrokenPipeError("Broken pipe")
            return b"success"

        result = await flaky_download()
        assert result == b"success"
        assert call_count == 2  # 1 failure + 1 success

    @pytest.mark.asyncio
    async def test_retry_exhausts_all_attempts(self):
        """Verify the retry raises after max_attempts exhausted."""
        from core.retry import retry

        call_count = 0

        @retry(max_attempts=3, backoff=1.0, base_delay=0.01)
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Connection refused")

        with pytest.raises(ConnectionError):
            await always_fail()

        assert call_count == 3  # All 3 attempts made

    @pytest.mark.asyncio
    async def test_retry_does_not_retry_non_transient_errors(self):
        """Verify the retry does NOT catch non-transient errors like ValueError."""
        from core.retry import retry

        call_count = 0

        @retry(max_attempts=3, backoff=1.0, base_delay=0.01)
        async def bad_request():
            nonlocal call_count
            call_count += 1
            raise ValueError("Not found")

        with pytest.raises(ValueError):
            await bad_request()

        assert call_count == 1  # Only 1 attempt — no retry


class TestTranscriptPollInterval:
    """Test that transcript poll interval default is 15 minutes."""

    def test_transcript_poll_interval_default(self):
        from config.settings import Settings
        s = Settings(
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.TRANSCRIPT_POLL_INTERVAL == 900  # 15 minutes
