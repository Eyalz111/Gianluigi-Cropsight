"""
Tests for Track E+F: Telegram UX and Production Hardening.

Tests cover:
- /search command with -m and -d flags
- /meetings browser command
- /status dashboard command
- Retry decorator
- Error alerting with deduplication
"""

import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock


# =========================================================================
# Test /search command enhancements
# =========================================================================

class TestSearchCommand:
    """Tests for enhanced /search command."""

    @pytest.mark.asyncio
    async def test_search_no_args_shows_usage(self):
        """Should show usage when no args provided."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()

        update = MagicMock()
        update.effective_chat.id = 123
        context = MagicMock()
        context.args = []

        await bot._handle_search(update, context)

        bot.send_message.assert_called_once()
        msg = bot.send_message.call_args[0][1]
        assert "Usage" in msg
        assert "-m" in msg
        assert "-d" in msg

    @pytest.mark.asyncio
    async def test_search_with_m_flag_filters_meetings(self):
        """Should pass source_type='meeting' when -m flag used."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()
        bot._get_user_id = MagicMock(return_value="eyal")

        update = MagicMock()
        update.effective_chat.id = 123
        update.effective_user.id = 123
        context = MagicMock()
        context.args = ["-m", "Moldova"]

        mock_embed = MagicMock()
        mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
        mock_db = MagicMock()
        mock_db.search_embeddings.return_value = []

        with (
            patch("services.embeddings.embedding_service", mock_embed),
            patch("services.supabase_client.supabase_client", mock_db),
        ):
            await bot._handle_search(update, context)

            mock_db.search_embeddings.assert_called_once()
            call_kwargs = mock_db.search_embeddings.call_args.kwargs
            assert call_kwargs["source_type"] == "meeting"

    @pytest.mark.asyncio
    async def test_search_with_d_flag_filters_documents(self):
        """Should pass source_type='document' when -d flag used."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()
        bot._get_user_id = MagicMock(return_value="eyal")

        update = MagicMock()
        update.effective_chat.id = 123
        update.effective_user.id = 123
        context = MagicMock()
        context.args = ["-d", "pitch", "deck"]

        mock_embed = MagicMock()
        mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
        mock_db = MagicMock()
        mock_db.search_embeddings.return_value = []

        with (
            patch("services.embeddings.embedding_service", mock_embed),
            patch("services.supabase_client.supabase_client", mock_db),
        ):
            await bot._handle_search(update, context)

            call_kwargs = mock_db.search_embeddings.call_args.kwargs
            assert call_kwargs["source_type"] == "document"

    @pytest.mark.asyncio
    async def test_search_formats_results(self):
        """Should format search results with titles and excerpts."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()

        update = MagicMock()
        update.effective_chat.id = 123
        update.effective_user.id = 123
        context = MagicMock()
        context.args = ["Moldova"]

        mock_embed = MagicMock()
        mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
        mock_db = MagicMock()
        mock_db.search_embeddings.return_value = [
            {"source_id": "m1", "source_type": "meeting", "chunk_text": "Discussion about Moldova pilot", "similarity": 0.9},
        ]
        mock_db.get_meeting.return_value = {"title": "Team Standup", "date": "2026-02-20T00:00:00"}

        with (
            patch("services.embeddings.embedding_service", mock_embed),
            patch("services.supabase_client.supabase_client", mock_db),
        ):
            await bot._handle_search(update, context)

            # Should have sent 2 messages (searching... + results)
            assert bot.send_message.call_count == 2
            result_msg = bot.send_message.call_args_list[1][0][1]
            assert "Team Standup" in result_msg


# =========================================================================
# Test /meetings command
# =========================================================================

class TestMeetingsCommand:
    """Tests for /meetings command."""

    @pytest.mark.asyncio
    async def test_meetings_lists_recent(self):
        """Should list recent meetings."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()

        update = MagicMock()
        update.effective_chat.id = 123
        context = MagicMock()
        context.args = []

        mock_db = MagicMock()
        mock_db.list_meetings.return_value = [
            {"title": "MVP Focus", "date": "2026-02-22T00:00:00", "participants": ["Eyal", "Roye"], "approval_status": "approved"},
            {"title": "BD Review", "date": "2026-02-20T00:00:00", "participants": ["Eyal", "Paolo"], "approval_status": "approved"},
        ]

        with patch("services.supabase_client.supabase_client", mock_db):
            await bot._handle_meetings(update, context)

        msg = bot.send_message.call_args[0][1]
        assert "MVP Focus" in msg
        assert "BD Review" in msg
        assert "Recent Meetings" in msg

    @pytest.mark.asyncio
    async def test_meetings_search_by_title(self):
        """Should search meetings by title when args provided."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()

        update = MagicMock()
        update.effective_chat.id = 123
        context = MagicMock()
        context.args = ["MVP"]

        mock_db = MagicMock()
        mock_db.list_meetings.return_value = [
            {"title": "MVP Focus", "date": "2026-02-22T00:00:00", "participants": ["Eyal"], "approval_status": "approved"},
            {"title": "BD Review", "date": "2026-02-20T00:00:00", "participants": ["Paolo"], "approval_status": "approved"},
        ]

        with patch("services.supabase_client.supabase_client", mock_db):
            await bot._handle_meetings(update, context)

        msg = bot.send_message.call_args[0][1]
        assert "MVP Focus" in msg
        assert "BD Review" not in msg
        assert "matching" in msg

    @pytest.mark.asyncio
    async def test_meetings_empty(self):
        """Should show no meetings message."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()

        update = MagicMock()
        update.effective_chat.id = 123
        context = MagicMock()
        context.args = []

        mock_db = MagicMock()
        mock_db.list_meetings.return_value = []

        with patch("services.supabase_client.supabase_client", mock_db):
            await bot._handle_meetings(update, context)

        msg = bot.send_message.call_args[0][1]
        assert "No meetings found" in msg


# =========================================================================
# Test /status command
# =========================================================================

class TestStatusCommand:
    """Tests for /status dashboard command."""

    @pytest.mark.asyncio
    async def test_status_admin_only(self):
        """Should reject non-admin users."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()
        bot._is_admin = MagicMock(return_value=False)

        update = MagicMock()
        update.effective_chat.id = 123
        update.effective_user.id = 999
        context = MagicMock()

        await bot._handle_status(update, context)

        msg = bot.send_message.call_args[0][1]
        assert "Only Eyal" in msg

    @pytest.mark.asyncio
    async def test_status_shows_metrics(self):
        """Should show system metrics for admin."""
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot.send_message = AsyncMock()
        bot._is_admin = MagicMock(return_value=True)

        update = MagicMock()
        update.effective_chat.id = 123
        update.effective_user.id = 1
        context = MagicMock()

        mock_db = MagicMock()
        mock_db.list_meetings.return_value = [
            {"date": "2026-02-22T00:00:00"},
            {"date": "2026-02-20T00:00:00"},
        ]
        mock_db.get_tasks.return_value = [
            {"status": "pending", "deadline": "2026-01-01"},
            {"status": "pending", "deadline": "2030-01-01"},
            {"status": "completed", "deadline": None},
        ]
        mock_db.get_commitments.return_value = [
            {"created_at": "2026-01-01T00:00:00"},
            {"created_at": datetime.now().isoformat()},
        ]
        mock_db.list_documents.return_value = [{"id": "d1"}]
        mock_db.client.table.return_value.select.return_value.gte.return_value.execute.return_value = MagicMock(
            data=[{"input_tokens": 1000, "output_tokens": 200}]
        )

        with (
            patch("services.supabase_client.supabase_client", mock_db),
            patch("config.settings.settings") as mock_settings,
        ):
            mock_settings.ENVIRONMENT = "development"

            await bot._handle_status(update, context)

        msg = bot.send_message.call_args[0][1]
        assert "System snapshot" in msg
        assert "2 meetings processed" in msg
        assert "1 documents ingested" in msg
        assert "development" in msg


# =========================================================================
# Test retry decorator
# =========================================================================

class TestRetryDecorator:
    """Tests for retry decorator."""

    @pytest.mark.asyncio
    async def test_async_succeeds_first_try(self):
        """Should return result on first success."""
        from core.retry import retry

        call_count = 0

        @retry(max_attempts=3)
        async def good_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await good_func()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_retries_on_connection_error(self):
        """Should retry on ConnectionError."""
        from core.retry import retry

        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("connection lost")
            return "recovered"

        result = await flaky_func()
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_async_raises_after_max_attempts(self):
        """Should raise after max attempts exceeded."""
        from core.retry import retry

        @retry(max_attempts=2, base_delay=0.01)
        async def always_fails():
            raise TimeoutError("timeout")

        with pytest.raises(TimeoutError):
            await always_fails()

    @pytest.mark.asyncio
    async def test_async_does_not_retry_non_transient(self):
        """Should NOT retry on non-transient errors (ValueError, etc.)."""
        from core.retry import retry

        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        async def bad_request():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            await bad_request()

        assert call_count == 1  # No retries

    def test_sync_retries(self):
        """Should work with sync functions too."""
        from core.retry import retry

        call_count = 0

        @retry(max_attempts=3, base_delay=0.01)
        def flaky_sync():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("fail")
            return "ok"

        result = flaky_sync()
        assert result == "ok"
        assert call_count == 2


# =========================================================================
# Test error alerting
# =========================================================================

class TestErrorAlerting:
    """Tests for critical error alerting."""

    @pytest.mark.asyncio
    async def test_sends_telegram_alert(self):
        """Should send Telegram message on critical error."""
        from core.error_alerting import alert_critical_error, clear_alert_history
        clear_alert_history()

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock(return_value=True)
        mock_db = MagicMock()

        with (
            patch("services.telegram_bot.telegram_bot", mock_bot),
            patch("services.supabase_client.supabase_client", mock_db),
        ):
            result = await alert_critical_error(
                component="transcript_pipeline",
                error_message="API auth expired",
            )

            assert result is True
            mock_bot.send_to_eyal.assert_called_once()
            msg = mock_bot.send_to_eyal.call_args[0][0]
            assert "transcript_pipeline" in msg
            assert "API auth expired" in msg

    @pytest.mark.asyncio
    async def test_deduplicates_within_window(self):
        """Should not re-alert for same error within 1 hour."""
        from core.error_alerting import alert_critical_error, clear_alert_history
        clear_alert_history()

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock(return_value=True)
        mock_db = MagicMock()

        with (
            patch("services.telegram_bot.telegram_bot", mock_bot),
            patch("services.supabase_client.supabase_client", mock_db),
        ):
            # First alert should send
            result1 = await alert_critical_error("pipeline", "error A")
            assert result1 is True

            # Same error again should be deduplicated
            result2 = await alert_critical_error("pipeline", "error A")
            assert result2 is False

            assert mock_bot.send_to_eyal.call_count == 1

    @pytest.mark.asyncio
    async def test_different_errors_not_deduplicated(self):
        """Should alert for different errors."""
        from core.error_alerting import alert_critical_error, clear_alert_history
        clear_alert_history()

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock(return_value=True)
        mock_db = MagicMock()

        with (
            patch("services.telegram_bot.telegram_bot", mock_bot),
            patch("services.supabase_client.supabase_client", mock_db),
        ):
            await alert_critical_error("pipeline", "error A")
            await alert_critical_error("pipeline", "error B")

            assert mock_bot.send_to_eyal.call_count == 2

    @pytest.mark.asyncio
    async def test_logs_to_audit_trail(self):
        """Should log critical_error to audit trail."""
        from core.error_alerting import alert_critical_error, clear_alert_history
        clear_alert_history()

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock(return_value=True)
        mock_db = MagicMock()

        with (
            patch("services.telegram_bot.telegram_bot", mock_bot),
            patch("services.supabase_client.supabase_client", mock_db),
        ):
            await alert_critical_error("db_connection", "connection refused")

            mock_db.log_action.assert_called_once()
            call_kwargs = mock_db.log_action.call_args.kwargs
            assert call_kwargs["action"] == "critical_error"
            assert call_kwargs["details"]["component"] == "db_connection"

    @pytest.mark.asyncio
    async def test_telegram_failure_does_not_raise(self):
        """Should not raise even if Telegram send fails."""
        from core.error_alerting import alert_critical_error, clear_alert_history
        clear_alert_history()

        mock_bot = MagicMock()
        mock_bot.send_to_eyal = AsyncMock(side_effect=Exception("Telegram down"))
        mock_db = MagicMock()

        with (
            patch("services.telegram_bot.telegram_bot", mock_bot),
            patch("services.supabase_client.supabase_client", mock_db),
        ):
            result = await alert_critical_error("test", "error")
            assert result is True  # Still returns True (alert was attempted)


# =========================================================================
# Test _escape_markdown
# =========================================================================

class TestEscapeMarkdown:
    """Tests for Markdown escaping helper."""

    def test_escapes_special_chars(self):
        """Should escape Markdown special characters."""
        from services.telegram_bot import _escape_markdown

        assert _escape_markdown("*bold*") == "\\*bold\\*"
        assert _escape_markdown("_italic_") == "\\_italic\\_"
        assert _escape_markdown("`code`") == "\\`code\\`"
        assert _escape_markdown("[link]") == "\\[link]"

    def test_plain_text_unchanged(self):
        """Should leave plain text unchanged."""
        from services.telegram_bot import _escape_markdown

        assert _escape_markdown("Hello world") == "Hello world"
