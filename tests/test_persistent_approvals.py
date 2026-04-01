"""
Tests for persistent approval state (v0.4 — Task #3).

Tests cover:
- Supabase CRUD methods for pending_approvals table
- submit_for_approval writes to Supabase (not in-memory dict)
- process_response reads from Supabase, deletes on approve/reject
- Timer reconstruction (future, expired, mixed)
"""

import asyncio
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# =============================================================================
# Test: Supabase CRUD for pending_approvals
# =============================================================================

class TestPendingApprovalsCRUD:
    """Tests for supabase_client pending_approvals methods."""

    def test_create_pending_approval(self):
        """Should insert a row into pending_approvals table."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [{
            "id": "uuid-1",
            "approval_id": "meeting-001",
            "content_type": "meeting_summary",
            "content": {"title": "Test"},
            "status": "pending",
            "auto_publish_at": None,
        }]
        mock_client.table.return_value.insert.return_value.execute.return_value = mock_result

        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        object.__setattr__(client, '_client', mock_client)

        result = client.create_pending_approval(
            approval_id="meeting-001",
            content_type="meeting_summary",
            content={"title": "Test"},
        )

        assert result["approval_id"] == "meeting-001"
        assert result["content_type"] == "meeting_summary"
        mock_client.table.assert_called_with("pending_approvals")

    def test_create_pending_approval_with_auto_publish(self):
        """Should include auto_publish_at when provided."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [{
            "id": "uuid-2",
            "approval_id": "meeting-002",
            "content_type": "meeting_summary",
            "content": {"title": "Test"},
            "status": "pending",
            "auto_publish_at": "2026-03-01T12:00:00",
        }]
        mock_client.table.return_value.insert.return_value.execute.return_value = mock_result

        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        object.__setattr__(client, '_client', mock_client)

        result = client.create_pending_approval(
            approval_id="meeting-002",
            content_type="meeting_summary",
            content={"title": "Test"},
            auto_publish_at="2026-03-01T12:00:00",
        )

        assert result["auto_publish_at"] == "2026-03-01T12:00:00"
        # Verify insert data included auto_publish_at
        insert_call = mock_client.table.return_value.insert.call_args[0][0]
        assert insert_call["auto_publish_at"] == "2026-03-01T12:00:00"

    def test_get_pending_approval_found(self):
        """Should return the approval record when found."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [{
            "approval_id": "meeting-001",
            "content_type": "meeting_summary",
            "content": {"title": "Found"},
            "status": "pending",
        }]
        mock_client.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_result

        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        object.__setattr__(client, '_client', mock_client)

        result = client.get_pending_approval("meeting-001")
        assert result is not None
        assert result["content"]["title"] == "Found"

    def test_get_pending_approval_not_found(self):
        """Should return None when approval_id doesn't exist."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.data = []
        mock_client.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_result

        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        object.__setattr__(client, '_client', mock_client)

        result = client.get_pending_approval("nonexistent")
        assert result is None

    def test_update_pending_approval(self):
        """Should update status and/or content."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [{
            "approval_id": "meeting-001",
            "status": "editing",
            "content": {"title": "Updated"},
        }]
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = mock_result

        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        object.__setattr__(client, '_client', mock_client)

        result = client.update_pending_approval(
            "meeting-001", status="editing", content={"title": "Updated"}
        )
        assert result["status"] == "editing"

    def test_delete_pending_approval_exists(self):
        """Should return True when a record is deleted."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [{"approval_id": "meeting-001"}]
        mock_client.table.return_value.delete.return_value.eq.return_value.execute.return_value = mock_result

        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        object.__setattr__(client, '_client', mock_client)

        assert client.delete_pending_approval("meeting-001") is True

    def test_delete_pending_approval_not_found(self):
        """Should return False when no record exists to delete."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.data = []
        mock_client.table.return_value.delete.return_value.eq.return_value.execute.return_value = mock_result

        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        object.__setattr__(client, '_client', mock_client)

        assert client.delete_pending_approval("nonexistent") is False

    def test_get_pending_auto_publishes(self):
        """Should return pending approvals with auto_publish_at set."""
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.data = [
            {"approval_id": "m-1", "auto_publish_at": "2026-03-01T12:00:00"},
            {"approval_id": "m-2", "auto_publish_at": "2026-03-01T13:00:00"},
        ]
        (
            mock_client.table.return_value
            .select.return_value
            .eq.return_value
            .not_.is_.return_value
            .execute.return_value
        ) = mock_result

        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        object.__setattr__(client, '_client', mock_client)

        results = client.get_pending_auto_publishes()
        assert len(results) == 2


# =============================================================================
# Test: submit_for_approval writes to Supabase
# =============================================================================

class TestSubmitForApprovalPersistence:
    """Tests that submit_for_approval uses Supabase instead of in-memory dict."""

    @pytest.mark.asyncio
    async def test_submit_creates_supabase_record(self):
        """submit_for_approval should call upsert_pending_approval."""
        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("guardrails.approval_flow.conversation_memory") as mock_mem,
        ):
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock(return_value={"id": "log-1"})
            mock_db.upsert_pending_approval = MagicMock(return_value={
                "approval_id": "test-001", "status": "pending"
            })
            mock_settings.APPROVAL_MODE = "manual"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"
            mock_settings.EYAL_EMAIL = "eyal@test.com"

            from guardrails.approval_flow import submit_for_approval

            content = {"title": "Sprint", "summary": "Notes", "discussion_summary": "D"}

            await submit_for_approval(
                content_type="meeting_summary",
                content=content,
                meeting_id="test-001",
            )

            # Should have created/upserted a persistent record
            mock_db.upsert_pending_approval.assert_called_once()
            call_kwargs = mock_db.upsert_pending_approval.call_args.kwargs
            assert call_kwargs["approval_id"] == "test-001"
            assert call_kwargs["content_type"] == "meeting_summary"
            assert call_kwargs["content"] is content
            # Manual mode → no auto_publish_at
            assert call_kwargs["auto_publish_at"] is None

    @pytest.mark.asyncio
    async def test_submit_auto_review_sets_auto_publish_at(self):
        """In auto_review mode, submit should set auto_publish_at timestamp."""
        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("guardrails.approval_flow.conversation_memory") as mock_mem,
            patch("guardrails.approval_flow.schedule_auto_publish"),
        ):
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_db.upsert_pending_approval = MagicMock(return_value={
                "approval_id": "auto-001", "status": "pending"
            })
            mock_settings.APPROVAL_MODE = "auto_review"
            mock_settings.AUTO_REVIEW_WINDOW_MINUTES = 60
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"
            mock_settings.EYAL_EMAIL = "eyal@test.com"

            from guardrails.approval_flow import submit_for_approval

            content = {"title": "Auto", "summary": "S", "discussion_summary": "D"}

            await submit_for_approval(
                content_type="meeting_summary",
                content=content,
                meeting_id="auto-001",
            )

            call_kwargs = mock_db.upsert_pending_approval.call_args.kwargs
            assert call_kwargs["auto_publish_at"] is not None
            # auto_publish_at should be ~60 minutes from now
            ts = datetime.fromisoformat(call_kwargs["auto_publish_at"])
            assert ts > datetime.now()


# =============================================================================
# Test: process_response uses Supabase
# =============================================================================

class TestProcessResponsePersistence:
    """Tests that process_response reads/deletes from Supabase."""

    @pytest.mark.asyncio
    async def test_approve_reads_from_supabase_and_deletes(self):
        """Approve should get_pending_approval then delete_pending_approval."""
        mock_meeting = {
            "id": "persist-001",
            "title": "Persistent Meeting",
            "summary": "Notes.",
            "date": "2026-03-01",
            "sensitivity": "normal",
            "approval_status": "pending",
        }
        pending_row = {
            "approval_id": "persist-001",
            "content_type": "meeting_summary",
            "content": {"title": "Persistent Meeting", "summary": "Notes."},
            "status": "pending",
        }

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.update_approval_status", new_callable=AsyncMock),
            patch("guardrails.approval_flow.cancel_auto_publish"),
            patch("guardrails.approval_flow.distribute_approved_content", new_callable=AsyncMock) as mock_dist,
        ):
            mock_db.get_pending_approval = MagicMock(return_value=pending_row)
            mock_db.delete_pending_approval = MagicMock(return_value=True)
            mock_db.get_meeting = MagicMock(return_value=mock_meeting)
            mock_db.list_decisions = MagicMock(return_value=[])
            mock_db.get_tasks = MagicMock(return_value=[])
            mock_db.list_follow_up_meetings = MagicMock(return_value=[])
            mock_db.get_open_questions = MagicMock(return_value=[])
            mock_dist.return_value = {"telegram_sent": True}

            from guardrails.approval_flow import process_response

            result = await process_response("persist-001", "approve")

            assert result["action"] == "approved"
            mock_db.get_pending_approval.assert_called_once_with("persist-001")
            mock_db.delete_pending_approval.assert_called_once_with("persist-001")

    @pytest.mark.asyncio
    async def test_reject_deletes_from_supabase(self):
        """Reject should delete the pending approval from Supabase."""
        pending_row = {
            "approval_id": "reject-persist-001",
            "content_type": "meeting_summary",
            "content": {"title": "Reject Me"},
            "status": "pending",
        }
        mock_meeting = {
            "id": "reject-persist-001",
            "title": "Reject Me",
            "approval_status": "pending",
        }

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.update_approval_status", new_callable=AsyncMock),
            patch("guardrails.approval_flow.cancel_auto_publish"),
        ):
            mock_db.get_pending_approval = MagicMock(return_value=pending_row)
            mock_db.delete_pending_approval = MagicMock(return_value=True)
            mock_db.get_meeting = MagicMock(return_value=mock_meeting)
            mock_db.log_action = MagicMock()

            from guardrails.approval_flow import process_response

            result = await process_response("reject-persist-001", "reject")

            assert result["action"] == "rejected"
            mock_db.delete_pending_approval.assert_called_once_with("reject-persist-001")


# =============================================================================
# Test: Timer Reconstruction
# =============================================================================

class TestReconstructAutoPublishTimers:
    """Tests for reconstruct_auto_publish_timers() on startup."""

    @pytest.mark.asyncio
    async def test_no_timers_returns_zero(self):
        """Should return 0 when no pending auto-publishes exist."""
        with patch("guardrails.approval_flow.supabase_client") as mock_db:
            mock_db.get_pending_auto_publishes = MagicMock(return_value=[])

            from guardrails.approval_flow import reconstruct_auto_publish_timers

            count = await reconstruct_auto_publish_timers()
            assert count == 0

    @pytest.mark.asyncio
    async def test_future_timer_schedules_task(self):
        """Should schedule asyncio task for future auto_publish_at."""
        future_time = (datetime.now() + timedelta(minutes=30)).astimezone().isoformat()

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow._pending_auto_publishes", {}) as pending,
            patch("guardrails.approval_flow.asyncio") as mock_asyncio,
        ):
            mock_db.get_pending_auto_publishes = MagicMock(return_value=[
                {"approval_id": "future-001", "auto_publish_at": future_time},
            ])
            mock_task = MagicMock()
            mock_asyncio.create_task.return_value = mock_task

            from guardrails.approval_flow import reconstruct_auto_publish_timers

            count = await reconstruct_auto_publish_timers()

            assert count == 1
            mock_asyncio.create_task.assert_called_once()
            assert "future-001" in pending

    @pytest.mark.asyncio
    async def test_expired_timer_auto_approves(self):
        """Should auto-approve immediately when auto_publish_at is in the past."""
        past_time = (datetime.now() - timedelta(minutes=10)).astimezone().isoformat()

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow._pending_auto_publishes", {}),
            patch("guardrails.approval_flow.asyncio") as mock_asyncio,
        ):
            mock_db.get_pending_auto_publishes = MagicMock(return_value=[
                {"approval_id": "expired-001", "auto_publish_at": past_time},
            ])
            mock_task = MagicMock()
            mock_asyncio.create_task.return_value = mock_task

            from guardrails.approval_flow import reconstruct_auto_publish_timers

            count = await reconstruct_auto_publish_timers()

            assert count == 1
            # Should have created a task (the immediate auto-approve)
            mock_asyncio.create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_mixed_timers(self):
        """Should handle both future and expired timers."""
        future_time = (datetime.now() + timedelta(minutes=30)).astimezone().isoformat()
        past_time = (datetime.now() - timedelta(minutes=10)).astimezone().isoformat()

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow._pending_auto_publishes", {}),
            patch("guardrails.approval_flow.asyncio") as mock_asyncio,
        ):
            mock_db.get_pending_auto_publishes = MagicMock(return_value=[
                {"approval_id": "future-002", "auto_publish_at": future_time},
                {"approval_id": "expired-002", "auto_publish_at": past_time},
            ])
            mock_task = MagicMock()
            mock_asyncio.create_task.return_value = mock_task

            from guardrails.approval_flow import reconstruct_auto_publish_timers

            count = await reconstruct_auto_publish_timers()

            assert count == 2
            # Should have created 2 tasks (one scheduled, one immediate)
            assert mock_asyncio.create_task.call_count == 2
