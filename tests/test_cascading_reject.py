"""
Tests for the cascading reject flow (T1.1).

Covers:
- _reject_meeting_cascade() helper — cascade delete + Sheets rebuild
- process_response() reject branch — delegation to the helper
- Non-meeting reject path (digests/preps/briefs) — no cascade
- CRITICAL alerts on failure
- Telegram callback delegation via force_action="reject"
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# =============================================================================
# _reject_meeting_cascade helper
# =============================================================================


class TestRejectMeetingCascade:
    @pytest.mark.asyncio
    async def test_cascades_for_real_meeting(self):
        """Meeting reject triggers delete_meeting_cascade and sheets rebuild."""
        from guardrails.approval_flow import _reject_meeting_cascade

        with patch("guardrails.approval_flow.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()):
            mock_sb.delete_meeting_cascade.return_value = {
                "embeddings": 50,
                "tasks": 3,
                "decisions": 4,
                "open_questions": 2,
                "meetings": 1,
                "topic_thread_mentions": 1,
            }
            mock_sb.get_tasks.return_value = [{"id": "t1"}]
            mock_sb.list_decisions.return_value = [{"id": "d1"}]
            mock_sheets.rebuild_tasks_sheet = AsyncMock()
            mock_sheets.rebuild_decisions_sheet = AsyncMock()

            result = await _reject_meeting_cascade("meeting-123", is_non_meeting=False)

            mock_sb.delete_meeting_cascade.assert_called_once_with("meeting-123")
            mock_sheets.rebuild_tasks_sheet.assert_called_once()
            mock_sheets.rebuild_decisions_sheet.assert_called_once()
            assert result["deleted"]["tasks"] == 3
            assert result["deleted"]["decisions"] == 4
            assert result["deleted"]["topic_thread_mentions"] == 1

    @pytest.mark.asyncio
    async def test_skips_cascade_for_non_meeting(self):
        """Non-meeting reject (digest/prep/brief) doesn't call cascade."""
        from guardrails.approval_flow import _reject_meeting_cascade

        with patch("guardrails.approval_flow.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()):
            mock_sb.delete_pending_approval.return_value = None

            result = await _reject_meeting_cascade("digest-2026-02-23", is_non_meeting=True)

            mock_sb.delete_meeting_cascade.assert_not_called()
            mock_sb.delete_pending_approval.assert_called_once_with("digest-2026-02-23")
            mock_sheets.rebuild_tasks_sheet.assert_not_called() if hasattr(
                mock_sheets.rebuild_tasks_sheet, "assert_not_called"
            ) else None
            assert result["deleted"] == {}

    @pytest.mark.asyncio
    async def test_alerts_on_cascade_failure(self):
        """If cascade raises, a CRITICAL alert is sent."""
        from guardrails.approval_flow import _reject_meeting_cascade

        with patch("guardrails.approval_flow.supabase_client") as mock_sb, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            mock_sb.delete_meeting_cascade.side_effect = Exception("DB error")

            result = await _reject_meeting_cascade("meeting-bad", is_non_meeting=False)

            mock_alert.assert_called_once()
            from services.alerting import AlertSeverity
            args, _ = mock_alert.call_args
            assert args[0] == AlertSeverity.CRITICAL
            assert "meeting-bad" in args[2]
            assert result.get("error")

    @pytest.mark.asyncio
    async def test_sheets_rebuild_failure_is_non_fatal(self):
        """Cascade succeeded + Sheets rebuild failed = warning, not failure."""
        from guardrails.approval_flow import _reject_meeting_cascade

        with patch("guardrails.approval_flow.supabase_client") as mock_sb, \
             patch("services.google_sheets.sheets_service") as mock_sheets, \
             patch("services.alerting.send_system_alert", new=AsyncMock()) as mock_alert:
            mock_sb.delete_meeting_cascade.return_value = {"tasks": 2, "meetings": 1}
            mock_sb.get_tasks.return_value = []
            mock_sb.list_decisions.return_value = []
            mock_sheets.rebuild_tasks_sheet = AsyncMock(side_effect=Exception("sheets down"))
            mock_sheets.rebuild_decisions_sheet = AsyncMock()

            result = await _reject_meeting_cascade("meeting-abc", is_non_meeting=False)

            # Cascade succeeded despite sheet rebuild failure
            assert result.get("deleted", {}).get("tasks") == 2
            assert "error" not in result
            # Warning alert should have fired (not CRITICAL)
            mock_alert.assert_called()
            from services.alerting import AlertSeverity
            args, _ = mock_alert.call_args
            assert args[0] == AlertSeverity.WARNING


# =============================================================================
# process_response() reject branch
# =============================================================================


class TestProcessResponseReject:
    @pytest.mark.asyncio
    async def test_reject_meeting_delegates_to_cascade(self):
        """process_response(force_action='reject') calls _reject_meeting_cascade."""
        from guardrails import approval_flow

        with patch("guardrails.approval_flow.supabase_client") as mock_sb, \
             patch("guardrails.approval_flow._reject_meeting_cascade", new=AsyncMock()) as mock_cascade, \
             patch("guardrails.approval_flow.cancel_auto_publish"), \
             patch("guardrails.approval_flow.cancel_approval_reminders"):
            mock_sb.get_pending_approval.return_value = None
            mock_sb.get_meeting.return_value = {"id": "m1", "title": "Test"}
            mock_sb.delete_pending_approval.return_value = None
            mock_cascade.return_value = {
                "deleted": {"tasks": 3, "decisions": 2, "embeddings": 40, "open_questions": 1, "topic_thread_mentions": 0}
            }

            result = await approval_flow.process_response(
                meeting_id="m1",
                response="reject",
                force_action="reject",
            )

            # is_non_meeting is computed via Python `or` chain which returns the
            # last falsy value (None, not False) when all terms are falsy.
            mock_cascade.assert_called_once()
            args = mock_cascade.call_args[0]
            assert args[0] == "m1"
            assert not args[1]  # falsy (None for real meetings)
            assert result["action"] == "rejected"
            assert "Deleted:" in result["next_step"]
            assert "3 tasks" in result["next_step"]
            assert "2 decisions" in result["next_step"]

    @pytest.mark.asyncio
    async def test_reject_non_meeting_skips_cascade(self):
        """Rejecting a digest/prep/brief ID triggers the is_non_meeting path."""
        from guardrails import approval_flow

        with patch("guardrails.approval_flow.supabase_client") as mock_sb, \
             patch("guardrails.approval_flow._reject_meeting_cascade", new=AsyncMock()) as mock_cascade, \
             patch("guardrails.approval_flow.cancel_auto_publish"), \
             patch("guardrails.approval_flow.cancel_approval_reminders"):
            mock_sb.get_pending_approval.return_value = None
            mock_sb.delete_pending_approval.return_value = None
            mock_cascade.return_value = {"deleted": {}}

            result = await approval_flow.process_response(
                meeting_id="digest-2026-02-23",
                response="reject",
                force_action="reject",
            )

            # Cascade helper was still called, but with is_non_meeting=True
            mock_cascade.assert_called_once_with("digest-2026-02-23", True)
            assert result["action"] == "rejected"
            # Non-meeting message
            assert result["next_step"] == "Content discarded"

    @pytest.mark.asyncio
    async def test_reject_surfaces_error_in_next_step(self):
        """If cascade returns an error, next_step surfaces it to the user."""
        from guardrails import approval_flow

        with patch("guardrails.approval_flow.supabase_client") as mock_sb, \
             patch("guardrails.approval_flow._reject_meeting_cascade", new=AsyncMock()) as mock_cascade, \
             patch("guardrails.approval_flow.cancel_auto_publish"), \
             patch("guardrails.approval_flow.cancel_approval_reminders"):
            mock_sb.get_pending_approval.return_value = None
            mock_sb.get_meeting.return_value = {"id": "m1", "title": "Test"}
            mock_sb.delete_pending_approval.return_value = None
            mock_cascade.return_value = {"deleted": {}, "error": "DB timeout"}

            result = await approval_flow.process_response(
                meeting_id="m1",
                response="reject",
                force_action="reject",
            )

            assert result["action"] == "rejected"
            assert "DB timeout" in result["next_step"]
            assert "cleanup_rejected_meetings" in result["next_step"]
