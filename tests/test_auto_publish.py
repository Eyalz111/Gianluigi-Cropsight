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
    """Tests for schedule_auto_publish() — timer creation."""

    def test_creates_task_in_auto_review_mode(self):
        """Should create an asyncio task when APPROVAL_MODE is auto_review."""
        with patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.asyncio") as mock_asyncio, \
             patch("guardrails.approval_flow._pending_auto_publishes", {}):

            mock_settings.APPROVAL_MODE = "auto_review"
            mock_settings.AUTO_REVIEW_WINDOW_MINUTES = 30

            mock_task = MagicMock()
            mock_task.done.return_value = False
            mock_asyncio.create_task.return_value = mock_task

            from guardrails.approval_flow import schedule_auto_publish, _pending_auto_publishes

            schedule_auto_publish("meeting-123")

            # Should have created a task
            mock_asyncio.create_task.assert_called_once()
            # Task should be stored in the dict
            assert "meeting-123" in _pending_auto_publishes

    def test_does_nothing_in_manual_mode(self):
        """Should not create any task when APPROVAL_MODE is manual."""
        with patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.asyncio") as mock_asyncio, \
             patch("guardrails.approval_flow._pending_auto_publishes", {}):

            mock_settings.APPROVAL_MODE = "manual"

            from guardrails.approval_flow import schedule_auto_publish

            schedule_auto_publish("meeting-456")

            # Should NOT have created a task
            mock_asyncio.create_task.assert_not_called()

    def test_cancels_existing_timer_before_scheduling_new(self):
        """Should cancel existing timer for same meeting before creating new one."""
        existing_task = MagicMock()
        existing_task.done.return_value = False

        pending = {"meeting-789": existing_task}

        with patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("guardrails.approval_flow.asyncio") as mock_asyncio, \
             patch("guardrails.approval_flow._pending_auto_publishes", pending):

            mock_settings.APPROVAL_MODE = "auto_review"
            mock_settings.AUTO_REVIEW_WINDOW_MINUTES = 15

            new_task = MagicMock()
            new_task.done.return_value = False
            mock_asyncio.create_task.return_value = new_task

            from guardrails.approval_flow import schedule_auto_publish

            schedule_auto_publish("meeting-789")

            # Should have cancelled the old task
            existing_task.cancel.assert_called_once()
            # Should have created a new one
            mock_asyncio.create_task.assert_called_once()


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
    """Tests for _auto_publish_after_delay() — background auto-approval."""

    @pytest.mark.asyncio
    async def test_approves_when_still_pending(self):
        """Should auto-approve and notify Eyal when meeting is still pending."""
        mock_meeting = {
            "id": "meeting-auto-1",
            "title": "Sprint Retro",
            "approval_status": "pending",
        }

        with patch("guardrails.approval_flow.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.process_response", new_callable=AsyncMock) as mock_process, \
             patch("guardrails.approval_flow.telegram_bot") as mock_tg, \
             patch("guardrails.approval_flow._pending_auto_publishes", {"meeting-auto-1": MagicMock()}):

            mock_db.get_meeting = MagicMock(return_value=mock_meeting)
            mock_process.return_value = {"action": "approved"}
            mock_tg.send_to_eyal = AsyncMock(return_value=True)

            from guardrails.approval_flow import _auto_publish_after_delay

            await _auto_publish_after_delay("meeting-auto-1", delay_minutes=30)

            # Should have slept for 30 * 60 seconds
            mock_sleep.assert_awaited_once_with(30 * 60)

            # Should have called process_response with approve
            mock_process.assert_awaited_once_with(
                meeting_id="meeting-auto-1",
                response="approve",
                response_source="auto_review",
            )

            # Should have notified Eyal
            mock_tg.send_to_eyal.assert_awaited_once()
            sent_msg = mock_tg.send_to_eyal.call_args[0][0]
            assert "Auto-published" in sent_msg
            assert "Sprint Retro" in sent_msg
            assert "/retract" in sent_msg

    @pytest.mark.asyncio
    async def test_skips_when_already_approved(self):
        """Should skip auto-publish when meeting was already approved by Eyal."""
        mock_meeting = {
            "id": "meeting-auto-2",
            "title": "Daily Standup",
            "approval_status": "approved",  # Already approved
        }

        with patch("guardrails.approval_flow.asyncio.sleep", new_callable=AsyncMock), \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.process_response", new_callable=AsyncMock) as mock_process, \
             patch("guardrails.approval_flow.telegram_bot") as mock_tg:

            mock_db.get_meeting = MagicMock(return_value=mock_meeting)
            mock_tg.send_to_eyal = AsyncMock()

            from guardrails.approval_flow import _auto_publish_after_delay

            await _auto_publish_after_delay("meeting-auto-2", delay_minutes=30)

            # Should NOT have called process_response
            mock_process.assert_not_awaited()

            # Should NOT have notified Eyal
            mock_tg.send_to_eyal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_meeting_not_found(self):
        """Should do nothing when the meeting doesn't exist."""
        with patch("guardrails.approval_flow.asyncio.sleep", new_callable=AsyncMock), \
             patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.process_response", new_callable=AsyncMock) as mock_process:

            mock_db.get_meeting = MagicMock(return_value=None)

            from guardrails.approval_flow import _auto_publish_after_delay

            await _auto_publish_after_delay("nonexistent", delay_minutes=30)

            mock_process.assert_not_awaited()


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
    """Tests for countdown indicator in approval messages."""

    @pytest.mark.asyncio
    async def test_shows_countdown_in_auto_review_mode(self):
        """Approval message should show auto-publish countdown in auto_review mode."""
        with patch("services.telegram_bot.settings") as mock_bot_settings, \
             patch("config.settings.settings") as mock_cfg:
            mock_bot_settings.TELEGRAM_BOT_TOKEN = "test-token"
            mock_bot_settings.TELEGRAM_GROUP_CHAT_ID = "-123"
            mock_bot_settings.TELEGRAM_EYAL_CHAT_ID = "999"

            mock_cfg.APPROVAL_MODE = "auto_review"
            mock_cfg.AUTO_REVIEW_WINDOW_MINUTES = 30

            from services.telegram_bot import TelegramBot

            bot = TelegramBot()
            bot.eyal_chat_id = "999"
            bot.send_to_eyal = AsyncMock(return_value=True)

            await bot.send_approval_request(
                meeting_title="Test Meeting",
                summary_preview="A brief discussion.",
                meeting_id="meeting-countdown-1",
                decisions=[{"description": "Use AWS"}],
                tasks=[{"title": "Deploy", "assignee": "Eyal", "priority": "H"}],
            )

            # Check the message sent to Eyal
            bot.send_to_eyal.assert_awaited_once()
            sent_msg = bot.send_to_eyal.call_args[0][0]
            assert "Auto-publish in 30 minutes" in sent_msg

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
            bot.send_to_eyal = AsyncMock(return_value=True)

            with patch("config.settings.settings") as mock_cfg:
                mock_cfg.APPROVAL_MODE = "manual"

                await bot.send_approval_request(
                    meeting_title="Test Meeting",
                    summary_preview="A brief discussion.",
                    meeting_id="meeting-no-countdown",
                )

            bot.send_to_eyal.assert_awaited_once()
            sent_msg = bot.send_to_eyal.call_args[0][0]
            assert "Auto-publish" not in sent_msg
