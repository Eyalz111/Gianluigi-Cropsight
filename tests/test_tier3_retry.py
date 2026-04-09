"""
Tier 3.4 + 3.5 tests: Gmail + Telegram retry wrappers, and
format_task_tracker call at end of distribute_approved_content.

Pure unit tests — no live services. Uses AsyncMock side_effect lists to
simulate transient BrokenPipe errors and verify the retry decorator
kicks in 3 times before giving up.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# T3.4 — Gmail retry
# =============================================================================


class TestGmailRetry:
    """
    Tests the @retry decorator on gmail._execute_send by patching the
    UNDERLYING `.execute()` call, not _execute_send itself. Patching the
    decorated method would replace the whole wrapper and bypass the
    retry loop — the decorator only fires if the real method body runs.
    """

    def _build_flaky_service(self, fail_count: int):
        """
        Build a chain mock where .users().messages().send().execute()
        raises BrokenPipeError for the first `fail_count` calls, then
        returns normally.
        """
        call_count = {"n": 0}

        def flaky_execute():
            call_count["n"] += 1
            if call_count["n"] <= fail_count:
                raise BrokenPipeError("simulated broken pipe")
            return {"id": "msg-123"}

        send_mock = MagicMock()
        send_mock.execute.side_effect = flaky_execute
        messages_mock = MagicMock()
        messages_mock.send.return_value = send_mock
        users_mock = MagicMock()
        users_mock.messages.return_value = messages_mock
        service_mock = MagicMock()
        service_mock.users.return_value = users_mock

        return service_mock, call_count

    @pytest.mark.asyncio
    async def test_gmail_send_retries_on_broken_pipe(self):
        """
        BrokenPipeError on the first 2 attempts should be retried; the 3rd
        attempt succeeds and send_email returns True.
        """
        from services.gmail import gmail_service

        service_mock, call_count = self._build_flaky_service(fail_count=2)

        with patch.object(
            type(gmail_service), "service",
            new=property(lambda self: service_mock),
        ):
            result = await gmail_service.send_email(
                to=["test@example.com"],
                subject="T3.4 retry test",
                body="test body",
            )

        assert result is True, "send_email should succeed after 3 attempts"
        assert call_count["n"] == 3, (
            f"_execute_send should have retried: 2 failures + 1 success = 3 calls; "
            f"got {call_count['n']}"
        )

    @pytest.mark.asyncio
    async def test_gmail_send_returns_false_after_max_retries(self):
        """
        If all 3 attempts fail with BrokenPipeError, send_email catches
        the final exception and returns False (existing behavior).
        """
        from services.gmail import gmail_service

        service_mock, call_count = self._build_flaky_service(fail_count=999)

        with patch.object(
            type(gmail_service), "service",
            new=property(lambda self: service_mock),
        ):
            result = await gmail_service.send_email(
                to=["test@example.com"],
                subject="T3.4 max retries test",
                body="test body",
            )

        assert result is False, "send_email should return False after 3 failed retries"
        assert call_count["n"] == 3, (
            f".execute() should have been called exactly 3 times (max_attempts); "
            f"got {call_count['n']}"
        )


# =============================================================================
# T3.4 — Telegram retry
# =============================================================================


class TestTelegramRetry:
    """
    Tests the @retry decorator on telegram_bot._bot_send_message by
    patching the UNDERLYING `app.bot.send_message` call. Same reasoning
    as TestGmailRetry — patching the decorated method bypasses the retry.
    """

    def _build_flaky_app(self, fail_count: int):
        """
        Build a telegram Application mock where app.bot.send_message
        raises BrokenPipeError for the first `fail_count` calls, then
        returns normally.
        """
        call_count = {"n": 0}

        async def flaky_send(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= fail_count:
                raise BrokenPipeError("simulated broken pipe")
            return None

        bot_mock = MagicMock()
        bot_mock.send_message = AsyncMock(side_effect=flaky_send)
        app_mock = MagicMock()
        app_mock.bot = bot_mock

        return app_mock, call_count

    @pytest.mark.asyncio
    async def test_telegram_send_retries_on_broken_pipe(self):
        from services.telegram_bot import telegram_bot

        app_mock, call_count = self._build_flaky_app(fail_count=2)
        original_app = telegram_bot._app
        telegram_bot._app = app_mock
        try:
            result = await telegram_bot.send_message(
                chat_id=123456,
                text="T3.4 retry test",
            )
        finally:
            telegram_bot._app = original_app

        assert result is True
        assert call_count["n"] == 3, (
            f"app.bot.send_message should have been called 3 times "
            f"(2 failures + 1 success); got {call_count['n']}"
        )

    @pytest.mark.asyncio
    async def test_telegram_send_returns_false_after_max_retries(self):
        from services.telegram_bot import telegram_bot

        app_mock, call_count = self._build_flaky_app(fail_count=999)
        original_app = telegram_bot._app
        telegram_bot._app = app_mock
        try:
            result = await telegram_bot.send_message(
                chat_id=123456,
                text="T3.4 max retries test",
            )
        finally:
            telegram_bot._app = original_app

        assert result is False
        assert call_count["n"] == 3, (
            f"app.bot.send_message should have been called exactly 3 times "
            f"(max_attempts); got {call_count['n']}"
        )


# =============================================================================
# T3.5 — format_task_tracker call at end of distribute_approved_content
# =============================================================================


class TestFormatAfterApproval:
    @pytest.mark.asyncio
    async def test_distribute_approved_content_calls_format_task_tracker(self):
        """
        When there's at least 1 task to distribute, format_task_tracker()
        must be called exactly once after the append loop completes.
        """
        from guardrails import approval_flow
        from services import google_sheets

        # Mock the sheets service so nothing hits the real API
        fake_sheets = MagicMock()
        fake_sheets.add_task = AsyncMock(return_value=True)
        fake_sheets.format_task_tracker = AsyncMock(return_value=True)
        fake_sheets.add_decisions_batch_to_sheet = AsyncMock(return_value=True)
        fake_sheets.ensure_decisions_tab = AsyncMock(return_value=True)
        fake_sheets.add_follow_ups_as_tasks = AsyncMock(return_value=1)

        content = {
            "title": "T3.5 test meeting",
            "date": "2026-04-09",
            "summary": "test summary",
            "discussion_summary": "",
            "executive_summary": "",
            "stakeholders": [],
            "tasks": [
                {"title": "task1", "assignee": "tester", "priority": "M", "deadline": None},
                {"title": "task2", "assignee": "tester", "priority": "L", "deadline": None},
            ],
            "decisions": [],
            "follow_ups": [],
            "open_questions": [],
        }

        with patch.object(approval_flow, "sheets_service", fake_sheets), \
             patch.object(approval_flow, "supabase_client") as mock_sb, \
             patch.object(approval_flow, "gmail_service") as mock_gmail, \
             patch.object(approval_flow, "telegram_bot") as mock_tg, \
             patch.object(approval_flow, "drive_service") as mock_drive:
            mock_sb.get_meeting = MagicMock(return_value={
                "id": "test-meeting",
                "title": "T3.5 test meeting",
                "date": "2026-04-09",
                "participants": [],
                "duration_minutes": 0,
                "sensitivity": "founders",
            })
            mock_drive.save_meeting_summary_docx = AsyncMock(
                return_value={"id": "docx1", "webViewLink": "http://x"}
            )
            mock_gmail.send_email_with_attachments = AsyncMock(return_value=True)
            mock_gmail.send_email = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_tg.send_message = AsyncMock(return_value=True)

            await approval_flow.distribute_approved_content(
                meeting_id="test-meeting",
                content=content,
                sensitivity="founders",
            )

        # The format call should have been made exactly once
        assert fake_sheets.format_task_tracker.call_count == 1, (
            f"format_task_tracker should be called exactly once after the "
            f"append loop; got {fake_sheets.format_task_tracker.call_count}"
        )
        # add_task should have been called once per task
        assert fake_sheets.add_task.call_count == 2

    @pytest.mark.asyncio
    async def test_distribute_skips_format_when_no_tasks(self):
        """
        If the tasks list is empty, format_task_tracker must NOT be called.
        """
        from guardrails import approval_flow

        fake_sheets = MagicMock()
        fake_sheets.add_task = AsyncMock(return_value=True)
        fake_sheets.format_task_tracker = AsyncMock(return_value=True)
        fake_sheets.add_decisions_batch_to_sheet = AsyncMock(return_value=True)
        fake_sheets.ensure_decisions_tab = AsyncMock(return_value=True)
        fake_sheets.add_follow_ups_as_tasks = AsyncMock(return_value=0)

        content = {
            "title": "T3.5 empty tasks",
            "date": "2026-04-09",
            "summary": "test",
            "discussion_summary": "",
            "executive_summary": "",
            "stakeholders": [],
            "tasks": [],
            "decisions": [],
            "follow_ups": [],
            "open_questions": [],
        }

        with patch.object(approval_flow, "sheets_service", fake_sheets), \
             patch.object(approval_flow, "supabase_client") as mock_sb, \
             patch.object(approval_flow, "gmail_service") as mock_gmail, \
             patch.object(approval_flow, "telegram_bot") as mock_tg, \
             patch.object(approval_flow, "drive_service") as mock_drive:
            mock_sb.get_meeting = MagicMock(return_value={
                "id": "test-meeting",
                "title": "T3.5 empty tasks",
                "date": "2026-04-09",
                "participants": [],
                "duration_minutes": 0,
                "sensitivity": "founders",
            })
            mock_drive.save_meeting_summary_docx = AsyncMock(
                return_value={"id": "docx1", "webViewLink": "http://x"}
            )
            mock_gmail.send_email_with_attachments = AsyncMock(return_value=True)
            mock_gmail.send_email = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_tg.send_message = AsyncMock(return_value=True)

            await approval_flow.distribute_approved_content(
                meeting_id="test-meeting",
                content=content,
                sensitivity="founders",
            )

        assert fake_sheets.add_task.call_count == 0
        assert fake_sheets.format_task_tracker.call_count == 0, (
            "format_task_tracker should not be called when tasks list is empty"
        )

    @pytest.mark.asyncio
    async def test_distribute_format_failure_does_not_break(self):
        """
        If format_task_tracker raises, distribute_approved_content should
        continue and return a success dict — format is non-fatal.
        """
        from guardrails import approval_flow

        fake_sheets = MagicMock()
        fake_sheets.add_task = AsyncMock(return_value=True)
        fake_sheets.format_task_tracker = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        fake_sheets.add_decisions_batch_to_sheet = AsyncMock(return_value=True)
        fake_sheets.ensure_decisions_tab = AsyncMock(return_value=True)
        fake_sheets.add_follow_ups_as_tasks = AsyncMock(return_value=0)

        content = {
            "title": "T3.5 format failure",
            "date": "2026-04-09",
            "summary": "test",
            "discussion_summary": "",
            "executive_summary": "",
            "stakeholders": [],
            "tasks": [{"title": "task1", "assignee": "tester", "priority": "M", "deadline": None}],
            "decisions": [],
            "follow_ups": [],
            "open_questions": [],
        }

        with patch.object(approval_flow, "sheets_service", fake_sheets), \
             patch.object(approval_flow, "supabase_client") as mock_sb, \
             patch.object(approval_flow, "gmail_service") as mock_gmail, \
             patch.object(approval_flow, "telegram_bot") as mock_tg, \
             patch.object(approval_flow, "drive_service") as mock_drive:
            mock_sb.get_meeting = MagicMock(return_value={
                "id": "test-meeting",
                "title": "T3.5 format failure",
                "date": "2026-04-09",
                "participants": [],
                "duration_minutes": 0,
                "sensitivity": "founders",
            })
            mock_drive.save_meeting_summary_docx = AsyncMock(
                return_value={"id": "docx1", "webViewLink": "http://x"}
            )
            mock_gmail.send_email_with_attachments = AsyncMock(return_value=True)
            mock_gmail.send_email = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_tg.send_message = AsyncMock(return_value=True)

            result = await approval_flow.distribute_approved_content(
                meeting_id="test-meeting",
                content=content,
                sensitivity="founders",
            )

        # Should still return a success dict despite the format failure
        assert result is not None
        assert result.get("sheets_updated") is True
        assert result.get("tasks_added") == 1
