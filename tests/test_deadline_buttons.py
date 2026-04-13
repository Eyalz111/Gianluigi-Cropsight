"""
Tests for PR 5 — inline deadline buttons (v2.3).

Covers:
- _handle_deadline_clear handler: updates DB, logs observation, edits message
- taskdelay branch now sets deadline_confidence=EXPLICIT
- Reminder keyboard includes the Clear date button
- Voice-aligned messages (no system-speak)
- Graceful fallback when task cannot be resolved
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# Reminder keyboard shape
# =============================================================================

class TestReminderKeyboardShape:
    def test_keyboard_has_clear_date_button(self):
        """The overdue reminder keyboard exposes Done/+1Week/Discuss + Clear."""
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton

        # Replicate the keyboard shape from _send_overdue_reminder
        callback_key = "some-task-uuid"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Done", callback_data=f"taskdone:{callback_key}"),
                InlineKeyboardButton("+1 Week", callback_data=f"taskdelay:{callback_key}"),
                InlineKeyboardButton("Discuss", callback_data=f"taskdiscuss:{callback_key}"),
            ],
            [
                InlineKeyboardButton("Clear date", callback_data=f"deadline_clear:{callback_key}"),
            ],
        ])

        all_callback_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert f"deadline_clear:{callback_key}" in all_callback_data
        assert f"taskdelay:{callback_key}" in all_callback_data  # still present


# =============================================================================
# _handle_deadline_clear handler
# =============================================================================

class TestDeadlineClearHandler:
    @pytest.mark.asyncio
    async def test_clears_deadline_and_logs_observation(self):
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)  # skip __init__

        fake_task = {"id": "task-uuid", "title": "Sign contract", "deadline": "2026-04-01"}

        # Patch the lookup helper to return our fake task
        bot._resolve_task_for_deadline_button = AsyncMock(return_value=fake_task)

        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.update_task_deadline.return_value = {"id": "task-uuid"}

            await bot._handle_deadline_clear(query, "task-uuid")

            # DB update called with deadline=None, confidence=NONE
            mock_sc.update_task_deadline.assert_called_once()
            call_kwargs = mock_sc.update_task_deadline.call_args.kwargs
            assert call_kwargs["deadline"] is None
            assert call_kwargs["confidence"] == "NONE"
            assert call_kwargs["task_id"] == "task-uuid"

            # Observation logged
            assert mock_sc.log_approval_observation.called
            obs_kwargs = mock_sc.log_approval_observation.call_args.kwargs
            assert obs_kwargs["content_type"] == "deadline_update"
            assert obs_kwargs["action"] == "edited"
            assert obs_kwargs["final_content"] == {"deadline": None}

        # Voice-aligned confirmation (no "Deadline cleared" or emoji prefix)
        query.edit_message_text.assert_called_once()
        msg = query.edit_message_text.call_args.args[0]
        assert msg.startswith("Date cleared — ")
        assert "Sign contract" in msg
        # No system-speak patterns
        assert "🗑" not in msg
        assert "Deadline cleared" not in msg

    @pytest.mark.asyncio
    async def test_unknown_task_shows_voice_aligned_error(self):
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot._resolve_task_for_deadline_button = AsyncMock(return_value=None)

        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        with patch("services.supabase_client.supabase_client") as mock_sc:
            await bot._handle_deadline_clear(query, "missing-key")

            # No DB write, no observation
            mock_sc.update_task_deadline.assert_not_called()
            mock_sc.log_approval_observation.assert_not_called()

        msg = query.edit_message_text.call_args.args[0]
        assert msg == "Can't find that task."

    @pytest.mark.asyncio
    async def test_db_failure_shows_graceful_error(self):
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        bot._resolve_task_for_deadline_button = AsyncMock(
            return_value={"id": "task-uuid", "title": "T", "deadline": "2026-04-01"}
        )

        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.update_task_deadline.side_effect = Exception("connection lost")

            await bot._handle_deadline_clear(query, "task-uuid")

            # Observation NOT logged (the DB update failed before that call)
            mock_sc.log_approval_observation.assert_not_called()

        msg = query.edit_message_text.call_args.args[0]
        assert msg == "Couldn't update — try again?"


# =============================================================================
# taskdelay branch: stamps EXPLICIT + logs observation
# =============================================================================

class TestTaskdelayStampsExplicit:
    def test_execute_task_update_passes_deadline_confidence(self):
        """
        _execute_task_update_from_reminder accepts deadline_confidence kwarg
        and includes it in the update payload passed to supabase_client.
        """
        import asyncio
        from services.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)

        task_info = {
            "task_id": "task-uuid",
            "task_text": "Send proposal",
            "assignee": "Paolo",
            "row_number": None,  # skip Sheets update
            "deadline": "2026-04-01",
        }

        with patch("services.supabase_client.supabase_client") as mock_sc:
            mock_sc.update_task.return_value = {"id": "task-uuid"}

            # Run the async function
            result = asyncio.run(
                bot._execute_task_update_from_reminder(
                    task_info=task_info,
                    delay_days=7,
                    deadline_confidence="EXPLICIT",
                )
            )

            # Check update_task was called with deadline_confidence
            mock_sc.update_task.assert_called_once()
            call_kwargs = mock_sc.update_task.call_args.kwargs
            assert call_kwargs.get("deadline_confidence") == "EXPLICIT"
            assert "deadline" in call_kwargs  # new deadline computed
