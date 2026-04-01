"""
Tests for the evening debrief prompt scheduler (Phase 11 C4).
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, AsyncMock

from schedulers.debrief_prompt_scheduler import DebriefPromptScheduler


class TestDebriefPromptScheduler:
    """Tests for the debrief prompt scheduler."""

    def test_stop(self):
        """Stop should set _running to False."""
        s = DebriefPromptScheduler()
        s._running = True
        s.stop()
        assert s._running is False

    def test_skip_saturday(self):
        """Should skip on Saturday (Shabbat)."""
        s = DebriefPromptScheduler()
        # Mock a Saturday
        saturday = datetime(2026, 4, 4, 18, 0, tzinfo=timezone.utc)  # April 4, 2026 is a Saturday
        with patch("schedulers.debrief_prompt_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = saturday
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            assert s._should_skip_today() is True

    def test_no_skip_weekday(self):
        """Should not skip on a weekday."""
        s = DebriefPromptScheduler()
        # Mock a Wednesday
        wednesday = datetime(2026, 4, 1, 18, 0, tzinfo=timezone.utc)  # April 1, 2026 is a Wednesday
        with patch("schedulers.debrief_prompt_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = wednesday
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            assert s._should_skip_today() is False

    @pytest.mark.asyncio
    async def test_send_prompt_sends_to_eyal(self):
        """Should send the debrief prompt via Telegram."""
        s = DebriefPromptScheduler()
        mock_tg = MagicMock()
        mock_tg.send_to_eyal = AsyncMock(return_value=True)
        mock_db = MagicMock()
        mock_db.upsert_scheduler_heartbeat = MagicMock()

        with patch("schedulers.debrief_prompt_scheduler.datetime") as mock_dt, \
             patch("services.telegram_bot.telegram_bot", mock_tg), \
             patch("services.supabase_client.supabase_client", mock_db):
            # Mock a Wednesday (not Saturday)
            mock_dt.now.return_value = datetime(2026, 4, 1, 18, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            await s._send_prompt()

        mock_tg.send_to_eyal.assert_called_once()
        call_args = mock_tg.send_to_eyal.call_args
        assert "End of day" in call_args[0][0]
        assert "/debrief" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_prompt_skips_saturday(self):
        """Should not send on Saturday."""
        s = DebriefPromptScheduler()
        mock_tg = MagicMock()
        mock_tg.send_to_eyal = AsyncMock()

        with patch("schedulers.debrief_prompt_scheduler.datetime") as mock_dt, \
             patch("services.telegram_bot.telegram_bot", mock_tg):
            mock_dt.now.return_value = datetime(2026, 4, 4, 18, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            await s._send_prompt()

        mock_tg.send_to_eyal.assert_not_called()
