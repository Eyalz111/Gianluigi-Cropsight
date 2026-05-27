"""
Tests for PR3 morning-brief engagement instrumentation:
- the SYNC supabase feedback helpers (restart-safe, same-day suffix, variant filter)
- the keyboard builder
- the Telegram callback handlers (parse + sync writes + recompute)
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import processors.morning_brief as mb
from services.supabase_client import supabase_client


@contextmanager
def _mock_db(mock_client):
    """Patch the lazy `client` property on the SupabaseClient class."""
    with patch.object(type(supabase_client), "client", new_callable=PropertyMock) as p:
        p.return_value = mock_client
        yield


# =========================================================================
# SYNC supabase helpers
# =========================================================================

class TestFeedbackSupabaseMethods:
    def test_create_row_appends_suffix_on_same_day(self):
        mock_client = MagicMock()
        # a row already exists for today's base id -> expect "-2"
        (mock_client.table.return_value.select.return_value
            .like.return_value.execute.return_value).data = [{"brief_id": "brief-2026-05-25"}]
        with _mock_db(mock_client):
            new_id = supabase_client.create_brief_feedback_row(
                "brief-2026-05-25", brief_date="2026-05-25", variant="primary", section_count=5
            )
        assert new_id == "brief-2026-05-25-2"
        inserted = mock_client.table.return_value.insert.call_args[0][0]
        assert inserted["brief_id"] == "brief-2026-05-25-2"
        assert inserted["variant"] == "primary"

    def test_create_row_uses_base_when_free(self):
        mock_client = MagicMock()
        (mock_client.table.return_value.select.return_value
            .like.return_value.execute.return_value).data = []
        with _mock_db(mock_client):
            new_id = supabase_client.create_brief_feedback_row("brief-2026-05-26")
        assert new_id == "brief-2026-05-26"

    def test_trend_filters_to_primary_variant(self):
        mock_client = MagicMock()
        (mock_client.table.return_value.select.return_value
            .eq.return_value.gte.return_value.execute.return_value).data = [
            {"vote": "up"}, {"vote": "down"}, {"vote": "up"}, {"vote": None},
        ]
        with _mock_db(mock_client):
            trend = supabase_client.get_brief_feedback_trend(days=30)
        assert trend == {"up": 2, "down": 1, "total": 4, "days": 30}
        # the query filtered variant == 'primary'
        mock_client.table.return_value.select.return_value.eq.assert_called_with("variant", "primary")


# =========================================================================
# Keyboard builder
# =========================================================================

class TestBriefKeyboard:
    def test_feedback_and_pull_buttons(self):
        overflow = [
            {"section": "attention", "label": "attention", "hidden": 2},
            {"section": "email:Team emails", "label": "Team emails", "hidden": 3},  # email -> no button
        ]
        markup = mb._build_brief_keyboard("brief-2026-05-25", overflow)
        rows = markup.inline_keyboard
        # row 0: 👍 / 👎
        assert rows[0][0].callback_data == "brieffb:up:brief-2026-05-25"
        assert rows[0][1].callback_data == "brieffb:down:brief-2026-05-25"
        # one pull button for attention; none for the email section
        pulls = [b for r in rows[1:] for b in r]
        assert any(b.callback_data == "brief_more:attention:brief-2026-05-25" for b in pulls)
        assert all("email" not in b.callback_data for b in pulls)


# =========================================================================
# Telegram callback handlers (called unbound with a mock self)
# =========================================================================

class TestBriefCallbackHandlers:
    async def test_thumbs_up_round_trip_and_sync_write(self):
        from services.telegram_bot import TelegramBot
        query = MagicMock()
        query.edit_message_reply_markup = AsyncMock()
        with patch("services.supabase_client.supabase_client.set_brief_feedback_vote") as mock_vote:
            await TelegramBot._handle_brief_feedback(MagicMock(), query, "up:brief-2026-05-25")
        # parsed correctly: vote 'up', brief_id 'brief-2026-05-25'
        mock_vote.assert_called_once_with("brief-2026-05-25", "up")
        query.edit_message_reply_markup.assert_awaited_once()

    async def test_thumbs_down_opens_noise_categories(self):
        from services.telegram_bot import TelegramBot
        query = MagicMock()
        query.edit_message_reply_markup = AsyncMock()
        with patch("services.supabase_client.supabase_client.set_brief_feedback_vote") as mock_vote:
            await TelegramBot._handle_brief_feedback(MagicMock(), query, "down:brief-2026-05-25")
        mock_vote.assert_called_once_with("brief-2026-05-25", "down", pending_noise=True)
        markup = query.edit_message_reply_markup.call_args.kwargs["reply_markup"]
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "briefnoise:too_long:brief-2026-05-25" in cbs

    async def test_noise_capture_records_category(self):
        from services.telegram_bot import TelegramBot
        query = MagicMock()
        query.edit_message_reply_markup = AsyncMock()
        with patch("services.supabase_client.supabase_client.set_brief_feedback_noise") as mock_noise:
            await TelegramBot._handle_brief_noise(MagicMock(), query, "too_long:brief-2026-05-25")
        mock_noise.assert_called_once_with("brief-2026-05-25", noise_category="too_long")

    async def test_brief_more_recomputes_section(self):
        from services.telegram_bot import TelegramBot
        query = MagicMock()
        query.message.reply_text = AsyncMock()
        fake_brief = {"sections": [{"type": "knowledge_flags", "items": [
            {"topic_name": "Moldova", "kind": "blocked", "detail": "x"},
        ]}]}
        with patch("processors.morning_brief.compile_morning_brief",
                   AsyncMock(return_value=fake_brief)):
            await TelegramBot._handle_brief_more(MagicMock(), query, "attention:brief-2026-05-25")
        query.message.reply_text.assert_awaited_once()
        sent = query.message.reply_text.call_args[0][0]
        assert "Moldova" in sent
