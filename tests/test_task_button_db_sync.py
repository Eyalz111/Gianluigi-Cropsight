"""
Tests for the Telegram task-reminder button -> DB + Sheets sync chain.

Covers the Tier 1 bug fixes:
- T1.2: update_task sets updated_at and raises ValueError on missing task
- T1.3: reminder scheduler resolves and stores task_id
- T1.4: _execute_task_update_from_reminder prefers task_id, alerts on failure
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock


# =============================================================================
# T1.2 — update_task sets updated_at and raises on missing task
# =============================================================================


class TestUpdateTaskUpdatedAt:
    def _make_client_with_mock(self, exec_data):
        """Build a SupabaseClient with a mock chain that captures the update payload."""
        from services.supabase_client import SupabaseClient

        client = SupabaseClient()
        mock_supabase = MagicMock()
        mock_chain = MagicMock()
        # update() returns the same chain so .eq().execute() works; side_effect
        # records the payload but must still return mock_chain
        mock_chain.update.return_value = mock_chain
        mock_chain.eq.return_value = mock_chain
        mock_chain.execute.return_value = MagicMock(data=exec_data)
        mock_supabase.table.return_value = mock_chain
        object.__setattr__(client, "_client", mock_supabase)
        return client, mock_chain

    def test_sets_updated_at_on_every_update(self):
        """update_task should always include updated_at in the payload."""
        client, mock_chain = self._make_client_with_mock([{"id": "t1", "status": "done"}])
        result = client.update_task("t1", status="done")

        mock_chain.update.assert_called_once()
        payload = mock_chain.update.call_args[0][0]
        assert "updated_at" in payload
        assert payload["status"] == "done"
        assert result["id"] == "t1"

    def test_raises_value_error_when_not_found(self):
        """update_task should raise ValueError when result.data is empty."""
        client, _ = self._make_client_with_mock([])

        with pytest.raises(ValueError, match="not found or not updated"):
            client.update_task("nonexistent-id", status="done")

    def test_passes_deadline_and_other_updates(self):
        """Deadline and **other_updates flow through to the payload."""
        from datetime import date

        client, mock_chain = self._make_client_with_mock([{"id": "t1"}])
        client.update_task("t1", status="in_progress", deadline=date(2026, 5, 1), priority="H")

        payload = mock_chain.update.call_args[0][0]
        assert payload["status"] == "in_progress"
        assert payload["priority"] == "H"
        assert payload["deadline"] == "2026-05-01"
        assert "updated_at" in payload


# =============================================================================
# T1.3 — reminder scheduler resolves task_id
# =============================================================================


class TestReminderResolvesTaskId:
    def test_resolve_db_task_id_finds_match(self):
        """_resolve_db_task_id returns the DB id when title matches."""
        from schedulers.task_reminder_scheduler import TaskReminderScheduler

        scheduler = TaskReminderScheduler()

        with patch("schedulers.task_reminder_scheduler.supabase_client") as mock_sb:
            # Single query with status=None now returns all active statuses
            mock_sb.get_tasks.return_value = [
                {"id": "db-task-1", "title": "Send the Monday report", "assignee": "Eyal", "status": "in_progress"},
            ]
            result = scheduler._resolve_db_task_id("Send the Monday report", "Eyal")

        assert result == "db-task-1"
        # Single status=None query (covers pending + in_progress + overdue)
        assert mock_sb.get_tasks.call_count == 1
        # Verify it was called with status=None
        call_kwargs = mock_sb.get_tasks.call_args.kwargs
        assert call_kwargs.get("status") is None

    def test_resolve_db_task_id_includes_in_progress(self):
        """Bug fix: tasks in 'in_progress' status must be findable.

        Previously the lookup only queried 'pending' and 'overdue' statuses,
        causing reminder buttons to fail with title-match-fallback errors
        when the task was already in_progress.
        """
        from schedulers.task_reminder_scheduler import TaskReminderScheduler

        scheduler = TaskReminderScheduler()

        with patch("schedulers.task_reminder_scheduler.supabase_client") as mock_sb:
            mock_sb.get_tasks.return_value = [
                {"id": "ip-1", "title": "Already started task", "assignee": "Eyal", "status": "in_progress"},
            ]
            result = scheduler._resolve_db_task_id("Already started task", "Eyal")

        assert result == "ip-1"

    def test_resolve_db_task_id_case_insensitive(self):
        """Match is case-insensitive and strips whitespace."""
        from schedulers.task_reminder_scheduler import TaskReminderScheduler

        scheduler = TaskReminderScheduler()
        with patch("schedulers.task_reminder_scheduler.supabase_client") as mock_sb:
            mock_sb.get_tasks.side_effect = [
                [{"id": "db-42", "title": "  Send DECK  ", "assignee": "Eyal"}],
                [],
            ]
            result = scheduler._resolve_db_task_id("send deck", "Eyal")
        assert result == "db-42"

    def test_resolve_db_task_id_returns_none_on_no_match(self):
        """Returns None when no DB task matches."""
        from schedulers.task_reminder_scheduler import TaskReminderScheduler

        scheduler = TaskReminderScheduler()
        with patch("schedulers.task_reminder_scheduler.supabase_client") as mock_sb:
            mock_sb.get_tasks.return_value = []
            result = scheduler._resolve_db_task_id("Something unmatched", "Ghost")
        assert result is None

    def test_resolve_db_task_id_handles_exception(self):
        """Returns None if DB query raises, without propagating."""
        from schedulers.task_reminder_scheduler import TaskReminderScheduler

        scheduler = TaskReminderScheduler()
        with patch("schedulers.task_reminder_scheduler.supabase_client") as mock_sb:
            mock_sb.get_tasks.side_effect = Exception("connection refused")
            result = scheduler._resolve_db_task_id("Send deck", "Eyal")
        assert result is None


# =============================================================================
# T1.4 — unified writer prefers task_id, alerts on failure
# =============================================================================


class TestExecuteTaskUpdateFromReminder:
    @pytest.fixture
    def bot(self):
        from services.telegram_bot import TelegramBot
        # Minimal instantiation without actually starting the bot
        b = TelegramBot.__new__(TelegramBot)
        return b

    @pytest.mark.asyncio
    async def test_prefers_task_id_when_provided(self, bot):
        """If task_info has task_id, use it directly — no title search."""
        task_info = {
            "task_id": "direct-id-1",
            "task_text": "Send deck",
            "assignee": "Eyal",
            "row_number": 5,
            "deadline": "2026-04-10",
        }

        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            mock_sb.update_task.return_value = {"id": "direct-id-1"}
            mock_sheets.update_task_row = AsyncMock()

            await bot._execute_task_update_from_reminder(task_info, status="done")

            # update_task called with the direct id
            mock_sb.update_task.assert_called_once()
            assert mock_sb.update_task.call_args[0][0] == "direct-id-1"
            # get_tasks should NOT have been called (title-match fallback skipped)
            mock_sb.get_tasks.assert_not_called()
            # Sheets updated with kwargs
            mock_sheets.update_task_row.assert_called_once_with(5, status="done")
            # No alerts fired
            mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_title_match_when_task_id_missing(self, bot):
        """Legacy reminders without task_id still work via title fallback."""
        task_info = {
            "task_text": "Send deck",
            "assignee": "Eyal",
            "row_number": 3,
            "deadline": "2026-04-10",
        }

        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            # Single status=None query returns all active tasks for the assignee
            mock_sb.get_tasks.return_value = [
                {"id": "fallback-id", "title": "Send deck", "status": "pending"},
            ]
            mock_sb.update_task.return_value = {"id": "fallback-id"}
            mock_sheets.update_task_row = AsyncMock()

            await bot._execute_task_update_from_reminder(task_info, status="done")

            mock_sb.update_task.assert_called_once()
            assert mock_sb.update_task.call_args[0][0] == "fallback-id"
            mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_title_match_finds_in_progress_task(self, bot):
        """Bug fix: title fallback must include in_progress tasks.

        Regression where reminder clicks failed for tasks already started.
        """
        task_info = {
            "task_text": "Already started task",
            "assignee": "Eyal",
            "row_number": 5,
            "deadline": "2026-04-10",
        }

        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            mock_sb.get_tasks.return_value = [
                {"id": "ip-task-1", "title": "Already started task", "status": "in_progress"},
            ]
            mock_sb.update_task.return_value = {"id": "ip-task-1"}
            mock_sheets.update_task_row = AsyncMock()

            await bot._execute_task_update_from_reminder(task_info, status="done")

            mock_sb.update_task.assert_called_once()
            assert mock_sb.update_task.call_args[0][0] == "ip-task-1"
            mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_alerts_on_db_update_failure(self, bot):
        """CRITICAL alert fires when DB update raises."""
        task_info = {
            "task_id": "id-1",
            "task_text": "Send deck",
            "assignee": "Eyal",
            "row_number": 3,
            "deadline": "2026-04-10",
        }

        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            mock_sb.update_task.side_effect = ValueError("Task id-1 not found or not updated")
            mock_sheets.update_task_row = AsyncMock()

            await bot._execute_task_update_from_reminder(task_info, status="done")

            # Sheets still succeeded, but DB failed — alert should fire
            mock_alert.assert_called_once()
            args, kwargs = mock_alert.call_args
            # Severity should be CRITICAL; message should mention DB
            from services.alerting import AlertSeverity
            assert args[0] == AlertSeverity.CRITICAL
            assert "DB:" in args[2]

    @pytest.mark.asyncio
    async def test_alerts_on_sheets_update_failure(self, bot):
        """CRITICAL alert fires when Sheets update raises."""
        task_info = {
            "task_id": "id-1",
            "task_text": "Send deck",
            "assignee": "Eyal",
            "row_number": 3,
            "deadline": "2026-04-10",
        }

        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            mock_sb.update_task.return_value = {"id": "id-1"}
            mock_sheets.update_task_row = AsyncMock(side_effect=Exception("Sheets 500"))

            await bot._execute_task_update_from_reminder(task_info, status="done")

            mock_alert.assert_called_once()
            args, _ = mock_alert.call_args
            assert "Sheets:" in args[2]

    @pytest.mark.asyncio
    async def test_alerts_on_both_failures(self, bot):
        """Alert message mentions both failures when both sides fail."""
        task_info = {
            "task_id": "id-1",
            "task_text": "Send deck",
            "assignee": "Eyal",
            "row_number": 3,
            "deadline": "2026-04-10",
        }

        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            mock_sb.update_task.side_effect = Exception("DB down")
            mock_sheets.update_task_row = AsyncMock(side_effect=Exception("Sheets down"))

            await bot._execute_task_update_from_reminder(task_info, status="done")

            mock_alert.assert_called_once()
            args, _ = mock_alert.call_args
            assert "DB:" in args[2]
            assert "Sheets:" in args[2]

    @pytest.mark.asyncio
    async def test_no_match_no_row_still_runs(self, bot):
        """
        If no task_id and no title match but also no row_number, the function
        should still return gracefully (with an alert about the DB miss).
        """
        task_info = {
            "task_text": "Ghost task",
            "assignee": "Nobody",
            "row_number": None,
            "deadline": "",
        }

        with patch("services.supabase_client.supabase_client") as mock_sb, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            mock_sb.get_tasks.return_value = []

            result = await bot._execute_task_update_from_reminder(task_info, status="done")

            # No match found for DB -> alert should fire
            mock_alert.assert_called_once()
            # Sheets was skipped because no row_number (not a failure)
            args, _ = mock_alert.call_args
            assert "DB:" in args[2]
            assert "Sheets:" not in args[2]
            # No deadline requested
            assert result is None
