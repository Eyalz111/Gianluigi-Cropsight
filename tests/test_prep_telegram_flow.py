"""
Tests for Phase 5.3: Telegram Outline Flow.

Tests cover:
- Outline sending with correct buttons (auto vs ask)
- Callback routing for all 4 actions
- Focus input + persistence
- Email response rejected for prep_outline
- generate_meeting_prep_from_outline
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

# Common mock for supabase_client used in local imports
SUPABASE_PATCH = "services.supabase_client.supabase_client"


# =============================================================================
# Test send_prep_outline
# =============================================================================

class TestSendPrepOutline:

    @pytest.mark.asyncio
    async def test_auto_confidence_buttons(self):
        """Auto confidence → Generate + Focus + Skip (no reclassify)."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"
        bot.send_to_eyal = AsyncMock(return_value=True)

        outline = {
            "event": {"title": "Tech Review", "start": "2026-03-17T10:00:00", "attendees": []},
            "template_name": "Founders Technical Review",
            "sections": [{"name": "Tasks", "status": "ok", "item_count": 3}],
            "suggested_agenda": ["Item 1"],
        }

        result = await bot.send_prep_outline(outline, "outline-123", confidence="auto")
        assert result is True

        call_args = bot.send_to_eyal.call_args
        markup = call_args.kwargs.get("reply_markup") or call_args[1].get("reply_markup")
        all_data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert any("prep_generate" in d for d in all_data)
        assert any("prep_focus" in d for d in all_data)
        assert any("prep_skip" in d for d in all_data)
        assert not any("prep_reclassify" in d for d in all_data)

    @pytest.mark.asyncio
    async def test_ask_confidence_has_reclassify(self):
        """Ask confidence → includes reclassify button."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"
        bot.send_to_eyal = AsyncMock(return_value=True)

        outline = {
            "event": {"title": "Meeting", "start": "", "attendees": []},
            "template_name": "Test",
            "sections": [],
            "suggested_agenda": [],
        }

        await bot.send_prep_outline(outline, "outline-456", confidence="ask")
        call_args = bot.send_to_eyal.call_args
        markup = call_args.kwargs.get("reply_markup") or call_args[1].get("reply_markup")
        all_data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert any("prep_reclassify" in d for d in all_data)


# =============================================================================
# Test callback routing
# =============================================================================

class TestPrepOutlineCallbacks:

    @pytest.mark.asyncio
    async def test_skip_updates_status(self):
        """Skip callback should mark approval as skipped."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"

        mock_query = MagicMock()
        mock_query.from_user.id = 123
        mock_query.edit_message_text = AsyncMock()

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.update_pending_approval.return_value = {}
            mock_db.log_action.return_value = None

            await bot._handle_prep_outline_callback(
                mock_query, MagicMock(), "prep_skip", "outline-123"
            )

            mock_db.update_pending_approval.assert_called_once_with(
                "outline-123", status="skipped"
            )
            mock_query.edit_message_text.assert_awaited_once_with("Prep skipped.")

    @pytest.mark.asyncio
    async def test_focus_stores_state(self):
        """Focus callback should set focus_active in content."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"

        mock_query = MagicMock()
        mock_query.from_user.id = 123
        mock_query.edit_message_text = AsyncMock()
        mock_context = MagicMock()
        mock_context.user_data = {}

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.get_pending_approval.return_value = {
                "content": {"outline": {}, "focus_instructions": []},
            }
            mock_db.update_pending_approval.return_value = {}

            await bot._handle_prep_outline_callback(
                mock_query, mock_context, "prep_focus", "outline-123"
            )

            update_call = mock_db.update_pending_approval.call_args
            updated_content = update_call.kwargs.get("content") or update_call[1].get("content")
            assert updated_content["focus_active"] is True
            assert mock_context.user_data["prep_focus_approval_id"] == "outline-123"

    @pytest.mark.asyncio
    async def test_generate_calls_generator(self):
        """Generate callback should call generate_meeting_prep_from_outline."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"
        bot.send_to_eyal = AsyncMock(return_value=True)

        mock_query = MagicMock()
        mock_query.from_user.id = 123
        mock_query.edit_message_text = AsyncMock()

        with patch("processors.meeting_prep.generate_meeting_prep_from_outline",
                   new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = {"status": "success"}

            await bot._handle_prep_outline_callback(
                mock_query, MagicMock(), "prep_generate", "outline-123"
            )

            mock_gen.assert_awaited_once_with("outline-123")

    @pytest.mark.asyncio
    async def test_non_eyal_rejected(self):
        """Non-Eyal user should be rejected."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"

        mock_query = MagicMock()
        mock_query.from_user.id = 999
        mock_query.answer = AsyncMock()

        await bot._handle_prep_outline_callback(
            mock_query, MagicMock(), "prep_generate", "outline-123"
        )
        mock_query.answer.assert_awaited_once()


# =============================================================================
# Test focus input handler
# =============================================================================

class TestPrepFocusInput:

    @pytest.mark.asyncio
    async def test_focus_input_from_cache(self):
        """Focus input with cached approval_id should work."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"
        bot.send_message = AsyncMock(return_value=True)

        mock_update = MagicMock()
        mock_update.message.text = "Focus on MVP timeline"
        mock_update.effective_chat.id = 123
        mock_context = MagicMock()
        mock_context.user_data = {"prep_focus_approval_id": "outline-123"}

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.get_pending_approval.return_value = {
                "approval_id": "outline-123",
                "content": {
                    "outline": {"event": {"title": "Test"}},
                    "focus_instructions": [],
                    "focus_active": True,
                },
            }
            mock_db.update_pending_approval.return_value = {}

            result = await bot._handle_prep_focus_input(mock_update, mock_context)
            assert result is True

            update_call = mock_db.update_pending_approval.call_args
            updated_content = update_call.kwargs.get("content") or update_call[1].get("content")
            assert "Focus on MVP timeline" in updated_content["focus_instructions"]
            assert updated_content["focus_active"] is False

    @pytest.mark.asyncio
    async def test_focus_input_supabase_fallback(self):
        """Without cache, should check Supabase for active focus."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"
        bot.send_message = AsyncMock(return_value=True)

        mock_update = MagicMock()
        mock_update.message.text = "Check Paolo's BD pipeline"
        mock_update.effective_chat.id = 123
        mock_context = MagicMock()
        mock_context.user_data = {}

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.get_pending_prep_outlines.return_value = [{
                "approval_id": "outline-789",
                "content": {"outline": {}, "focus_instructions": [], "focus_active": True},
            }]
            mock_db.get_pending_approval.return_value = {
                "approval_id": "outline-789",
                "content": {"outline": {}, "focus_instructions": [], "focus_active": True},
            }
            mock_db.update_pending_approval.return_value = {}

            result = await bot._handle_prep_focus_input(mock_update, mock_context)
            assert result is True

    @pytest.mark.asyncio
    async def test_no_active_focus_returns_false(self):
        """No active focus → return False."""
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "123"

        mock_update = MagicMock()
        mock_update.message.text = "Regular question"
        mock_context = MagicMock()
        mock_context.user_data = {}

        with patch(SUPABASE_PATCH) as mock_db:
            mock_db.get_pending_prep_outlines.return_value = []

            result = await bot._handle_prep_focus_input(mock_update, mock_context)
            assert result is False


# =============================================================================
# Test email rejection for prep_outline
# =============================================================================

class TestPrepOutlineEmailGuard:

    @pytest.mark.asyncio
    async def test_email_response_rejected(self):
        """Email response to prep_outline should be rejected."""
        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.telegram_bot"), \
             patch("guardrails.approval_flow.gmail_service"):

            mock_db.get_pending_approval.return_value = {
                "approval_id": "outline-123",
                "content_type": "prep_outline",
                "content": {"outline": {}},
                "status": "pending",
            }

            from guardrails.approval_flow import process_response

            result = await process_response(
                meeting_id="outline-123",
                response="approve",
                response_source="email",
            )

            assert result["action"] == "error"
            assert "Telegram" in result["error"]


# =============================================================================
# Test generate_meeting_prep_from_outline
# =============================================================================

class TestGenerateMeetingPrepFromOutline:

    @pytest.mark.asyncio
    async def test_generates_from_stored_outline(self):
        """Should load outline, generate doc, submit for approval (no Drive upload yet)."""
        with patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.supabase_client") as mock_af_db, \
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow.gmail_service") as mock_gmail, \
             patch("guardrails.approval_flow.conversation_memory"), \
             patch("guardrails.sensitivity_classifier.classify_sensitivity", return_value="founders"):

            mock_db.get_pending_approval.return_value = {
                "approval_id": "outline-123",
                "content": {
                    "outline": {
                        "event": {"title": "Tech Review", "start": "2026-03-17T10:00:00", "attendees": [], "id": "evt1"},
                        "sections": [{"name": "Tasks", "status": "ok", "data": [], "item_count": 0}],
                    },
                    "meeting_type": "founders_technical",
                    "focus_instructions": ["Focus on ML pipeline"],
                },
            }
            mock_db.update_pending_approval.return_value = {}
            mock_db.log_action.return_value = None

            mock_af_db.get_pending_approvals_by_status.return_value = []
            mock_af_db.upsert_pending_approval.return_value = {}
            mock_af_db.log_action.return_value = None
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)

            from processors.meeting_prep import generate_meeting_prep_from_outline

            result = await generate_meeting_prep_from_outline("outline-123")
            assert result["status"] == "success"
            assert "prep_approval_id" in result

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self):
        """Missing outline should return error."""
        with patch("processors.meeting_prep.supabase_client") as mock_db:
            mock_db.get_pending_approval.return_value = None

            from processors.meeting_prep import generate_meeting_prep_from_outline

            result = await generate_meeting_prep_from_outline("outline-nonexistent")
            assert result["status"] == "error"
