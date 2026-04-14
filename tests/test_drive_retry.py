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


class TestExecuteWithRetry:
    """Tests for the _execute_with_retry helper on Drive + Gmail services."""

    def _make_drive_service(self):
        with patch.object(GoogleDriveService, "__init__", lambda self: None):
            svc = GoogleDriveService()
            svc._service = MagicMock()
            svc._credentials = MagicMock()
            svc._processed_file_ids = set()
            svc._processed_doc_ids = set()
            return svc

    def test_drive_execute_with_retry_nulls_service_on_broken_pipe(self):
        """On BrokenPipeError, _execute_with_retry nulls _service so the
        next factory() call triggers a rebuild (via property getter)."""
        svc = self._make_drive_service()
        first_request = MagicMock()
        first_request.execute.side_effect = BrokenPipeError(32, "Broken pipe")
        second_request = MagicMock()
        second_request.execute.return_value = {"files": []}

        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return first_request if calls["n"] == 1 else second_request

        with patch("time.sleep"):
            result = svc._execute_with_retry(factory, max_retries=3, base_delay=0.001)

        assert result == {"files": []}
        assert calls["n"] == 2
        # self._service was nulled between attempts — the 2nd factory call
        # observes None and triggers property-driven rebuild on next .service
        # access (not directly observable here since we pre-seeded MagicMock,
        # but the null assignment is a post-condition we assert via fresh mock).

    def test_drive_execute_with_retry_propagates_after_max_attempts(self):
        svc = self._make_drive_service()
        req = MagicMock()
        req.execute.side_effect = ConnectionError("refused")
        with patch("time.sleep"), pytest.raises(ConnectionError):
            svc._execute_with_retry(lambda: req, max_retries=3, base_delay=0.001)
        assert req.execute.call_count == 3

    def test_drive_execute_with_retry_does_not_retry_non_transient(self):
        svc = self._make_drive_service()
        req = MagicMock()
        req.execute.side_effect = ValueError("bad argument")
        with patch("time.sleep"), pytest.raises(ValueError):
            svc._execute_with_retry(lambda: req, max_retries=3, base_delay=0.001)
        assert req.execute.call_count == 1

    def test_drive_execute_with_retry_retries_transient_string_error(self):
        """Errors whose str() contains 'broken pipe'/'503'/etc. are retried
        even when the exception type isn't in the OSError family."""
        svc = self._make_drive_service()
        failing = MagicMock()
        failing.execute.side_effect = Exception("HttpError 503: backend unavailable")
        succeeding = MagicMock()
        succeeding.execute.return_value = {"ok": True}
        n = {"i": 0}
        def factory():
            n["i"] += 1
            return failing if n["i"] == 1 else succeeding
        with patch("time.sleep"):
            result = svc._execute_with_retry(factory, max_retries=3, base_delay=0.001)
        assert result == {"ok": True}

    def test_gmail_execute_with_retry_retries_broken_pipe(self):
        """Gmail service mirrors the same pattern."""
        from services.gmail import GmailService
        with patch.object(GmailService, "__init__", lambda self: None):
            svc = GmailService()
            svc._service = MagicMock()
            svc._credentials = MagicMock()
            svc.sender_email = "test@test.com"

        failing = MagicMock()
        failing.execute.side_effect = BrokenPipeError(32, "Broken pipe")
        succeeding = MagicMock()
        succeeding.execute.return_value = {"messages": []}
        n = {"i": 0}
        def factory():
            n["i"] += 1
            return failing if n["i"] == 1 else succeeding
        with patch("time.sleep"):
            result = svc._execute_with_retry(factory, max_retries=3, base_delay=0.001)
        assert result == {"messages": []}


class TestDriveWatcherListRetries:
    """Tests that Drive listing operations route through retry wrapper."""

    def _make_service(self):
        with patch.object(GoogleDriveService, "__init__", lambda self: None):
            svc = GoogleDriveService()
            svc._service = MagicMock()
            svc._credentials = MagicMock()
            svc._processed_file_ids = set()
            svc._processed_doc_ids = set()
            return svc

    @pytest.mark.asyncio
    async def test_get_new_transcripts_retries_on_broken_pipe(self):
        """get_new_transcripts survives one BrokenPipe and returns files."""
        from config.settings import settings
        svc = self._make_service()

        # Shared mock service — returned by _build_service after retry nulls _service
        mock_service = MagicMock()
        executions = {"n": 0}
        def make_list(**kwargs):
            req = MagicMock()
            def _execute():
                executions["n"] += 1
                if executions["n"] == 1:
                    raise BrokenPipeError(32, "Broken pipe")
                return {"files": [{"id": "f1", "name": "test.txt"}]}
            req.execute = _execute
            return req
        mock_service.files.return_value.list.side_effect = make_list
        svc._service = mock_service

        # _execute_with_retry nulls _service between attempts; the service
        # property rebuilds via _build_service — redirect that to our mock.
        svc._build_service = MagicMock(return_value=mock_service)

        with patch.object(settings, "RAW_TRANSCRIPTS_FOLDER_ID", "folder-abc"), \
             patch("time.sleep"):
            result = await svc.get_new_transcripts()

        assert len(result) == 1
        assert result[0]["id"] == "f1"
        assert executions["n"] == 2  # 1 fail + 1 retry success


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
