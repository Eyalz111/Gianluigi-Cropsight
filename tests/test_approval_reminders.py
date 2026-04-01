"""Tests for approval reminders, expiry, and queue awareness."""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta


@pytest.fixture
def mock_settings():
    with patch("guardrails.approval_flow.settings") as mock:
        mock.APPROVAL_MODE = "manual"
        mock.AUTO_REVIEW_WINDOW_MINUTES = 60
        mock.APPROVAL_REMINDER_ENABLED = True
        mock.APPROVAL_REMINDER_HOURS = "2,6"
        mock.approval_reminder_hours_list = [2, 6]
        mock.TELEGRAM_EYAL_CHAT_ID = "123"
        mock.EYAL_EMAIL = "eyal@test.com"
        yield mock


@pytest.fixture
def mock_supabase():
    with patch("guardrails.approval_flow.supabase_client") as mock:
        mock.get_pending_approvals_by_status.return_value = []
        mock.upsert_pending_approval.return_value = {"approval_id": "test-id"}
        mock.get_pending_approval.return_value = None
        mock.log_action.return_value = None
        mock.expire_pending_approvals.return_value = []
        yield mock


@pytest.fixture
def mock_telegram():
    with patch("guardrails.approval_flow.telegram_bot") as mock:
        mock.send_approval_request = AsyncMock(return_value=True)
        mock.send_to_eyal = AsyncMock(return_value=True)
        yield mock


@pytest.fixture
def mock_gmail():
    with patch("guardrails.approval_flow.gmail_service") as mock:
        mock.send_approval_request = AsyncMock(return_value=True)
        yield mock


@pytest.fixture
def mock_conversation():
    with patch("guardrails.approval_flow.conversation_memory") as mock:
        mock.inject_approval_context = MagicMock()
        yield mock


class TestApprovalReminders:
    @pytest.mark.asyncio
    async def test_schedule_reminders_creates_tasks(self, mock_settings, mock_supabase, mock_telegram):
        from guardrails.approval_flow import schedule_approval_reminders, _pending_reminders, cancel_approval_reminders

        schedule_approval_reminders("test-1", "meeting_summary")
        assert "test-1" in _pending_reminders
        assert len(_pending_reminders["test-1"]) == 2  # 2h and 6h

        # Cleanup
        cancel_approval_reminders("test-1")

    @pytest.mark.asyncio
    async def test_cancel_reminders(self, mock_settings, mock_supabase, mock_telegram):
        from guardrails.approval_flow import schedule_approval_reminders, cancel_approval_reminders, _pending_reminders

        schedule_approval_reminders("test-2", "meeting_summary")
        assert "test-2" in _pending_reminders

        cancel_approval_reminders("test-2")
        assert "test-2" not in _pending_reminders

    @pytest.mark.asyncio
    async def test_disabled_reminders(self, mock_settings, mock_supabase, mock_telegram):
        mock_settings.APPROVAL_REMINDER_ENABLED = False
        from guardrails.approval_flow import schedule_approval_reminders, _pending_reminders

        schedule_approval_reminders("test-3", "meeting_summary")
        assert "test-3" not in _pending_reminders


class TestSendApprovalReminder:
    @pytest.mark.asyncio
    async def test_sends_when_still_pending(self, mock_settings, mock_supabase, mock_telegram):
        from guardrails.approval_flow import _send_approval_reminder

        mock_supabase.get_pending_approval.return_value = {
            "status": "pending",
            "content": {"title": "Test Meeting"},
        }

        # Use hours=0 to avoid real sleep
        with patch("guardrails.approval_flow.asyncio.sleep", new_callable=AsyncMock):
            await _send_approval_reminder("test-id", 0, "meeting_summary")
        mock_telegram.send_to_eyal.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_already_approved(self, mock_settings, mock_supabase, mock_telegram):
        from guardrails.approval_flow import _send_approval_reminder

        mock_supabase.get_pending_approval.return_value = {
            "status": "approved",
            "content": {"title": "Test Meeting"},
        }

        with patch("guardrails.approval_flow.asyncio.sleep", new_callable=AsyncMock):
            await _send_approval_reminder("test-id", 0, "meeting_summary")
        mock_telegram.send_to_eyal.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_not_found(self, mock_settings, mock_supabase, mock_telegram):
        from guardrails.approval_flow import _send_approval_reminder

        mock_supabase.get_pending_approval.return_value = None

        with patch("guardrails.approval_flow.asyncio.sleep", new_callable=AsyncMock):
            await _send_approval_reminder("test-id", 0, "meeting_summary")
        mock_telegram.send_to_eyal.assert_not_called()


class TestApprovalExpiry:
    @pytest.mark.asyncio
    async def test_expire_stale_approvals_sends_notification(self, mock_settings, mock_supabase, mock_telegram):
        from guardrails.approval_flow import expire_stale_approvals

        mock_supabase.expire_pending_approvals.return_value = [
            {"approval_id": "exp-1", "content_type": "morning_brief", "content": {"title": "Morning Brief"}},
        ]
        expired = await expire_stale_approvals()
        assert len(expired) == 1
        mock_telegram.send_to_eyal.assert_called_once()
        call_args = mock_telegram.send_to_eyal.call_args[0][0]
        assert "Morning Brief" in call_args

    @pytest.mark.asyncio
    async def test_expire_stale_approvals_empty(self, mock_settings, mock_supabase, mock_telegram):
        from guardrails.approval_flow import expire_stale_approvals

        mock_supabase.expire_pending_approvals.return_value = []
        expired = await expire_stale_approvals()
        assert len(expired) == 0
        mock_telegram.send_to_eyal.assert_not_called()


class TestQueueAwareness:
    @pytest.mark.asyncio
    async def test_queue_note_when_pending_exists(self, mock_settings, mock_supabase, mock_telegram, mock_gmail, mock_conversation):
        """submit_for_approval should detect existing pending of same type."""
        mock_supabase.get_pending_approvals_by_status.return_value = [
            {"approval_id": "other-id", "content_type": "meeting_summary", "status": "pending"},
        ]

        from guardrails.approval_flow import submit_for_approval
        result = await submit_for_approval(
            content_type="meeting_summary",
            content={"title": "Test Meeting", "summary": "Test"},
            meeting_id="new-id",
        )
        assert result["status"] == "pending"


class TestExpiryCalculation:
    @pytest.mark.asyncio
    async def test_morning_brief_gets_24h_expiry(self, mock_settings, mock_supabase, mock_telegram, mock_gmail, mock_conversation):
        from guardrails.approval_flow import submit_for_approval

        await submit_for_approval(
            content_type="morning_brief",
            content={"title": "Morning Brief", "formatted": "test", "stats": {}},
            meeting_id="brief-id",
        )

        # Check that upsert_pending_approval was called with expires_at set
        call_kwargs = mock_supabase.upsert_pending_approval.call_args
        expires_at = call_kwargs.kwargs.get("expires_at") or call_kwargs[1].get("expires_at")
        assert expires_at is not None

    @pytest.mark.asyncio
    async def test_meeting_summary_no_expiry(self, mock_settings, mock_supabase, mock_telegram, mock_gmail, mock_conversation):
        from guardrails.approval_flow import submit_for_approval

        await submit_for_approval(
            content_type="meeting_summary",
            content={"title": "Test", "summary": "test"},
            meeting_id="summary-id",
        )

        call_kwargs = mock_supabase.upsert_pending_approval.call_args
        expires_at = call_kwargs.kwargs.get("expires_at") or call_kwargs[1].get("expires_at")
        assert expires_at is None


class TestSettingsProperties:
    def test_approval_reminder_hours_list(self):
        from config.settings import Settings
        s = Settings(
            APPROVAL_REMINDER_HOURS="2,6,12",
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.approval_reminder_hours_list == [2, 6, 12]

    def test_approval_reminder_hours_empty(self):
        from config.settings import Settings
        s = Settings(
            APPROVAL_REMINDER_HOURS="",
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.approval_reminder_hours_list == []

    def test_morning_brief_skip_days(self):
        from config.settings import Settings
        s = Settings(
            MORNING_BRIEF_SKIP_DAYS="Saturday,Sunday",
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.morning_brief_skip_days_list == ["Saturday", "Sunday"]

    def test_weekly_digest_settings(self):
        from config.settings import Settings
        s = Settings(
            WEEKLY_DIGEST_DAY="4",
            WEEKLY_DIGEST_HOUR="14",
            WEEKLY_DIGEST_WINDOW_HOURS="2",
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.WEEKLY_DIGEST_DAY == 4
        assert s.WEEKLY_DIGEST_HOUR == 14
        assert s.WEEKLY_DIGEST_WINDOW_HOURS == 2

    def test_rag_weight_settings(self):
        from config.settings import Settings
        s = Settings(
            RAG_WEIGHT_DEBRIEF="1.5",
            RAG_WEIGHT_DECISION="1.3",
            RAG_WEIGHT_GANTT="0.7",
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.RAG_WEIGHT_DEBRIEF == 1.5
        assert s.RAG_WEIGHT_DECISION == 1.3
        assert s.RAG_WEIGHT_GANTT == 0.7

    def test_document_poll_interval_default(self):
        from config.settings import Settings
        s = Settings(
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.DOCUMENT_POLL_INTERVAL == 900

    def test_health_report_enabled_default(self):
        from config.settings import Settings
        s = Settings(
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.DAILY_HEALTH_REPORT_ENABLED is True
