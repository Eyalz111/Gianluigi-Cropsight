"""
Tests for v1.0 Phase 0: new Pydantic models, enums, settings, and CRUD methods.

Tests cover:
- All 4 new enums have expected values
- All 8 new Pydantic models instantiate correctly
- New settings have defaults and app starts without new env vars
- New Supabase CRUD methods work with mocked client
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, date
from uuid import uuid4


# =============================================================================
# Enums
# =============================================================================

class TestV1Enums:
    """Test all new v1.0 enums."""

    def test_gantt_proposal_status_values(self):
        from models.schemas import GanttProposalStatus
        assert GanttProposalStatus.PENDING == "pending"
        assert GanttProposalStatus.APPROVED == "approved"
        assert GanttProposalStatus.REJECTED == "rejected"
        assert GanttProposalStatus.ROLLED_BACK == "rolled_back"

    def test_debrief_status_values(self):
        from models.schemas import DebriefStatus
        assert DebriefStatus.IN_PROGRESS == "in_progress"
        assert DebriefStatus.CONFIRMING == "confirming"
        assert DebriefStatus.APPROVED == "approved"
        assert DebriefStatus.CANCELLED == "cancelled"

    def test_email_classification_values(self):
        from models.schemas import EmailClassification
        assert EmailClassification.RELEVANT == "relevant"
        assert EmailClassification.BORDERLINE == "borderline"
        assert EmailClassification.FALSE_POSITIVE == "false_positive"
        assert EmailClassification.SKIPPED == "skipped"

    def test_intent_type_values(self):
        from models.schemas import IntentType
        expected = {
            "question", "task_update", "information_injection",
            "gantt_request", "debrief", "approval_response",
            "weekly_review", "meeting_prep_request", "ambiguous",
        }
        actual = {e.value for e in IntentType}
        assert actual == expected


# =============================================================================
# Pydantic Models
# =============================================================================

class TestGanttSchemaRow:
    def test_minimal_instantiation(self):
        from models.schemas import GanttSchemaRow
        row = GanttSchemaRow(sheet_name="Main", section="Product", row_number=5)
        assert row.sheet_name == "Main"
        assert row.workspace_id == "cropsight"
        assert row.owner_column == "C"
        assert row.protected is False

    def test_full_instantiation(self):
        from models.schemas import GanttSchemaRow
        row = GanttSchemaRow(
            sheet_name="Main", section="Product", subsection="MVP",
            row_number=10, owner_column="D", protected=True, notes="Test"
        )
        assert row.subsection == "MVP"
        assert row.protected is True


class TestGanttProposal:
    def test_default_status(self):
        from models.schemas import GanttProposal, GanttProposalStatus
        proposal = GanttProposal(changes=[{"row": 5, "column": "E", "value": "done"}])
        assert proposal.status == GanttProposalStatus.PENDING
        assert proposal.workspace_id == "cropsight"

    def test_with_source(self):
        from models.schemas import GanttProposal
        uid = uuid4()
        proposal = GanttProposal(
            source_type="meeting", source_id=uid,
            changes=[{"row": 5}]
        )
        assert proposal.source_type == "meeting"
        assert proposal.source_id == uid


class TestGanttSnapshot:
    def test_instantiation(self):
        from models.schemas import GanttSnapshot
        uid = uuid4()
        snap = GanttSnapshot(
            proposal_id=uid, sheet_name="Main",
            cell_references=["B5", "C5"],
            old_values={"B5": "", "C5": ""},
            new_values={"B5": "done", "C5": "Eyal"},
        )
        assert snap.proposal_id == uid
        assert len(snap.cell_references) == 2


class TestDebriefSession:
    def test_minimal_instantiation(self):
        from models.schemas import DebriefSession, DebriefStatus
        session = DebriefSession(date=date(2026, 3, 14))
        assert session.status == DebriefStatus.IN_PROGRESS
        assert session.items_captured == []
        assert session.raw_messages == []

    def test_with_items(self):
        from models.schemas import DebriefSession
        session = DebriefSession(
            date=date(2026, 3, 14),
            items_captured=[{"type": "task", "text": "Follow up with investor"}],
            calendar_events_covered=["event-1"],
        )
        assert len(session.items_captured) == 1


class TestEmailScan:
    def test_minimal_instantiation(self):
        from models.schemas import EmailScan
        scan = EmailScan(
            scan_type="constant",
            email_id="msg-123",
            date=datetime(2026, 3, 14, 10, 0),
        )
        assert scan.approved is False
        assert scan.workspace_id == "cropsight"

    def test_with_classification(self):
        from models.schemas import EmailScan, EmailClassification
        scan = EmailScan(
            scan_type="daily",
            email_id="msg-456",
            date=datetime(2026, 3, 14),
            sender="investor@example.com",
            subject="Follow up",
            classification=EmailClassification.RELEVANT,
        )
        assert scan.classification == EmailClassification.RELEVANT


class TestMCPSession:
    def test_instantiation(self):
        from models.schemas import MCPSession
        session = MCPSession(
            session_date=date(2026, 3, 14),
            summary="Reviewed Q1 progress",
        )
        assert session.decisions_made == []
        assert session.pending_items == []


class TestWeeklyReport:
    def test_instantiation(self):
        from models.schemas import WeeklyReport
        report = WeeklyReport(week_number=11, year=2026)
        assert report.report_url is None
        assert report.data is None


class TestMeetingPrepHistory:
    def test_instantiation(self):
        from models.schemas import MeetingPrepHistory
        prep = MeetingPrepHistory(
            meeting_type="investor",
            meeting_date=datetime(2026, 3, 15, 14, 0),
            prep_content={"sections": ["background", "agenda"]},
        )
        assert prep.status == "pending"
        assert prep.recipients is None


# =============================================================================
# Settings
# =============================================================================

class TestV1Settings:
    """Test new v1.0 settings have safe defaults."""

    def test_gantt_settings_default_empty(self):
        with patch.dict("os.environ", {}, clear=False):
            from config.settings import Settings
            s = Settings()
            assert s.GANTT_SHEET_ID == ""
            assert s.GANTT_BACKUP_FOLDER_ID == ""

    def test_email_intelligence_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            from config.settings import Settings
            s = Settings()
            assert s.EYAL_PERSONAL_EMAIL == ""
            assert s.PERSONAL_CONTACTS_BLOCKLIST == ""

    def test_mcp_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            from config.settings import Settings
            s = Settings()
            assert s.MCP_AUTH_TOKEN == ""
            assert s.MCP_PORT == 8080

    def test_weekly_review_default(self):
        with patch.dict("os.environ", {}, clear=False):
            from config.settings import Settings
            s = Settings()
            assert s.WEEKLY_REVIEW_CALENDAR_TITLE == "CropSight: Weekly Review with Gianluigi"

    def test_reports_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            from config.settings import Settings
            s = Settings()
            assert s.REPORTS_BASE_URL == ""
            assert s.REPORTS_SECRET_TOKEN == ""

    def test_drive_folder_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            from config.settings import Settings
            s = Settings()
            assert s.WEEKLY_REPORTS_FOLDER_ID == ""
            assert s.GANTT_SLIDES_FOLDER_ID == ""

    def test_blocklist_property_empty(self):
        with patch.dict("os.environ", {}, clear=False):
            from config.settings import Settings
            s = Settings()
            assert s.personal_contacts_blocklist_list == []

    def test_blocklist_property_parses(self):
        with patch.dict("os.environ", {"PERSONAL_CONTACTS_BLOCKLIST": "a@b.com, c@d.com, "}, clear=False):
            from config.settings import Settings
            s = Settings()
            assert s.personal_contacts_blocklist_list == ["a@b.com", "c@d.com"]


# =============================================================================
# Supabase CRUD Methods (Mocked)
# =============================================================================

def _make_mock_client():
    """Create a mock Supabase client with chainable query builder."""
    mock_client = MagicMock()
    mock_table = MagicMock()

    # Make all methods chainable
    for method in ["select", "insert", "update", "upsert", "delete",
                   "eq", "neq", "gte", "lte", "in_", "order", "limit"]:
        getattr(mock_table, method).return_value = mock_table

    mock_table.execute.return_value = MagicMock(data=[{"id": "test-id"}])
    mock_client.table.return_value = mock_table

    return mock_client, mock_table


class TestGanttSchemaCRUD:
    def test_upsert_gantt_schema_rows(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.upsert_gantt_schema_rows([{"sheet_name": "Main", "section": "Product", "row_number": 5}])
        mock_client.table.assert_called_with("gantt_schema")
        assert result == [{"id": "test-id"}]

    def test_get_gantt_schema(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.get_gantt_schema(sheet_name="Main")
        mock_table.eq.assert_called_with("sheet_name", "Main")

    def test_get_gantt_protected_rows(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.get_gantt_protected_rows("Main")
        # Should filter by sheet_name AND protected=True
        mock_table.eq.assert_any_call("sheet_name", "Main")
        mock_table.eq.assert_any_call("protected", True)


class TestGanttProposalsCRUD:
    def test_create_gantt_proposal(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.create_gantt_proposal("meeting", "src-1", [{"row": 5}])
        mock_client.table.assert_called_with("gantt_proposals")
        assert result == {"id": "test-id"}

    def test_get_gantt_proposal(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.get_gantt_proposal("prop-1")
        mock_table.eq.assert_called_with("id", "prop-1")

    def test_get_gantt_proposals_with_status(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.get_gantt_proposals(status="pending")
        mock_table.eq.assert_called_with("status", "pending")

    def test_update_gantt_proposal(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.update_gantt_proposal("prop-1", "approved", reviewed_by="eyal")
        mock_table.eq.assert_called_with("id", "prop-1")


class TestGanttSnapshotsCRUD:
    def test_create_gantt_snapshot(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.create_gantt_snapshot("prop-1", "Main", ["B5"], {"B5": ""}, {"B5": "done"})
        mock_client.table.assert_called_with("gantt_snapshots")

    def test_get_gantt_snapshots(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.get_gantt_snapshots("prop-1")
        mock_table.eq.assert_called_with("proposal_id", "prop-1")


class TestDebriefSessionsCRUD:
    def test_create_debrief_session(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.create_debrief_session("2026-03-14")
        mock_client.table.assert_called_with("debrief_sessions")

    def test_get_active_debrief_session(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.get_active_debrief_session()
        mock_table.eq.assert_called_with("status", "in_progress")

    def test_update_debrief_session(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.update_debrief_session("sess-1", status="approved")
        mock_table.eq.assert_called_with("id", "sess-1")


class TestEmailScansCRUD:
    def test_create_email_scan(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.create_email_scan("constant", "msg-1", "2026-03-14", sender="a@b.com")
        mock_client.table.assert_called_with("email_scans")

    def test_is_email_already_scanned_true(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        assert client.is_email_already_scanned("msg-1") is True

    def test_is_email_already_scanned_false(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        mock_table.execute.return_value = MagicMock(data=[])
        client._client = mock_client

        assert client.is_email_already_scanned("msg-1") is False

    def test_get_email_scans_with_filters(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.get_email_scans(scan_type="daily", date_from="2026-03-01")
        mock_table.eq.assert_called_with("scan_type", "daily")
        mock_table.gte.assert_called_with("date", "2026-03-01")


class TestMCPSessionsCRUD:
    def test_create_mcp_session(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.create_mcp_session("2026-03-14", "Reviewed Q1 progress")
        mock_client.table.assert_called_with("mcp_sessions")

    def test_get_latest_mcp_session(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.get_latest_mcp_session()
        mock_table.order.assert_called_with("session_date", desc=True)


class TestWeeklyReportsCRUD:
    def test_create_weekly_report(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.create_weekly_report(11, 2026, data={"summary": "test"})
        mock_client.table.assert_called_with("weekly_reports")

    def test_get_weekly_report(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.get_weekly_report(11, 2026)
        mock_table.eq.assert_any_call("week_number", 11)
        mock_table.eq.assert_any_call("year", 2026)

    def test_update_weekly_report(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.update_weekly_report("rep-1", report_url="https://example.com/report")
        mock_table.eq.assert_called_with("id", "rep-1")


class TestMeetingPrepHistoryCRUD:
    def test_create_meeting_prep_history(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        result = client.create_meeting_prep_history(
            "investor", "2026-03-15T14:00:00",
            {"sections": ["background"]}, calendar_event_id="cal-1"
        )
        mock_client.table.assert_called_with("meeting_prep_history")

    def test_get_meeting_prep_history_with_filter(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.get_meeting_prep_history(meeting_type="investor")
        mock_table.eq.assert_called_with("meeting_type", "investor")

    def test_update_meeting_prep_history(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient()
        mock_client, mock_table = _make_mock_client()
        client._client = mock_client

        client.update_meeting_prep_history("prep-1", "approved", approved_at="2026-03-15T15:00:00")
        mock_table.eq.assert_called_with("id", "prep-1")
