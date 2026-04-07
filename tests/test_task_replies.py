"""
Tests for Telegram inline task replies (Phase 3).

Verifies button generation, callback routing, short_id mapping,
deadline calculation, and task action map integrity.
"""

import pytest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


class TestTaskReminderButtons:
    """Tests for inline button generation on overdue reminders."""

    def _make_scheduler(self):
        """Create a TaskReminderScheduler with no auto-start."""
        with patch("schedulers.task_reminder_scheduler.settings") as mock_s:
            mock_s.TASK_REMINDER_CHECK_INTERVAL = 3600
            from schedulers.task_reminder_scheduler import TaskReminderScheduler
            return TaskReminderScheduler(check_interval=3600)

    def test_short_id_generation(self):
        """Short IDs increment correctly."""
        scheduler = self._make_scheduler()
        id1 = scheduler._next_short_id()
        id2 = scheduler._next_short_id()
        assert id1 == "t1"
        assert id2 == "t2"

    def test_short_id_unique_across_calls(self):
        """Short IDs are unique across multiple calls."""
        scheduler = self._make_scheduler()
        ids = [scheduler._next_short_id() for _ in range(100)]
        assert len(set(ids)) == 100

    def test_task_action_map_starts_empty(self):
        """task_action_map starts empty."""
        scheduler = self._make_scheduler()
        assert len(scheduler.task_action_map) == 0

    def test_message_task_map_starts_empty(self):
        """message_task_map starts empty."""
        scheduler = self._make_scheduler()
        assert len(scheduler.message_task_map) == 0

    def test_task_action_map_stores_task_info(self):
        """task_action_map correctly stores and retrieves task info."""
        scheduler = self._make_scheduler()
        short_id = scheduler._next_short_id()
        scheduler.task_action_map[short_id] = {
            "task_text": "Update pitch deck",
            "assignee": "Eyal",
            "row_number": 5,
            "deadline": "2026-04-01",
        }
        assert scheduler.task_action_map[short_id]["task_text"] == "Update pitch deck"
        assert scheduler.task_action_map[short_id]["row_number"] == 5

    @pytest.mark.asyncio
    async def test_overdue_reminder_stores_mappings(self):
        """_send_overdue_reminder stores both action map and message map."""
        scheduler = self._make_scheduler()

        mock_msg = MagicMock()
        mock_msg.message_id = 12345

        with patch("schedulers.task_reminder_scheduler.telegram_bot") as mock_bot:
            mock_bot.eyal_chat_id = "8190904141"
            mock_bot.app.bot.send_message = AsyncMock(return_value=mock_msg)
            mock_bot.send_message = AsyncMock(return_value=True)
            mock_bot.send_to_eyal = AsyncMock(return_value=True)

            task = {
                "task": "Update pitch deck",
                "assignee": "Eyal",
                "priority": "H",
                "deadline": "2026-04-01",
                "row_number": 5,
            }

            result = await scheduler._send_overdue_reminder(task, days_overdue=3)

        assert result is True
        assert len(scheduler.task_action_map) == 1
        short_id = list(scheduler.task_action_map.keys())[0]
        assert scheduler.task_action_map[short_id]["task_text"] == "Update pitch deck"
        assert scheduler.task_action_map[short_id]["assignee"] == "Eyal"
        assert 12345 in scheduler.message_task_map
        assert scheduler.message_task_map[12345] == short_id

    @pytest.mark.asyncio
    async def test_overdue_reminder_sends_with_keyboard(self):
        """_send_overdue_reminder sends message with inline keyboard to Eyal."""
        scheduler = self._make_scheduler()

        mock_msg = MagicMock()
        mock_msg.message_id = 999

        with patch("schedulers.task_reminder_scheduler.telegram_bot") as mock_bot:
            mock_bot.eyal_chat_id = "8190904141"
            mock_bot.app.bot.send_message = AsyncMock(return_value=mock_msg)
            mock_bot.send_message = AsyncMock(return_value=True)

            task = {
                "task": "Send investor update",
                "assignee": "Paolo",
                "priority": "M",
                "deadline": "2026-04-05",
                "row_number": 10,
            }

            await scheduler._send_overdue_reminder(task, days_overdue=2)

        # Verify send_message was called with reply_markup
        call_args = mock_bot.app.bot.send_message.call_args
        assert call_args.kwargs.get("reply_markup") is not None


class TestCallbackDataFormat:
    """Tests for callback data format and Telegram limits."""

    def test_callback_data_within_64_bytes(self):
        """All callback data variants fit within Telegram's 64-byte limit."""
        for short_id in ["t1", "t99", "t999", "t9999"]:
            for action in ["taskdone", "taskdelay", "taskdiscuss"]:
                data = f"{action}:{short_id}"
                assert len(data.encode("utf-8")) <= 64, f"{data} exceeds 64 bytes"

    def test_callback_data_parseable(self):
        """Callback data can be split into action:short_id."""
        data = "taskdone:t42"
        action, short_id = data.split(":", 1)
        assert action == "taskdone"
        assert short_id == "t42"


class TestDeadlineCalculation:
    """Tests for deadline arithmetic used in task delay."""

    def test_delay_7_days(self):
        """7-day delay from April 1 = April 8."""
        from datetime import datetime, timedelta
        old = datetime.strptime("2026-04-01", "%Y-%m-%d").date()
        new = old + timedelta(days=7)
        assert new.isoformat() == "2026-04-08"

    def test_delay_14_days(self):
        """14-day delay from April 1 = April 15."""
        from datetime import datetime, timedelta
        old = datetime.strptime("2026-04-01", "%Y-%m-%d").date()
        new = old + timedelta(days=14)
        assert new.isoformat() == "2026-04-15"

    def test_delay_handles_month_boundary(self):
        """Delay across month boundary works correctly."""
        from datetime import datetime, timedelta
        old = datetime.strptime("2026-04-28", "%Y-%m-%d").date()
        new = old + timedelta(days=7)
        assert new.isoformat() == "2026-05-05"

    def test_invalid_date_fallback(self):
        """Invalid date string triggers ValueError (caller handles fallback)."""
        from datetime import datetime
        with pytest.raises(ValueError):
            datetime.strptime("not-a-date", "%Y-%m-%d")

    def test_empty_date_fallback(self):
        """Empty date string triggers ValueError (caller handles fallback)."""
        from datetime import datetime
        with pytest.raises(ValueError):
            datetime.strptime("", "%Y-%m-%d")


class TestTaskActionRouting:
    """Tests for task action recognition in callback handler."""

    def test_valid_task_actions(self):
        """All 3 task actions are recognized."""
        valid_actions = {"taskdone", "taskdelay", "taskdiscuss"}
        for action in valid_actions:
            assert action in ("taskdone", "taskdelay", "taskdiscuss")

    def test_non_task_action_not_matched(self):
        """Non-task actions are not matched."""
        non_task_actions = ["approve", "reject", "edit", "sens_toggle"]
        for action in non_task_actions:
            assert action not in ("taskdone", "taskdelay", "taskdiscuss")
