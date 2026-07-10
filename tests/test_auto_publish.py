"""
Tests for auto-publish with review window (Phase 6).

Tests:
- schedule_auto_publish() creates task in auto_review mode
- schedule_auto_publish() does nothing in manual mode
- cancel_auto_publish() cancels pending task
- _auto_publish_after_delay() approves when still pending
- _auto_publish_after_delay() skips when already approved
- /retract command reverts last approved meeting
- /retract non-admin rejection
- Approval message shows countdown in auto_review mode
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =============================================================================
# Test schedule_auto_publish
# =============================================================================

class TestScheduleAutoPublish:
    """schedule_auto_publish() is a permanent NO-OP — auto-publish was REMOVED
    (2026-07-10). It never arms a timer, in ANY mode."""

    def test_never_schedules_even_in_auto_review(self):
        with patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.asyncio") as mock_asyncio, \
             patch("guardrails.approval_flow._pending_auto_publishes", {}) as pending:

            mock_settings.APPROVAL_MODE = "auto_review"  # inert now — still no timer
            mock_settings.AUTO_REVIEW_WINDOW_MINUTES = 30

            from guardrails.approval_flow import schedule_auto_publish
            schedule_auto_publish("meeting-123")

            mock_asyncio.create_task.assert_not_called()
            assert pending == {}

    def test_never_schedules_in_manual(self):
        with patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.asyncio") as mock_asyncio, \
             patch("guardrails.approval_flow._pending_auto_publishes", {}):

            mock_settings.APPROVAL_MODE = "manual"

            from guardrails.approval_flow import schedule_auto_publish
            schedule_auto_publish("meeting-456")

            mock_asyncio.create_task.assert_not_called()


# =============================================================================
# Test cancel_auto_publish
# =============================================================================

class TestCancelAutoPublish:
    """Tests for cancel_auto_publish() — timer cancellation."""

    def test_cancels_pending_task(self):
        """Should cancel a pending (not done) task."""
        mock_task = MagicMock()
        mock_task.done.return_value = False

        pending = {"meeting-cancel": mock_task}

        with patch("guardrails.approval_flow._pending_auto_publishes", pending):
            from guardrails.approval_flow import cancel_auto_publish

            cancel_auto_publish("meeting-cancel")

            mock_task.cancel.assert_called_once()
            # Should be removed from dict
            assert "meeting-cancel" not in pending

    def test_does_not_cancel_done_task(self):
        """Should not call cancel on already-done task."""
        mock_task = MagicMock()
        mock_task.done.return_value = True

        pending = {"meeting-done": mock_task}

        with patch("guardrails.approval_flow._pending_auto_publishes", pending):
            from guardrails.approval_flow import cancel_auto_publish

            cancel_auto_publish("meeting-done")

            mock_task.cancel.assert_not_called()

    def test_noop_for_unknown_meeting(self):
        """Should not raise when meeting_id is not in the dict."""
        pending = {}

        with patch("guardrails.approval_flow._pending_auto_publishes", pending):
            from guardrails.approval_flow import cancel_auto_publish

            # Should not raise
            cancel_auto_publish("nonexistent-meeting")


# =============================================================================
# Test _auto_publish_after_delay
# =============================================================================

class TestAutoPublishAfterDelay:
    """_auto_publish_after_delay() is REMOVED — a hard no-op that can NEVER
    distribute, in ANY mode. It only disarms the persisted row + drops the task."""

    @pytest.mark.asyncio
    async def test_never_distributes_even_in_auto_review(self):
        """The nuclear guarantee: even with APPROVAL_MODE=auto_review and a pending
        meeting, it refuses to distribute (auto-publish was removed)."""
        with patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.process_response", new_callable=AsyncMock) as mock_process, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow._pending_auto_publishes", {"m1": MagicMock()}):

            mock_settings.APPROVAL_MODE = "auto_review"
            mock_tg.send_to_eyal = AsyncMock()

            from guardrails.approval_flow import _auto_publish_after_delay, _pending_auto_publishes
            await _auto_publish_after_delay("m1", delay_minutes=30)

            mock_process.assert_not_awaited()              # NOTHING distributed
            mock_tg.send_to_eyal.assert_not_awaited()
            mock_db.get_meeting.assert_not_called()        # never even looks at the meeting
            mock_db.clear_auto_publish_at.assert_called_once_with("m1")  # legacy row disarmed
            assert "m1" not in _pending_auto_publishes

    @pytest.mark.asyncio
    async def test_never_distributes_in_manual(self):
        with patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.process_response", new_callable=AsyncMock) as mock_process, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow._pending_auto_publishes", {"m2": MagicMock()}):

            mock_settings.APPROVAL_MODE = "manual"
            mock_tg.send_to_eyal = AsyncMock()

            from guardrails.approval_flow import _auto_publish_after_delay
            await _auto_publish_after_delay("m2", delay_minutes=30)

            mock_process.assert_not_awaited()
            mock_tg.send_to_eyal.assert_not_awaited()


# =============================================================================
# Test /retract command
# =============================================================================

class TestRetractCommand:
    """Tests for the /retract Telegram command."""

    @pytest.mark.asyncio
    async def test_retract_reverts_last_approved_meeting(self):
        """Eyal should be able to retract the last approved summary."""
        mock_meeting = {
            "id": "meeting-retract-1",
            "title": "Investor Update",
            "approval_status": "approved",
        }

        with patch("services.telegram_bot.settings") as mock_settings, \
             patch("services.supabase_client.supabase_client") as mock_db:
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_GROUP_CHAT_ID = "-123"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"

            mock_db.list_meetings = MagicMock(return_value=[mock_meeting])
            mock_db.update_meeting = MagicMock()
            mock_db.log_action = MagicMock()

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.eyal_chat_id = "999"

            # Mock update and context
            update = MagicMock()
            update.effective_user.id = 999
            update.effective_chat.id = 999
            context = MagicMock()

            # Mock send_message
            bot.send_message = AsyncMock(return_value=True)

            await bot._handle_retract(update, context)

            # Should update meeting status to retracted
            mock_db.update_meeting.assert_called_once_with(
                "meeting-retract-1",
                approval_status="retracted",
            )

            # Should log the action
            mock_db.log_action.assert_called_once()
            log_call = mock_db.log_action.call_args
            assert log_call[1]["action"] == "summary_retracted"

            # Should send confirmation message
            bot.send_message.assert_awaited_once()
            sent_msg = bot.send_message.call_args[0][1]
            assert "Retracted" in sent_msg
            assert "Investor Update" in sent_msg

    @pytest.mark.asyncio
    async def test_retract_non_admin_rejected(self):
        """Non-admin users should be rejected from retracting."""
        with patch("services.telegram_bot.settings") as mock_settings:
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_GROUP_CHAT_ID = "-123"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.eyal_chat_id = "999"

            # Mock update from non-admin user
            update = MagicMock()
            update.effective_user.id = 12345  # Not Eyal
            update.effective_chat.id = 12345
            context = MagicMock()

            bot.send_message = AsyncMock(return_value=True)

            await bot._handle_retract(update, context)

            # Should send rejection message
            bot.send_message.assert_awaited_once()
            sent_msg = bot.send_message.call_args[0][1]
            assert "Only Eyal" in sent_msg

    @pytest.mark.asyncio
    async def test_retract_no_approved_meetings(self):
        """Should inform Eyal when there's nothing to retract."""
        with patch("services.telegram_bot.settings") as mock_settings, \
             patch("services.supabase_client.supabase_client") as mock_db:
            mock_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_settings.TELEGRAM_GROUP_CHAT_ID = "-123"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"

            mock_db.list_meetings = MagicMock(return_value=[])

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.eyal_chat_id = "999"

            update = MagicMock()
            update.effective_user.id = 999
            update.effective_chat.id = 999
            context = MagicMock()

            bot.send_message = AsyncMock(return_value=True)

            await bot._handle_retract(update, context)

            bot.send_message.assert_awaited_once()
            sent_msg = bot.send_message.call_args[0][1]
            assert "No recently approved" in sent_msg


# =============================================================================
# Test approval message countdown in auto_review mode
# =============================================================================

class TestApprovalMessageCountdown:
    """Approval cards must NEVER show an auto-publish countdown — auto-publish was
    removed (2026-07-10), so implying a timed auto-send would be a lie."""

    @pytest.mark.asyncio
    async def test_no_countdown_even_in_auto_review(self):
        """Even with the (inert) auto_review env var, the card shows no countdown."""
        with patch("services.telegram_bot.settings") as mock_bot_settings, \
             patch("config.settings.settings") as mock_cfg:
            mock_bot_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_bot_settings.TELEGRAM_GROUP_CHAT_ID = "-123"
            mock_bot_settings.TELEGRAM_EYAL_CHAT_ID = "999"

            mock_cfg.APPROVAL_MODE = "auto_review"
            mock_cfg.AUTO_REVIEW_WINDOW_MINUTES = 60

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.eyal_chat_id = "999"
            mock_send = AsyncMock(return_value=MagicMock(message_id=1))
            bot._app = MagicMock()
            bot.app.bot.send_message = mock_send

            await bot.send_approval_request(
                meeting_title="Test Meeting",
                summary_preview="A brief discussion.",
                meeting_id="meeting-countdown-1",
                decisions=[{"description": "Use AWS"}],
                tasks=[{"title": "Deploy", "assignee": "Eyal", "priority": "H"}],
            )

            mock_send.assert_awaited_once()
            sent_msg = mock_send.call_args.kwargs.get("text", "")
            assert "Auto-publish" not in sent_msg  # no countdown, ever

    @pytest.mark.asyncio
    async def test_no_countdown_in_manual_mode(self):
        """Approval message should NOT show countdown in manual mode."""
        with patch("services.telegram_bot.settings") as mock_bot_settings:
            mock_bot_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_bot_settings.TELEGRAM_GROUP_CHAT_ID = "-123"
            mock_bot_settings.TELEGRAM_EYAL_CHAT_ID = "999"

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.eyal_chat_id = "999"
            mock_send = AsyncMock(return_value=MagicMock(message_id=1))
            bot._app = MagicMock()
            bot.app.bot.send_message = mock_send

            with patch("config.settings.settings") as mock_cfg:
                mock_cfg.APPROVAL_MODE = "manual"

                await bot.send_approval_request(
                    meeting_title="Test Meeting",
                    summary_preview="A brief discussion.",
                    meeting_id="meeting-no-countdown",
                )

            mock_send.assert_awaited_once()
            sent_msg = mock_send.call_args.kwargs.get("text", "")
            assert "Auto-publish" not in sent_msg


# =============================================================================
# Test reconstruct_auto_publish_timers mode gate (restart-time safety)
# =============================================================================

class TestReconstructDisarmOnly:
    """reconstruct_auto_publish_timers() no longer re-arms ANYTHING (auto-publish
    removed) — it only disarms legacy rows and always returns 0, in every mode."""

    @pytest.mark.asyncio
    async def test_disarms_legacy_rows_and_returns_zero(self):
        rows = [
            {"approval_id": "a1", "auto_publish_at": "2026-07-01T10:00:00"},
            {"approval_id": "a2", "auto_publish_at": "2026-07-01T11:00:00"},
        ]
        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.asyncio") as mock_asyncio:

            mock_db.get_pending_auto_publishes = MagicMock(return_value=rows)

            from guardrails.approval_flow import reconstruct_auto_publish_timers
            count = await reconstruct_auto_publish_timers()

            assert count == 0                                    # nothing armed
            assert mock_db.clear_auto_publish_at.call_count == 2  # both disarmed
            mock_asyncio.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_disarms_even_when_auto_review_still_set(self):
        """The inert auto_review env var can't bring auto-publish back — a leftover
        timer is disarmed, not re-armed, even with APPROVAL_MODE=auto_review."""
        rows = [{"approval_id": "legacy1", "auto_publish_at": "2026-07-01T10:00:00"}]
        with patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.asyncio") as mock_asyncio:

            mock_settings.APPROVAL_MODE = "auto_review"  # inert
            mock_db.get_pending_auto_publishes = MagicMock(return_value=rows)

            from guardrails.approval_flow import reconstruct_auto_publish_timers
            count = await reconstruct_auto_publish_timers()

            assert count == 0
            mock_db.clear_auto_publish_at.assert_called_once_with("legacy1")
            mock_asyncio.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_is_noop(self):
        with patch("guardrails.approval_flow.supabase_client") as mock_db:
            mock_db.get_pending_auto_publishes = MagicMock(return_value=[])
            from guardrails.approval_flow import reconstruct_auto_publish_timers
            count = await reconstruct_auto_publish_timers()
            assert count == 0
            mock_db.clear_auto_publish_at.assert_not_called()
