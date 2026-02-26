"""
Tests for Google Sheets professional formatting methods.

Tests:
- format_task_tracker() sends batchUpdate with formatting requests
- format_task_tracker() includes frozen header row
- format_task_tracker() includes header color and bold text
- format_task_tracker() includes conditional formatting for status column
- format_task_tracker() returns False when TASK_TRACKER_SHEET_ID is empty
- format_task_tracker() returns False when API throws an error
- format_stakeholder_tracker() sends batchUpdate with formatting requests
- format_stakeholder_tracker() returns False when STAKEHOLDER_TRACKER_SHEET_ID is empty
- format_stakeholder_tracker() includes frozen header row
"""

import pytest
from unittest.mock import MagicMock, patch


def _make_service():
    """Create a GoogleSheetsService with a mocked Sheets API service."""
    from services.google_sheets import GoogleSheetsService

    svc = GoogleSheetsService()
    mock_api = MagicMock()
    svc._service = mock_api
    return svc, mock_api


def _extract_requests(mock_api) -> list[dict]:
    """Pull the requests list from the batchUpdate call."""
    call_args = mock_api.spreadsheets().batchUpdate.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    return body["requests"]


# =============================================================================
# TestFormatTaskTracker
# =============================================================================

class TestFormatTaskTracker:
    """Tests for format_task_tracker() — professional formatting on Task Tracker."""

    @pytest.mark.asyncio
    async def test_sends_batch_update_request(self):
        """Should call batchUpdate with the Task Tracker sheet ID."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            result = await svc.format_task_tracker()

            assert result is True
            mock_api.spreadsheets().batchUpdate.assert_called_once()
            call_kwargs = mock_api.spreadsheets().batchUpdate.call_args.kwargs
            assert call_kwargs["spreadsheetId"] == "task-sheet-123"

    @pytest.mark.asyncio
    async def test_includes_frozen_header(self):
        """Requests should contain an updateSheetProperties with frozenRowCount=1."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            freeze_reqs = [
                r for r in requests
                if "updateSheetProperties" in r
                and r["updateSheetProperties"]["properties"]
                    .get("gridProperties", {})
                    .get("frozenRowCount") == 1
            ]
            assert len(freeze_reqs) == 1, "Expected exactly one freeze-row request"

    @pytest.mark.asyncio
    async def test_includes_header_formatting(self):
        """Requests should contain a repeatCell for header row with bold white text on dark blue."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            header_reqs = [r for r in requests if "repeatCell" in r]
            assert len(header_reqs) >= 1, "Expected at least one repeatCell request"

            cell_fmt = header_reqs[0]["repeatCell"]["cell"]["userEnteredFormat"]
            # Bold white text
            assert cell_fmt["textFormat"]["bold"] is True
            assert cell_fmt["textFormat"]["foregroundColor"]["red"] == 1.0
            # Dark blue background (0x1A / 255 ~ 0.102)
            bg = cell_fmt["backgroundColor"]
            assert bg["red"] == pytest.approx(0x1A / 255, abs=0.01)
            assert bg["blue"] == pytest.approx(0x7E / 255, abs=0.01)

    @pytest.mark.asyncio
    async def test_includes_conditional_formatting(self):
        """Requests should contain addConditionalFormatRule for overdue, done, in_progress."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            cond_reqs = [r for r in requests if "addConditionalFormatRule" in r]
            assert len(cond_reqs) == 3, "Expected 3 conditional format rules"

            # Collect the status keywords from the rules
            keywords = []
            for cr in cond_reqs:
                rule = cr["addConditionalFormatRule"]["rule"]
                values = rule["booleanRule"]["condition"]["values"]
                keywords.append(values[0]["userEnteredValue"])

            assert "overdue" in keywords
            assert "done" in keywords
            assert "in_progress" in keywords

    @pytest.mark.asyncio
    async def test_returns_false_when_no_sheet_id(self):
        """Should return False immediately when TASK_TRACKER_SHEET_ID is empty."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = ""

            result = await svc.format_task_tracker()

            assert result is False
            mock_api.spreadsheets().batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_on_error(self):
        """Should return False and log error when batchUpdate raises."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"
            mock_api.spreadsheets().batchUpdate().execute.side_effect = Exception(
                "API quota exceeded"
            )

            result = await svc.format_task_tracker()

            assert result is False


# =============================================================================
# TestFormatStakeholderTracker
# =============================================================================

class TestFormatStakeholderTracker:
    """Tests for format_stakeholder_tracker() — professional formatting on Stakeholder Tracker."""

    @pytest.mark.asyncio
    async def test_sends_batch_update_request(self):
        """Should call batchUpdate with the Stakeholder Tracker sheet ID."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            result = await svc.format_stakeholder_tracker()

            assert result is True
            mock_api.spreadsheets().batchUpdate.assert_called_once()
            call_kwargs = mock_api.spreadsheets().batchUpdate.call_args.kwargs
            assert call_kwargs["spreadsheetId"] == "stakeholder-sheet-456"

    @pytest.mark.asyncio
    async def test_returns_false_when_no_sheet_id(self):
        """Should return False immediately when STAKEHOLDER_TRACKER_SHEET_ID is empty."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = ""

            result = await svc.format_stakeholder_tracker()

            assert result is False
            mock_api.spreadsheets().batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_includes_frozen_header(self):
        """Requests should contain an updateSheetProperties with frozenRowCount=1."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)
            freeze_reqs = [
                r for r in requests
                if "updateSheetProperties" in r
                and r["updateSheetProperties"]["properties"]
                    .get("gridProperties", {})
                    .get("frozenRowCount") == 1
            ]
            assert len(freeze_reqs) == 1, "Expected exactly one freeze-row request"
