"""
Tests for guardrails hardening:
  P5-03 — the approval card HTML-escapes the untrusted assignee/led_by/raised_by
          fields (no injected link / no parse-break that silently drops the card).
  P5-08 — inbound-interaction logging omits the message preview for Eyal's DM
          (don't accumulate a plaintext log of his sensitive queries).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# P5-03 — approval card escapes untrusted fields
# =============================================================================

class TestApprovalCardEscaping:
    @pytest.mark.asyncio
    async def test_untrusted_fields_are_html_escaped(self):
        from services.telegram_bot import telegram_bot

        captured = {"text": ""}

        async def fake_send(chat_id, text, **kwargs):
            captured["text"] += text
            m = MagicMock()
            m.message_id = 1
            return m

        app_mock = MagicMock()
        app_mock.bot.send_message = AsyncMock(side_effect=fake_send)
        app_mock.bot.delete_message = AsyncMock()

        orig_app, orig_ids, orig_chat = (
            telegram_bot._app, telegram_bot._approval_message_ids, telegram_bot.eyal_chat_id,
        )
        telegram_bot._app = app_mock
        telegram_bot._approval_message_ids = {}
        telegram_bot.eyal_chat_id = 123
        try:
            await telegram_bot.send_approval_request(
                meeting_title="M", summary_preview="s", meeting_id="m1",
                tasks=[{"title": "t", "assignee": "<b>Paolo</b>", "priority": "M"}],
                follow_ups=[{"title": "f", "led_by": "a & b"}],
                open_questions=[{"question": "q?", "raised_by": "<i>x</i>"}],
            )
        finally:
            telegram_bot._app = orig_app
            telegram_bot._approval_message_ids = orig_ids
            telegram_bot.eyal_chat_id = orig_chat

        text = captured["text"]
        # The injected markup must be escaped, not rendered.
        assert "<b>Paolo</b>" not in text
        assert "&lt;b&gt;Paolo&lt;/b&gt;" in text
        assert "a &amp; b" in text
        assert "&lt;i&gt;x&lt;/i&gt;" in text


# =============================================================================
# P5-08 — inbound-interaction logging redacts Eyal-DM previews
# =============================================================================

class TestInboundPreviewRedaction:
    def test_dm_preview_omitted(self):
        from guardrails import inbound_filter
        with patch.object(inbound_filter.supabase_client, "log_action") as mock_log:
            inbound_filter.log_inbound_interaction(
                sender="eyal", channel="telegram_dm",
                preview="our investor term sheet is 12M at 60 pre",
                verified=True, relevant="True", action="allowed",
            )
        details = mock_log.call_args.kwargs["details"]
        assert details["message_preview"] == ""

    def test_group_preview_kept(self):
        from guardrails import inbound_filter
        with patch.object(inbound_filter.supabase_client, "log_action") as mock_log:
            inbound_filter.log_inbound_interaction(
                sender="roye", channel="telegram_group",
                preview="standup notes for today",
                verified=True, relevant="True", action="allowed",
            )
        details = mock_log.call_args.kwargs["details"]
        assert details["message_preview"] == "standup notes for today"
