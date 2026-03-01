"""
Tests for v0.5 remaining features: E3 (inline Drive links),
F2 (orphan cleanup scheduler), F3 (structured logging).

~18 tests covering all three features.
"""

import json
import logging
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock


# =========================================================================
# E3: Inline Drive Links in Agent Responses
# =========================================================================

class TestInlineDriveLinks:
    """Tests for Drive/Sheets links injected into agent tool results."""

    @pytest.mark.asyncio
    async def test_get_meeting_summary_includes_folder_link(self):
        """_tool_get_meeting_summary should include summaries_folder link."""
        with patch("core.agent.supabase_client") as mock_db, \
             patch("core.agent.settings") as mock_settings:
            mock_settings.MEETING_SUMMARIES_FOLDER_ID = "folder123"
            mock_settings.model_agent = "claude-haiku-4-5-20251001"
            mock_settings.ANTHROPIC_API_KEY = "test"
            mock_db.get_meeting.return_value = {
                "title": "Test Meeting",
                "date": "2026-03-01",
                "summary": "A summary",
                "participants": ["Eyal"],
            }

            from core.agent import GianluigiAgent
            with patch.object(GianluigiAgent, "__init__", lambda self: None):
                agent = GianluigiAgent()
                result = await agent._tool_get_meeting_summary({"meeting_id": "abc"})

            assert "summaries_folder" in result
            assert "folder123" in result["summaries_folder"]
            assert "drive.google.com" in result["summaries_folder"]

    @pytest.mark.asyncio
    async def test_get_meeting_summary_no_link_when_unset(self):
        """No link added when MEETING_SUMMARIES_FOLDER_ID is empty."""
        with patch("core.agent.supabase_client") as mock_db, \
             patch("core.agent.settings") as mock_settings:
            mock_settings.MEETING_SUMMARIES_FOLDER_ID = ""
            mock_db.get_meeting.return_value = {
                "title": "Test", "date": "2026-03-01",
                "summary": "S", "participants": [],
            }

            from core.agent import GianluigiAgent
            with patch.object(GianluigiAgent, "__init__", lambda self: None):
                agent = GianluigiAgent()
                result = await agent._tool_get_meeting_summary({"meeting_id": "abc"})

            assert "summaries_folder" not in result

    @pytest.mark.asyncio
    async def test_get_tasks_includes_sheet_link(self):
        """_tool_get_tasks should include task_tracker link."""
        with patch("core.agent.supabase_client") as mock_db, \
             patch("core.agent.settings") as mock_settings, \
             patch("core.agent.get_team_member", return_value=None):
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet456"
            mock_db.get_tasks.return_value = [
                {"title": "Task 1", "assignee": "Eyal", "status": "pending"},
            ]

            from core.agent import GianluigiAgent
            with patch.object(GianluigiAgent, "__init__", lambda self: None):
                agent = GianluigiAgent()
                result = await agent._tool_get_tasks({"status": "pending"})

            assert "task_tracker" in result
            assert "sheet456" in result["task_tracker"]
            assert "docs.google.com/spreadsheets" in result["task_tracker"]

    @pytest.mark.asyncio
    async def test_get_commitments_includes_sheet_link(self):
        """_tool_get_commitments should include task_tracker link."""
        with patch("core.agent.supabase_client") as mock_db, \
             patch("core.agent.settings") as mock_settings, \
             patch("core.agent.get_team_member", return_value=None):
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet789"
            mock_db.get_commitments.return_value = []

            from core.agent import GianluigiAgent
            with patch.object(GianluigiAgent, "__init__", lambda self: None):
                agent = GianluigiAgent()
                result = await agent._tool_get_commitments({})

            assert "task_tracker" in result
            assert "sheet789" in result["task_tracker"]

    def test_system_prompt_contains_link_guidance(self):
        """System prompt should instruct the agent to include links."""
        from core.system_prompt import get_system_prompt
        prompt = get_system_prompt()
        assert "summaries_folder" in prompt
        assert "task_tracker" in prompt
        assert "documents_folder" in prompt


# =========================================================================
# F2: Orphan Cleanup Scheduler
# =========================================================================

class TestOrphanCleanupStaleApprovals:
    """Tests for stale approval detection."""

    def test_stale_approvals_detected(self):
        """Stale approvals should produce notifications."""
        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db:
            mock_db.get_stale_pending_approvals.return_value = [
                {"content_type": "meeting_summary", "approval_id": "abc-123"},
            ]

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            result = scheduler._check_stale_approvals()

            assert len(result) == 1
            assert result[0]["type"] == "stale_approval"
            assert "abc-123" in result[0]["message"]

    def test_no_stale_approvals(self):
        """No stale approvals should produce empty list."""
        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db:
            mock_db.get_stale_pending_approvals.return_value = []

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            result = scheduler._check_stale_approvals()

            assert result == []


class TestOrphanCleanupEmbeddings:
    """Tests for orphan embedding cleanup."""

    def test_orphan_embeddings_detected_and_deleted(self):
        """Orphan embeddings should be found and deleted."""
        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db:
            mock_db.get_orphan_embedding_ids.return_value = ["emb-1", "emb-2"]
            mock_db.delete_embeddings_by_ids.return_value = 2

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            count = scheduler._cleanup_orphan_embeddings()

            assert count == 2
            mock_db.delete_embeddings_by_ids.assert_called_once_with(["emb-1", "emb-2"])

    def test_no_orphan_embeddings(self):
        """No orphans should result in zero deletions."""
        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db:
            mock_db.get_orphan_embedding_ids.return_value = []

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            count = scheduler._cleanup_orphan_embeddings()

            assert count == 0
            mock_db.delete_embeddings_by_ids.assert_not_called()


class TestOrphanCleanupStaleTasks:
    """Tests for stale manual task detection."""

    def test_stale_tasks_detected(self):
        """Old manual tasks should produce notifications."""
        mock_result = MagicMock()
        mock_result.data = [
            {"id": "t1", "title": "Old task", "assignee": "Eyal",
             "created_at": "2026-01-01T00:00:00"},
        ]

        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db:
            mock_db.client.table.return_value.select.return_value \
                .eq.return_value.is_.return_value.lt.return_value \
                .execute.return_value = mock_result

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            result = scheduler._check_stale_tasks()

            assert len(result) == 1
            assert result[0]["type"] == "stale_task"
            assert "Old task" in result[0]["message"]

    def test_no_stale_tasks(self):
        """No stale tasks should produce empty list."""
        mock_result = MagicMock()
        mock_result.data = []

        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db:
            mock_db.client.table.return_value.select.return_value \
                .eq.return_value.is_.return_value.lt.return_value \
                .execute.return_value = mock_result

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            result = scheduler._check_stale_tasks()

            assert result == []


class TestOrphanCleanupFailedAutoPublish:
    """Tests for failed auto-publish detection."""

    def test_failed_auto_publishes_detected(self):
        """Past-due auto-publishes should produce notifications."""
        mock_result = MagicMock()
        mock_result.data = [
            {"content_type": "weekly_digest", "approval_id": "digest-1",
             "auto_publish_at": "2026-02-28T12:00:00", "status": "pending"},
        ]

        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db:
            mock_db.client.table.return_value.select.return_value \
                .eq.return_value.not_.is_.return_value.lt.return_value \
                .execute.return_value = mock_result

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            result = scheduler._check_failed_auto_publishes()

            assert len(result) == 1
            assert result[0]["type"] == "failed_auto_publish"
            assert "digest-1" in result[0]["message"]


class TestOrphanCleanupFullRun:
    """Tests for full cleanup run and scheduler lifecycle."""

    @pytest.mark.asyncio
    async def test_full_cleanup_empty_db(self):
        """Full cleanup on empty DB should produce no errors."""
        mock_result = MagicMock()
        mock_result.data = []

        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db, \
             patch("schedulers.orphan_cleanup_scheduler.telegram_bot") as mock_tg:
            mock_db.get_stale_pending_approvals.return_value = []
            mock_db.get_orphan_embedding_ids.return_value = []
            mock_db.client.table.return_value.select.return_value \
                .eq.return_value.is_.return_value.lt.return_value \
                .execute.return_value = mock_result
            mock_db.client.table.return_value.select.return_value \
                .eq.return_value.not_.is_.return_value.lt.return_value \
                .execute.return_value = mock_result

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            results = await scheduler._run_cleanup()

            assert results["stale_approvals"] == 0
            assert results["orphan_embeddings_deleted"] == 0
            assert results["stale_tasks"] == 0
            assert results["failed_auto_publishes"] == 0
            # No notifications sent
            mock_tg.send_to_eyal.assert_not_called()

    def test_scheduler_start_stop_lifecycle(self):
        """Scheduler should track running state."""
        from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
        scheduler = OrphanCleanupScheduler(check_interval=60)

        assert scheduler._running is False
        scheduler._running = True
        assert scheduler._running is True
        scheduler.stop()
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_cleanup_logs_to_audit_trail(self):
        """Cleanup should log results to the audit trail."""
        mock_result = MagicMock()
        mock_result.data = []

        with patch("schedulers.orphan_cleanup_scheduler.supabase_client") as mock_db, \
             patch("schedulers.orphan_cleanup_scheduler.telegram_bot"):
            mock_db.get_stale_pending_approvals.return_value = []
            mock_db.get_orphan_embedding_ids.return_value = []
            mock_db.client.table.return_value.select.return_value \
                .eq.return_value.is_.return_value.lt.return_value \
                .execute.return_value = mock_result
            mock_db.client.table.return_value.select.return_value \
                .eq.return_value.not_.is_.return_value.lt.return_value \
                .execute.return_value = mock_result

            from schedulers.orphan_cleanup_scheduler import OrphanCleanupScheduler
            scheduler = OrphanCleanupScheduler()
            await scheduler._run_cleanup()

            # Verify audit log was called
            mock_db.log_action.assert_called_once()
            call_args = mock_db.log_action.call_args
            assert call_args[1]["action"] == "orphan_cleanup_completed"


# =========================================================================
# F3: Structured Logging
# =========================================================================

class TestStructuredLogging:
    """Tests for the structured logging formatter."""

    def test_json_format_in_production(self):
        """Production mode should output valid JSON."""
        from core.logging_config import StructuredFormatter

        formatter = StructuredFormatter(environment="production")
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO, pathname="",
            lineno=0, msg="Test message", args=(), exc_info=None,
        )
        output = formatter.format(record)

        # Should be valid JSON
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "Test message"

    def test_human_readable_in_dev(self):
        """Dev mode should output plain text, not JSON."""
        from core.logging_config import StructuredFormatter

        formatter = StructuredFormatter(environment="development")
        record = logging.LogRecord(
            name="test.logger", level=logging.WARNING, pathname="",
            lineno=0, msg="Dev warning", args=(), exc_info=None,
        )
        output = formatter.format(record)

        # Should NOT be JSON
        assert "Dev warning" in output
        assert "WARNING" in output
        assert "test.logger" in output
        # Verify it's NOT JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(output)

    def test_json_has_required_fields(self):
        """JSON output must include timestamp, level, logger, message."""
        from core.logging_config import StructuredFormatter

        formatter = StructuredFormatter(environment="production")
        record = logging.LogRecord(
            name="gianluigi", level=logging.ERROR, pathname="",
            lineno=0, msg="Something broke", args=(), exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "timestamp" in parsed
        assert "level" in parsed
        assert "logger" in parsed
        assert "message" in parsed
        assert parsed["level"] == "ERROR"
        assert parsed["logger"] == "gianluigi"

    def test_setup_logging_configures_handler(self):
        """setup_logging should configure the root logger with our formatter."""
        from core.logging_config import setup_logging, StructuredFormatter

        # Use a unique logger to avoid side effects
        setup_logging(level="DEBUG", environment="production")
        root = logging.getLogger()

        assert len(root.handlers) >= 1
        # At least one handler should use StructuredFormatter
        has_structured = any(
            isinstance(h.formatter, StructuredFormatter)
            for h in root.handlers
        )
        assert has_structured

        # Clean up: restore default
        setup_logging(level="INFO", environment="development")
