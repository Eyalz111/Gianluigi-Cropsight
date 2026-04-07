"""
Tests for approval routing across content types (Changes 1+3).

The approval flow now supports 3 content types:
- "meeting_summary" (original — dispatches to distribute_approved_content)
- "meeting_prep" (new — dispatches to distribute_approved_prep)
- "weekly_digest" (new — dispatches to distribute_approved_digest)

v0.4: Approvals are now persisted in Supabase (not in-memory dict).

Tests:
1. submit_for_approval() persists to Supabase and sends type-specific messages
2. process_response() dispatches to correct distributor when approved
3. distribute_approved_prep() — sensitive vs normal distribution
4. distribute_approved_digest() — email + Telegram group posting
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =============================================================================
# Test: submit_for_approval routing by content type
# =============================================================================

class TestSubmitForApprovalRouting:
    """Tests for submit_for_approval() — content-type branching."""

    @pytest.mark.asyncio
    async def test_meeting_prep_persists_and_sends_prep_message(self):
        """Meeting prep should be persisted to Supabase and send prep-specific messages."""
        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("guardrails.approval_flow.conversation_memory") as mock_mem,
        ):
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock(return_value={"id": "log-1"})
            mock_db.upsert_pending_approval = MagicMock(return_value={"approval_id": "prep-001"})
            mock_settings.APPROVAL_MODE = "manual"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"
            mock_settings.EYAL_EMAIL = "eyal@test.com"

            from guardrails.approval_flow import submit_for_approval

            content = {
                "title": "Investor Call",
                "summary": "Prep for investor meeting with slides.",
                "sensitivity": "founders",
                "start_time": "2026-02-27 10:00",
            }

            result = await submit_for_approval(
                content_type="meeting_prep",
                content=content,
                meeting_id="prep-001",
            )

            # Should persist to Supabase with correct type
            mock_db.upsert_pending_approval.assert_called_once()
            call_kwargs = mock_db.upsert_pending_approval.call_args.kwargs
            assert call_kwargs["approval_id"] == "prep-001"
            assert call_kwargs["content_type"] == "meeting_prep"
            assert call_kwargs["content"] is content

            # Return value should indicate pending
            assert result["status"] == "pending"
            assert result["approval_id"] == "prep-001"
            assert result["telegram_sent"] is True
            assert result["email_sent"] is True

            # Telegram: meeting_prep now uses send_to_eyal directly (minimal card)
            mock_tg.send_to_eyal.assert_awaited_once()
            tg_msg = mock_tg.send_to_eyal.call_args[0][0]
            assert "Investor Call" in tg_msg

            # Email should include "Meeting Prep:" prefix
            email_call_kwargs = mock_gmail.send_approval_request.call_args.kwargs
            assert "Meeting Prep:" in email_call_kwargs["meeting_title"]

            # Audit log should record content_type
            log_kwargs = mock_db.log_action.call_args.kwargs
            assert log_kwargs["details"]["content_type"] == "meeting_prep"

    @pytest.mark.asyncio
    async def test_weekly_digest_persists_and_sends_digest_message(self):
        """Weekly digest should be persisted to Supabase and send digest-specific messages."""
        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("guardrails.approval_flow.conversation_memory") as mock_mem,
        ):
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock(return_value={"id": "log-2"})
            mock_db.upsert_pending_approval = MagicMock(return_value={"approval_id": "digest-001"})
            mock_settings.APPROVAL_MODE = "manual"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"
            mock_settings.EYAL_EMAIL = "eyal@test.com"

            from guardrails.approval_flow import submit_for_approval

            content = {
                "title": "Weekly Digest",
                "summary": "Summary of the week.",
                "week_of": "2026-02-17",
                "meetings_count": 5,
                "decisions_count": 12,
                "tasks_completed": 8,
                "tasks_overdue": 2,
                "digest_document": "Full digest doc text here...",
            }

            result = await submit_for_approval(
                content_type="weekly_digest",
                content=content,
                meeting_id="digest-001",
            )

            # Should persist to Supabase with correct type
            call_kwargs = mock_db.upsert_pending_approval.call_args.kwargs
            assert call_kwargs["content_type"] == "weekly_digest"
            assert call_kwargs["content"] is content

            # Return value
            assert result["status"] == "pending"
            assert result["telegram_sent"] is True
            assert result["email_sent"] is True

            # Telegram message should include "Weekly Digest"
            tg_call_kwargs = mock_tg.send_approval_request.call_args.kwargs
            assert "Weekly Digest" in tg_call_kwargs["meeting_title"]

            # Email should include "Weekly Digest"
            email_call_kwargs = mock_gmail.send_approval_request.call_args.kwargs
            assert "Weekly Digest" in email_call_kwargs["meeting_title"]
            assert "Week of" in email_call_kwargs["meeting_title"]

    @pytest.mark.asyncio
    async def test_meeting_summary_persists_and_uses_default_branch(self):
        """Meeting summary (default) should persist to Supabase and use original flow."""
        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("guardrails.approval_flow.conversation_memory") as mock_mem,
        ):
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock(return_value={"id": "log-3"})
            mock_db.upsert_pending_approval = MagicMock(return_value={"approval_id": "summary-001"})
            mock_settings.APPROVAL_MODE = "manual"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"
            mock_settings.EYAL_EMAIL = "eyal@test.com"

            from guardrails.approval_flow import submit_for_approval

            content = {
                "title": "Sprint Planning",
                "summary": "We planned sprint 12.",
                "decisions": [{"description": "Use React"}],
                "tasks": [{"title": "Build UI", "assignee": "Eyal"}],
                "follow_ups": [],
                "open_questions": ["How to deploy?"],
                "discussion_summary": "Discussed sprint tasks.",
            }

            result = await submit_for_approval(
                content_type="meeting_summary",
                content=content,
                meeting_id="summary-001",
            )

            # Should persist to Supabase
            call_kwargs = mock_db.upsert_pending_approval.call_args.kwargs
            assert call_kwargs["content_type"] == "meeting_summary"

            # Default branch sends decisions, tasks, follow_ups, open_questions
            tg_call_kwargs = mock_tg.send_approval_request.call_args.kwargs
            assert tg_call_kwargs["decisions"] == content["decisions"]
            assert tg_call_kwargs["tasks"] == content["tasks"]

            # Email now uses discussion_summary (matches Telegram)
            email_call_kwargs = mock_gmail.send_approval_request.call_args.kwargs
            assert email_call_kwargs["summary_preview"] == content["discussion_summary"]
            assert email_call_kwargs["decisions"] == content["decisions"]
            assert email_call_kwargs["tasks"] == content["tasks"]

    @pytest.mark.asyncio
    async def test_prep_telegram_preview_includes_sensitivity(self):
        """Meeting prep Telegram preview should show sensitivity (no Drive link before approval)."""
        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("guardrails.approval_flow.conversation_memory") as mock_mem,
        ):
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_db.upsert_pending_approval = MagicMock(return_value={})
            mock_settings.APPROVAL_MODE = "manual"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"
            mock_settings.EYAL_EMAIL = "eyal@test.com"

            from guardrails.approval_flow import submit_for_approval

            content = {
                "title": "Board Meeting",
                "summary": "Important board meeting prep.",
                "sensitivity": "ceo",
                "start_time": "2026-03-01 14:00",
            }

            await submit_for_approval(
                content_type="meeting_prep",
                content=content,
                meeting_id="prep-002",
            )

            # Meeting prep now uses send_to_eyal directly (minimal card with HTML)
            mock_tg.send_to_eyal.assert_awaited_once()
            card = mock_tg.send_to_eyal.call_args[0][0]
            assert "ceo" in card.lower()
            assert "Board Meeting" in card

    @pytest.mark.asyncio
    async def test_digest_telegram_preview_includes_stats(self):
        """Weekly digest Telegram preview should include meeting/task stats."""
        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("guardrails.approval_flow.conversation_memory") as mock_mem,
        ):
            mock_tg.send_approval_request = AsyncMock(return_value=True)
            mock_gmail.send_approval_request = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_db.upsert_pending_approval = MagicMock(return_value={})
            mock_settings.APPROVAL_MODE = "manual"
            mock_settings.TELEGRAM_EYAL_CHAT_ID = "999"
            mock_settings.EYAL_EMAIL = "eyal@test.com"

            from guardrails.approval_flow import submit_for_approval

            content = {
                "title": "Weekly Digest",
                "summary": "",
                "week_of": "2026-02-17",
                "meetings_count": 7,
                "decisions_count": 15,
                "tasks_completed": 10,
                "tasks_overdue": 3,
                "digest_document": "Full digest content.",
            }

            await submit_for_approval(
                content_type="weekly_digest",
                content=content,
                meeting_id="digest-002",
            )

            tg_call_kwargs = mock_tg.send_approval_request.call_args.kwargs
            preview = tg_call_kwargs["summary_preview"]
            assert "Meetings: 7" in preview
            assert "Decisions: 15" in preview
            assert "Tasks completed: 10" in preview
            assert "Tasks overdue: 3" in preview


# =============================================================================
# Test: process_response dispatches to correct distributor
# =============================================================================

class TestProcessResponseRouting:
    """Tests for process_response() — dispatch based on content type from Supabase."""

    @pytest.mark.asyncio
    async def test_approve_meeting_prep_dispatches_to_distribute_approved_prep(self):
        """Approving a meeting_prep should call distribute_approved_prep."""
        prep_content = {
            "title": "Investor Call",
            "summary": "Prep notes for investor call.",
            "sensitivity": "founders",
            "meeting_type": "generic",
            "start_time": "2026-02-28 09:00",
        }
        pending_row = {
            "approval_id": "prep-approve-1",
            "content_type": "meeting_prep",
            "content": prep_content,
            "status": "pending",
        }

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.update_approval_status", new_callable=AsyncMock),
            patch("guardrails.approval_flow.cancel_auto_publish"),
            patch(
                "guardrails.approval_flow.distribute_approved_prep",
                new_callable=AsyncMock,
            ) as mock_distribute_prep,
        ):
            mock_db.get_pending_approval = MagicMock(return_value=pending_row)
            mock_db.delete_pending_approval = MagicMock(return_value=True)
            mock_distribute_prep.return_value = {"telegram_sent": True, "type": "meeting_prep"}

            from guardrails.approval_flow import process_response

            result = await process_response(
                meeting_id="prep-approve-1",
                response="approve",
            )

            assert result["action"] == "approved"
            assert result["next_step"] == "Meeting prep distributed to team"

            # distribute_approved_prep should have been called
            mock_distribute_prep.assert_awaited_once_with(
                meeting_id="prep-approve-1",
                content=prep_content,
            )

            # Entry should be deleted from Supabase
            mock_db.delete_pending_approval.assert_called_with("prep-approve-1")

    @pytest.mark.asyncio
    async def test_approve_weekly_digest_dispatches_to_distribute_approved_digest(self):
        """Approving a weekly_digest should call distribute_approved_digest."""
        digest_content = {
            "title": "Weekly Digest",
            "week_of": "2026-02-17",
            "digest_document": "Full digest text.",
            "meetings_count": 4,
            "decisions_count": 8,
            "tasks_completed": 6,
            "tasks_overdue": 1,
        }
        pending_row = {
            "approval_id": "digest-approve-1",
            "content_type": "weekly_digest",
            "content": digest_content,
            "status": "pending",
        }

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.update_approval_status", new_callable=AsyncMock),
            patch("guardrails.approval_flow.cancel_auto_publish"),
            patch(
                "guardrails.approval_flow.distribute_approved_digest",
                new_callable=AsyncMock,
            ) as mock_distribute_digest,
        ):
            mock_db.get_pending_approval = MagicMock(return_value=pending_row)
            mock_db.delete_pending_approval = MagicMock(return_value=True)
            mock_distribute_digest.return_value = {
                "email_sent": True,
                "telegram_sent": True,
                "type": "weekly_digest",
            }

            from guardrails.approval_flow import process_response

            result = await process_response(
                meeting_id="digest-approve-1",
                response="approve",
            )

            assert result["action"] == "approved"
            assert result["next_step"] == "Weekly digest distributed to team"

            mock_distribute_digest.assert_awaited_once_with(
                meeting_id="digest-approve-1",
                content=digest_content,
            )

            mock_db.delete_pending_approval.assert_called_with("digest-approve-1")

    @pytest.mark.asyncio
    async def test_approve_meeting_summary_dispatches_to_distribute_approved_content(self):
        """Approving a meeting_summary should call distribute_approved_content."""
        summary_content = {
            "title": "Sprint Retro",
            "summary": "Retrospective notes.",
        }
        pending_row = {
            "approval_id": "summary-approve-1",
            "content_type": "meeting_summary",
            "content": summary_content,
            "status": "pending",
        }

        mock_meeting = {
            "id": "summary-approve-1",
            "title": "Sprint Retro",
            "summary": "Retrospective notes.",
            "date": "2026-02-25",
            "sensitivity": "founders",
            "approval_status": "pending",
        }

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.update_approval_status", new_callable=AsyncMock),
            patch("guardrails.approval_flow.cancel_auto_publish"),
            patch(
                "guardrails.approval_flow.distribute_approved_content",
                new_callable=AsyncMock,
            ) as mock_distribute_content,
        ):
            mock_db.get_pending_approval = MagicMock(return_value=pending_row)
            mock_db.delete_pending_approval = MagicMock(return_value=True)
            mock_db.get_meeting = MagicMock(return_value=mock_meeting)
            mock_db.list_decisions = MagicMock(return_value=[])
            mock_db.get_tasks = MagicMock(return_value=[])
            mock_db.list_follow_up_meetings = MagicMock(return_value=[])
            mock_db.get_open_questions = MagicMock(return_value=[])
            mock_distribute_content.return_value = {"telegram_sent": True, "email_sent": True}

            from guardrails.approval_flow import process_response

            result = await process_response(
                meeting_id="summary-approve-1",
                response="approve",
            )

            assert result["action"] == "approved"
            assert result["next_step"] == "Content distributed to team"

            # distribute_approved_content should have been called (not prep or digest)
            mock_distribute_content.assert_awaited_once()
            call_kwargs = mock_distribute_content.call_args.kwargs
            assert call_kwargs["meeting_id"] == "summary-approve-1"
            assert call_kwargs["sensitivity"] == "founders"

    @pytest.mark.asyncio
    async def test_approve_with_no_pending_entry_defaults_to_meeting_summary(self):
        """If Supabase has no pending row, should default to meeting_summary flow."""
        mock_meeting = {
            "id": "orphan-001",
            "title": "Orphan Meeting",
            "summary": "Some notes.",
            "date": "2026-02-24",
            "sensitivity": "founders",
            "approval_status": "pending",
        }

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.update_approval_status", new_callable=AsyncMock),
            patch("guardrails.approval_flow.cancel_auto_publish"),
            patch(
                "guardrails.approval_flow.distribute_approved_content",
                new_callable=AsyncMock,
            ) as mock_distribute_content,
        ):
            mock_db.get_pending_approval = MagicMock(return_value=None)
            mock_db.delete_pending_approval = MagicMock(return_value=False)
            mock_db.get_meeting = MagicMock(return_value=mock_meeting)
            mock_db.list_decisions = MagicMock(return_value=[])
            mock_db.get_tasks = MagicMock(return_value=[])
            mock_db.list_follow_up_meetings = MagicMock(return_value=[])
            mock_db.get_open_questions = MagicMock(return_value=[])
            mock_distribute_content.return_value = {}

            from guardrails.approval_flow import process_response

            result = await process_response(
                meeting_id="orphan-001",
                response="approve",
            )

            assert result["action"] == "approved"
            assert result["next_step"] == "Content distributed to team"
            mock_distribute_content.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reject_does_not_dispatch_any_distributor(self):
        """Rejecting content should not call any distribute function."""
        pending_row = {
            "approval_id": "reject-001",
            "content_type": "meeting_prep",
            "content": {"title": "Some prep"},
            "status": "pending",
        }

        mock_meeting = {
            "id": "reject-001",
            "title": "Some Meeting",
            "approval_status": "pending",
        }

        with (
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.update_approval_status", new_callable=AsyncMock),
            patch("guardrails.approval_flow.cancel_auto_publish"),
            patch(
                "guardrails.approval_flow.distribute_approved_prep",
                new_callable=AsyncMock,
            ) as mock_prep,
            patch(
                "guardrails.approval_flow.distribute_approved_digest",
                new_callable=AsyncMock,
            ) as mock_digest,
            patch(
                "guardrails.approval_flow.distribute_approved_content",
                new_callable=AsyncMock,
            ) as mock_content,
        ):
            mock_db.get_pending_approval = MagicMock(return_value=pending_row)
            mock_db.delete_pending_approval = MagicMock(return_value=True)
            mock_db.get_meeting = MagicMock(return_value=mock_meeting)
            mock_db.log_action = MagicMock()

            from guardrails.approval_flow import process_response

            result = await process_response(
                meeting_id="reject-001",
                response="reject",
            )

            assert result["action"] == "rejected"

            # None of the distributors should have been called
            mock_prep.assert_not_awaited()
            mock_digest.assert_not_awaited()
            mock_content.assert_not_awaited()


# =============================================================================
# Test: distribute_approved_prep
# =============================================================================

class TestDistributeApprovedPrep:
    """Tests for distribute_approved_prep() — Telegram routing and logging."""

    @pytest.mark.asyncio
    async def test_normal_meeting_sends_to_eyal_and_group(self):
        """Normal sensitivity should send prep to both Eyal and group chat."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.settings") as mock_settings,
            patch("services.google_drive.drive_service") as mock_drive,
        ):
            mock_settings.ENVIRONMENT = "production"
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_drive.save_meeting_prep = AsyncMock(return_value={"webViewLink": "https://drive/prep"})

            from guardrails.approval_flow import distribute_approved_prep

            content = {
                "title": "Sprint Planning",
                "sensitivity": "founders",
                "summary": "Prep doc content here.",
                "start_time": "2026-02-27 09:00",
            }

            result = await distribute_approved_prep(
                meeting_id="prep-dist-1",
                content=content,
            )

            assert result["telegram_sent"] is True
            assert result["type"] == "meeting_prep"
            assert result["drive_uploaded"] is True

            # Should send to both Eyal and group in production
            mock_tg.send_to_eyal.assert_awaited_once()
            mock_tg.send_to_group.assert_awaited_once()

            # Verify message content
            eyal_msg = mock_tg.send_to_eyal.call_args[0][0]
            assert "Sprint Planning" in eyal_msg
            assert "Meeting Prep Ready" in eyal_msg

    @pytest.mark.asyncio
    async def test_sensitive_meeting_sends_only_to_eyal(self):
        """Sensitive meetings should only send prep to Eyal, not group."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("services.google_drive.drive_service") as mock_drive,
        ):
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_drive.save_meeting_prep = AsyncMock(return_value={"webViewLink": "https://drive/prep"})

            from guardrails.approval_flow import distribute_approved_prep

            content = {
                "title": "Board Compensation",
                "sensitivity": "ceo",
                "summary": "Sensitive prep content.",
                "start_time": "2026-03-01 14:00",
            }

            result = await distribute_approved_prep(
                meeting_id="prep-dist-2",
                content=content,
            )

            assert result["telegram_sent"] is True

            # Should send to Eyal only
            mock_tg.send_to_eyal.assert_awaited_once()
            # Should NOT send to group
            mock_tg.send_to_group.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_logs_action_with_correct_details(self):
        """Should log the distribution action to audit log with correct details."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("services.google_drive.drive_service") as mock_drive,
        ):
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_drive.save_meeting_prep = AsyncMock(return_value={})

            from guardrails.approval_flow import distribute_approved_prep

            content = {
                "title": "Team Sync",
                "sensitivity": "founders",
                "summary": "Sync prep content.",
                "start_time": "2026-02-28 11:00",
            }

            await distribute_approved_prep(
                meeting_id="prep-dist-3",
                content=content,
            )

            # Verify audit log was called
            mock_db.log_action.assert_called_once()
            log_kwargs = mock_db.log_action.call_args.kwargs
            assert log_kwargs["action"] == "meeting_prep_distributed"
            assert log_kwargs["details"]["meeting_id"] == "prep-dist-3"
            assert log_kwargs["details"]["title"] == "Team Sync"
            assert log_kwargs["details"]["sensitivity"] == "founders"
            assert log_kwargs["triggered_by"] == "eyal"

    @pytest.mark.asyncio
    async def test_telegram_error_sets_sent_false(self):
        """If Telegram raises an exception, telegram_sent should remain False."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("services.google_drive.drive_service") as mock_drive,
        ):
            mock_tg.send_to_eyal = AsyncMock(
                side_effect=Exception("Telegram API error")
            )
            mock_db.log_action = MagicMock()
            mock_drive.save_meeting_prep = AsyncMock(return_value={})

            from guardrails.approval_flow import distribute_approved_prep

            content = {
                "title": "Broken Meeting",
                "sensitivity": "founders",
                "summary": "Content here.",
                "start_time": "2026-02-28 12:00",
            }

            result = await distribute_approved_prep(
                meeting_id="prep-dist-4",
                content=content,
            )

            # telegram_sent should be False due to exception
            assert result["telegram_sent"] is False

            # Audit log should still be called
            mock_db.log_action.assert_called_once()


# =============================================================================
# Test: distribute_approved_digest
# =============================================================================

class TestDistributeApprovedDigest:
    """Tests for distribute_approved_digest() — email + Telegram group posting."""

    @pytest.mark.asyncio
    async def test_sends_email_to_team_and_telegram_to_group(self):
        """Should send email to all team members and post summary to Telegram group."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.settings") as mock_settings,
        ):
            mock_gmail.send_weekly_digest = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_settings.ENVIRONMENT = "production"
            mock_settings.team_emails = [
                "eyal@cropsight.ai",
                "noga@cropsight.ai",
            ]

            from guardrails.approval_flow import distribute_approved_digest

            content = {
                "week_of": "2026-02-17",
                "digest_document": "Full weekly digest content here.",
                "drive_link": "https://drive.google.com/file/digest-001",
                "meetings_count": 6,
                "decisions_count": 10,
                "tasks_completed": 7,
                "tasks_overdue": 2,
            }

            result = await distribute_approved_digest(
                meeting_id="digest-dist-1",
                content=content,
            )

            assert result["email_sent"] is True
            assert result["telegram_sent"] is True
            assert result["type"] == "weekly_digest"
            assert result["emails_to"] == ["eyal@cropsight.ai", "noga@cropsight.ai"]

            # Verify email was sent with correct params
            mock_gmail.send_weekly_digest.assert_awaited_once()
            email_kwargs = mock_gmail.send_weekly_digest.call_args.kwargs
            assert email_kwargs["week_of"] == "2026-02-17"
            assert email_kwargs["digest_content"] == "Full weekly digest content here."
            assert email_kwargs["drive_link"] == "https://drive.google.com/file/digest-001"
            assert set(email_kwargs["recipients"]) == {
                "eyal@cropsight.ai",
                "noga@cropsight.ai",
            }

            # Verify Telegram group message includes stats
            mock_tg.send_to_group.assert_awaited_once()
            tg_msg = mock_tg.send_to_group.call_args[0][0]
            assert "CropSight Weekly Digest" in tg_msg
            assert "Meetings: 6" in tg_msg
            assert "Decisions: 10" in tg_msg
            assert "Tasks completed: 7" in tg_msg
            assert "Tasks overdue: 2" in tg_msg
            assert "https://drive.google.com/file/digest-001" in tg_msg

    @pytest.mark.asyncio
    async def test_skips_email_when_no_team_emails(self):
        """Should not send email when team_emails is empty."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.settings") as mock_settings,
        ):
            mock_gmail.send_weekly_digest = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_settings.ENVIRONMENT = "development"
            mock_settings.EYAL_EMAIL = ""  # No email configured
            mock_settings.team_emails = []  # No team emails

            from guardrails.approval_flow import distribute_approved_digest

            content = {
                "week_of": "2026-02-17",
                "digest_document": "Digest text.",
                "drive_link": "",
                "meetings_count": 3,
                "decisions_count": 5,
                "tasks_completed": 4,
                "tasks_overdue": 0,
            }

            result = await distribute_approved_digest(
                meeting_id="digest-dist-2",
                content=content,
            )

            # Email should not have been sent
            assert result["email_sent"] is False
            mock_gmail.send_weekly_digest.assert_not_awaited()

            # Telegram should still work
            assert result["telegram_sent"] is True

    @pytest.mark.asyncio
    async def test_logs_action_with_week_and_stats(self):
        """Should log the distribution action with week_of and meetings_count."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.settings") as mock_settings,
        ):
            mock_gmail.send_weekly_digest = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_settings.team_emails = ["eyal@cropsight.ai"]

            from guardrails.approval_flow import distribute_approved_digest

            content = {
                "week_of": "2026-02-10",
                "digest_document": "Another digest.",
                "drive_link": "",
                "meetings_count": 2,
                "decisions_count": 3,
                "tasks_completed": 1,
                "tasks_overdue": 0,
            }

            await distribute_approved_digest(
                meeting_id="digest-dist-3",
                content=content,
            )

            mock_db.log_action.assert_called_once()
            log_kwargs = mock_db.log_action.call_args.kwargs
            assert log_kwargs["action"] == "weekly_digest_distributed"
            assert log_kwargs["details"]["week_of"] == "2026-02-10"
            assert log_kwargs["details"]["meetings_count"] == 2
            assert log_kwargs["triggered_by"] == "eyal"

    @pytest.mark.asyncio
    async def test_email_failure_does_not_block_telegram(self):
        """If email sending fails, Telegram should still be attempted."""
        with (
            patch("guardrails.approval_flow.telegram_bot") as mock_tg,
            patch("guardrails.approval_flow.gmail_service") as mock_gmail,
            patch("guardrails.approval_flow.supabase_client") as mock_db,
            patch("guardrails.approval_flow.settings") as mock_settings,
        ):
            mock_gmail.send_weekly_digest = AsyncMock(
                side_effect=Exception("SMTP error")
            )
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_db.log_action = MagicMock()
            mock_settings.ENVIRONMENT = "production"
            mock_settings.team_emails = ["eyal@cropsight.ai"]

            from guardrails.approval_flow import distribute_approved_digest

            content = {
                "week_of": "2026-02-17",
                "digest_document": "Digest with email failure.",
                "drive_link": "",
                "meetings_count": 1,
                "decisions_count": 1,
                "tasks_completed": 0,
                "tasks_overdue": 0,
            }

            result = await distribute_approved_digest(
                meeting_id="digest-dist-4",
                content=content,
            )

            # Email failed
            assert result["email_sent"] is False
            # Telegram should still succeed
            assert result["telegram_sent"] is True
            mock_tg.send_to_group.assert_awaited_once()

            # Audit log should still be called
            mock_db.log_action.assert_called_once()
