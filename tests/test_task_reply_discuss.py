"""
Tests for the overdue-task-reminder "discuss" free-text reply path.

Guards audit finding P3-06: the old code called a bare (unimported)
`create_open_question(...)` inside `try/except: pass` and then ALWAYS told Eyal
"Added to next meeting agenda." — a silent NameError + a false confirmation, so
the item never reached an agenda. The fix anchors the open_question to the task's
source meeting, confirms ONLY on success, and alerts on failure.

Hermetic: TelegramBot is built via __new__ (no __init__/network); all collaborators
are patched. No live Google/Telegram/DB.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_bot_and_update(task_info, user_text="let's discuss this one"):
    """Build a TelegramBot (no __init__) + a fake reply Update for the discuss path."""
    from services.telegram_bot import TelegramBot

    bot = TelegramBot.__new__(TelegramBot)
    bot.send_message = AsyncMock()

    update = MagicMock()
    update.message.reply_to_message.message_id = 123
    update.message.text = user_text
    update.effective_chat.id = 8190904141  # Eyal DM

    fake_scheduler = MagicMock()
    fake_scheduler.message_task_map = {123: "short-1"}
    fake_scheduler.task_action_map = {"short-1": task_info}

    return bot, update, fake_scheduler


def _supabase_with_meeting(meeting_id):
    """A supabase_client mock whose tasks lookup returns the given meeting_id."""
    mock_sc = MagicMock()
    chain = mock_sc.client.table.return_value.select.return_value.eq.return_value.limit.return_value
    chain.execute.return_value.data = (
        [{"meeting_id": meeting_id}] if meeting_id is not None else []
    )
    return mock_sc


@pytest.mark.asyncio
async def test_discuss_anchors_open_question_to_source_meeting():
    task_info = {"task_id": "task-uuid", "task_text": "Sign the lease"}
    bot, update, fake_scheduler = _make_bot_and_update(task_info)
    mock_sc = _supabase_with_meeting("mtg-123")

    with patch("schedulers.task_reminder_scheduler.task_reminder_scheduler", fake_scheduler), \
         patch("core.llm.call_llm", return_value=("discuss", {})), \
         patch("services.supabase_client.supabase_client", mock_sc), \
         patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
        handled = await bot._handle_task_reply(update, MagicMock())

    assert handled is True
    # open_question created, anchored to the task's source meeting
    mock_sc.create_open_question.assert_called_once()
    kwargs = mock_sc.create_open_question.call_args.kwargs
    assert kwargs["meeting_id"] == "mtg-123"
    assert "Sign the lease" in kwargs["question"]
    assert kwargs["raised_by"] == "Eyal"
    # Eyal is told it was added — only because it actually was
    bot.send_message.assert_awaited_once()
    assert "next meeting agenda" in bot.send_message.await_args.args[1]
    # No alert on the happy path
    mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_discuss_no_source_meeting_warns_and_alerts_no_false_confirm():
    # The exact original-bug scenario: the open_question can't be anchored.
    task_info = {"task_id": "task-uuid", "task_text": "Renegotiate the SAFE"}
    bot, update, fake_scheduler = _make_bot_and_update(task_info)
    mock_sc = _supabase_with_meeting(None)  # lookup returns no meeting

    with patch("schedulers.task_reminder_scheduler.task_reminder_scheduler", fake_scheduler), \
         patch("core.llm.call_llm", return_value=("discuss", {})), \
         patch("services.supabase_client.supabase_client", mock_sc), \
         patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
        handled = await bot._handle_task_reply(update, MagicMock())

    assert handled is True
    # Nothing was written...
    mock_sc.create_open_question.assert_not_called()
    # ...and Eyal is NOT falsely told it was added
    msg = bot.send_message.await_args.args[1]
    assert "next meeting agenda" not in msg
    assert "manually" in msg
    # ...and the failure is surfaced as a CRITICAL alert
    mock_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_discuss_create_failure_surfaces_warning_not_confirmation():
    task_info = {"task_id": "task-uuid", "task_text": "Approve the budget"}
    bot, update, fake_scheduler = _make_bot_and_update(task_info)
    mock_sc = _supabase_with_meeting("mtg-7")
    mock_sc.create_open_question.side_effect = Exception("supabase write failed")

    with patch("schedulers.task_reminder_scheduler.task_reminder_scheduler", fake_scheduler), \
         patch("core.llm.call_llm", return_value=("discuss", {})), \
         patch("services.supabase_client.supabase_client", mock_sc), \
         patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
        handled = await bot._handle_task_reply(update, MagicMock())

    assert handled is True
    msg = bot.send_message.await_args.args[1]
    assert "next meeting agenda" not in msg  # no false confirmation
    assert "manually" in msg
    mock_alert.assert_awaited_once()
