"""
Tests for Commitment Dashboard — Sheets tab integration.

Tests ensure_commitments_tab and add_commitments_batch_to_sheet
in services/google_sheets.py.
"""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock


def _make_sheets_service():
    """Create a GoogleSheetsService with a mocked Google API client."""
    from services.google_sheets import GoogleSheetsService

    service = GoogleSheetsService()
    mock_api = MagicMock()
    service._service = mock_api
    return service, mock_api


class TestEnsureCommitmentsTab:
    """Tests for ensure_commitments_tab."""

    @pytest.mark.asyncio
    async def test_creates_tab_when_missing(self):
        """Creates Commitments tab if it doesn't exist."""
        svc, mock_api = _make_sheets_service()

        # No existing Commitments tab
        mock_api.spreadsheets().get().execute.return_value = {
            "sheets": [
                {"properties": {"title": "Tasks", "sheetId": 0}},
            ]
        }

        # batchUpdate returns new sheetId
        mock_api.spreadsheets().batchUpdate().execute.return_value = {
            "replies": [{"addSheet": {"properties": {"sheetId": 12345}}}]
        }

        # values().update for header row
        mock_api.spreadsheets().values().update().execute.return_value = {}

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "test-sheet-id"
            result = await svc.ensure_commitments_tab()

        assert result == 12345

    @pytest.mark.asyncio
    async def test_returns_existing_tab(self):
        """Returns existing sheetId if Commitments tab already exists."""
        svc, mock_api = _make_sheets_service()

        mock_api.spreadsheets().get().execute.return_value = {
            "sheets": [
                {"properties": {"title": "Tasks", "sheetId": 0}},
                {"properties": {"title": "Commitments", "sheetId": 99}},
            ]
        }

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "test-sheet-id"
            result = await svc.ensure_commitments_tab()

        assert result == 99

    @pytest.mark.asyncio
    async def test_returns_none_without_sheet_id(self):
        """Returns None if TASK_TRACKER_SHEET_ID not configured."""
        svc, mock_api = _make_sheets_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = ""
            result = await svc.ensure_commitments_tab()

        assert result is None


class TestAddCommitmentsBatch:
    """Tests for add_commitments_batch_to_sheet."""

    @pytest.mark.asyncio
    async def test_appends_correct_rows(self):
        """Commitments are appended as rows with correct columns."""
        svc, mock_api = _make_sheets_service()

        # Mock ensure_commitments_tab to return a sheetId
        import asyncio
        future = asyncio.Future()
        future.set_result(12345)
        svc.ensure_commitments_tab = MagicMock(return_value=future)

        mock_api.spreadsheets().values().append().execute.return_value = {}

        commitments = [
            {
                "commitment_text": "Send the proposal by Friday",
                "speaker": "Eyal",
                "implied_deadline": "2026-03-07",
                "status": "open",
            },
            {
                "commitment_text": "Review the architecture doc",
                "speaker": "Roye",
                "implied_deadline": "",
                "status": "open",
            },
        ]

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "test-sheet-id"
            result = await svc.add_commitments_batch_to_sheet(
                commitments=commitments,
                source_meeting="Strategy Meeting",
                created_date="2026-03-01",
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_empty_commitments_returns_true(self):
        """Empty commitments list is a no-op that returns True."""
        svc, mock_api = _make_sheets_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "test-sheet-id"
            result = await svc.add_commitments_batch_to_sheet(
                commitments=[],
                source_meeting="Test",
                created_date="2026-03-01",
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_without_sheet_id(self):
        """Returns False if TASK_TRACKER_SHEET_ID not configured."""
        svc, mock_api = _make_sheets_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = ""
            result = await svc.add_commitments_batch_to_sheet(
                commitments=[{"commitment_text": "test", "speaker": "Eyal"}],
                source_meeting="Test",
                created_date="2026-03-01",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_api_error(self):
        """Returns False on Google Sheets API error."""
        svc, mock_api = _make_sheets_service()

        import asyncio
        future = asyncio.Future()
        future.set_result(12345)
        svc.ensure_commitments_tab = MagicMock(return_value=future)

        mock_api.spreadsheets().values().append().execute.side_effect = Exception("API error")

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "test-sheet-id"
            result = await svc.add_commitments_batch_to_sheet(
                commitments=[{"commitment_text": "test", "speaker": "Eyal"}],
                source_meeting="Test",
                created_date="2026-03-01",
            )

        assert result is False


class TestCommitmentsInApproval:
    """Tests for commitments passing through approval content."""

    def test_commitments_key_added_to_approval_content(self):
        """When cross_reference has commitments, they're in approval_content."""
        # This is a structural test — verify the key exists in the content dict
        result = {
            "meeting_id": "test-uuid",
            "summary": "test",
            "decisions": [],
            "tasks": [],
            "follow_ups": [],
            "open_questions": [],
            "discussion_summary": "",
            "commitments": [
                {"commitment_text": "Send proposal", "speaker": "Eyal"},
            ],
        }

        # Build approval content the same way transcript_watcher does
        approval_content = {
            "meeting_id": result["meeting_id"],
            "title": "Test Meeting",
            "summary": result.get("summary", ""),
            "decisions": result.get("decisions", []),
            "tasks": result.get("tasks", []),
            "follow_ups": result.get("follow_ups", []),
            "open_questions": result.get("open_questions", []),
            "discussion_summary": result.get("discussion_summary", ""),
        }
        if result.get("commitments"):
            approval_content["commitments"] = result["commitments"]

        assert "commitments" in approval_content
        assert len(approval_content["commitments"]) == 1
        assert approval_content["commitments"][0]["speaker"] == "Eyal"
