"""Tests for Sub-Phase 6.0: Weekly review models, settings, and Supabase CRUD."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


# =========================================================================
# Model & Enum Tests
# =========================================================================

class TestWeeklyReviewStatus:
    """Test WeeklyReviewStatus enum values."""

    def test_all_statuses_exist(self):
        from models.schemas import WeeklyReviewStatus
        assert WeeklyReviewStatus.PREPARING == "preparing"
        assert WeeklyReviewStatus.READY == "ready"
        assert WeeklyReviewStatus.IN_PROGRESS == "in_progress"
        assert WeeklyReviewStatus.CONFIRMING == "confirming"
        assert WeeklyReviewStatus.APPROVED == "approved"
        assert WeeklyReviewStatus.CANCELLED == "cancelled"

    def test_status_count(self):
        from models.schemas import WeeklyReviewStatus
        assert len(WeeklyReviewStatus) == 6


class TestWeeklyReviewSession:
    """Test WeeklyReviewSession model."""

    def test_minimal_creation(self):
        from models.schemas import WeeklyReviewSession
        session = WeeklyReviewSession(week_number=12, year=2026)
        assert session.week_number == 12
        assert session.year == 2026
        assert session.status.value == "preparing"
        assert session.current_part == 0
        assert session.agenda_data == {}
        assert session.gantt_proposals == []
        assert session.corrections == []
        assert session.trigger_type == "calendar"

    def test_full_creation(self):
        from models.schemas import WeeklyReviewSession, WeeklyReviewStatus
        session = WeeklyReviewSession(
            week_number=12,
            year=2026,
            status=WeeklyReviewStatus.IN_PROGRESS,
            current_part=2,
            agenda_data={"week_in_review": {}},
            calendar_event_id="cal_123",
            trigger_type="manual",
        )
        assert session.status == WeeklyReviewStatus.IN_PROGRESS
        assert session.current_part == 2
        assert session.calendar_event_id == "cal_123"

    def test_defaults(self):
        from models.schemas import WeeklyReviewSession
        session = WeeklyReviewSession(week_number=1, year=2026)
        assert session.workspace_id == "cropsight"
        assert session.id is None
        assert session.report_id is None
        assert session.raw_messages == []


class TestWeeklyReportExtended:
    """Test WeeklyReport model with new Phase 6 fields."""

    def test_new_fields_default(self):
        from models.schemas import WeeklyReport
        report = WeeklyReport(week_number=12, year=2026)
        assert report.html_content is None
        assert report.access_token is None
        assert report.session_id is None
        assert report.status == "draft"
        assert report.distributed_at is None

    def test_new_fields_set(self):
        from models.schemas import WeeklyReport
        report = WeeklyReport(
            week_number=12,
            year=2026,
            html_content="<html>test</html>",
            access_token="abc123",
            status="distributed",
        )
        assert report.html_content == "<html>test</html>"
        assert report.access_token == "abc123"
        assert report.status == "distributed"


# =========================================================================
# Settings Tests
# =========================================================================

class TestWeeklyReviewSettings:
    """Test weekly review settings defaults."""

    def test_defaults(self):
        from config.settings import Settings
        s = Settings(
            ANTHROPIC_API_KEY="test",
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_KEY="test",
            TELEGRAM_BOT_TOKEN="test",
        )
        assert s.WEEKLY_REVIEW_PREP_HOURS == 3
        assert s.WEEKLY_REVIEW_NOTIFY_MINUTES == 30
        assert s.WEEKLY_REVIEW_MAX_CORRECTIONS == 10
        assert s.WEEKLY_REVIEW_ENABLED is False
        assert s.WEEKLY_REVIEW_SCHEDULER_INTERVAL == 900


# =========================================================================
# Supabase CRUD Tests
# =========================================================================

@pytest.fixture
def mock_supabase():
    """Create a mock Supabase client for testing CRUD operations."""
    from services.supabase_client import SupabaseClient
    client = SupabaseClient()
    client._client = MagicMock()
    return client


class TestWeeklyReviewSessionCRUD:
    """Test Supabase CRUD methods for weekly review sessions."""

    def test_create_weekly_review_session(self, mock_supabase):
        mock_supabase._client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "uuid-1", "week_number": 12, "year": 2026, "status": "preparing"}]
        )
        result = mock_supabase.create_weekly_review_session(12, 2026)
        assert result["id"] == "uuid-1"
        assert result["week_number"] == 12
        mock_supabase._client.table.assert_called_with("weekly_review_sessions")

    def test_create_weekly_review_session_with_kwargs(self, mock_supabase):
        mock_supabase._client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "uuid-1", "trigger_type": "manual"}]
        )
        result = mock_supabase.create_weekly_review_session(
            12, 2026, trigger_type="manual", calendar_event_id="cal_1"
        )
        call_args = mock_supabase._client.table.return_value.insert.call_args[0][0]
        assert call_args["trigger_type"] == "manual"
        assert call_args["calendar_event_id"] == "cal_1"

    def test_get_weekly_review_session(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "uuid-1", "status": "in_progress"}]
        )
        result = mock_supabase.get_weekly_review_session("uuid-1")
        assert result["id"] == "uuid-1"

    def test_get_weekly_review_session_not_found(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[]
        )
        result = mock_supabase.get_weekly_review_session("nonexistent")
        assert result is None

    def test_get_active_weekly_review_session(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.in_.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "uuid-1", "status": "in_progress"}]
        )
        result = mock_supabase.get_active_weekly_review_session()
        assert result["id"] == "uuid-1"

    def test_get_active_weekly_review_session_none(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.in_.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[]
        )
        result = mock_supabase.get_active_weekly_review_session()
        assert result is None

    def test_update_weekly_review_session(self, mock_supabase):
        mock_supabase._client.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "uuid-1", "status": "in_progress"}]
        )
        result = mock_supabase.update_weekly_review_session("uuid-1", status="in_progress")
        assert result["status"] == "in_progress"

    def test_get_weekly_report_by_token(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "report-1", "access_token": "tok123"}]
        )
        result = mock_supabase.get_weekly_report_by_token("tok123")
        assert result["id"] == "report-1"

    def test_get_weekly_report_by_token_not_found(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[]
        )
        result = mock_supabase.get_weekly_report_by_token("invalid")
        assert result is None

    def test_get_stale_tasks(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.eq.return_value.lt.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[{"id": "task-1", "title": "Stale task"}]
        )
        result = mock_supabase.get_stale_tasks(days=14)
        assert len(result) == 1

    def test_get_debrief_sessions_for_week(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.gte.return_value.lte.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"id": "ds-1", "date": "2026-03-16"}]
        )
        result = mock_supabase.get_debrief_sessions_for_week("2026-03-16", "2026-03-20")
        assert len(result) == 1

    def test_get_email_scans_for_week(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.gte.return_value.lte.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"id": "es-1"}]
        )
        result = mock_supabase.get_email_scans_for_week("2026-03-16", "2026-03-20")
        assert len(result) == 1

    def test_get_pending_gantt_proposals(self, mock_supabase):
        mock_supabase._client.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
            data=[{"id": "gp-1", "status": "pending"}]
        )
        result = mock_supabase.get_pending_gantt_proposals()
        assert len(result) == 1


class TestColumnAlignment:
    """Verify Supabase column names match migration SQL."""

    def test_weekly_review_sessions_columns(self):
        """Column names used in code should match migration SQL."""
        migration_columns = {
            "id", "workspace_id", "week_number", "year", "status",
            "current_part", "agenda_data", "gantt_proposals", "corrections",
            "report_id", "calendar_event_id", "trigger_type", "raw_messages",
            "created_at", "updated_at",
        }
        from models.schemas import WeeklyReviewSession
        model_fields = set(WeeklyReviewSession.model_fields.keys())
        assert model_fields == migration_columns

    def test_weekly_reports_new_columns(self):
        """New Phase 6 columns on weekly_reports should exist in model."""
        new_columns = {"html_content", "access_token", "session_id", "status", "distributed_at"}
        from models.schemas import WeeklyReport
        model_fields = set(WeeklyReport.model_fields.keys())
        for col in new_columns:
            assert col in model_fields, f"Column {col} missing from WeeklyReport model"
