"""
Tests for Google Sheets professional formatting methods.

Tests:
- format_task_tracker() sends batchUpdate with formatting requests
- format_task_tracker() includes frozen header row
- format_task_tracker() includes header color and bold text
- format_task_tracker() includes status conditional formatting (4 rules on col F)
- format_task_tracker() includes category conditional formatting (6 rules on col B)
- format_task_tracker() includes priority conditional formatting (3 rules on col G)
- format_task_tracker() includes pending status rule
- format_task_tracker() includes fixed column widths (no autoResize)
- format_task_tracker() includes text wrapping on Task column (A)
- format_task_tracker() includes data validation dropdowns
- format_task_tracker() includes alternating row banding
- format_task_tracker() clears existing conditional format rules (idempotent)
- format_task_tracker() returns False when TASK_TRACKER_SHEET_ID is empty
- format_task_tracker() returns False when API throws an error
- format_stakeholder_tracker() sends batchUpdate with formatting requests
- format_stakeholder_tracker() returns False when STAKEHOLDER_TRACKER_SHEET_ID is empty
- format_stakeholder_tracker() includes frozen header row
- format_stakeholder_tracker() includes status conditional formatting (4 rules on col O)
- format_stakeholder_tracker() includes priority conditional formatting (3 rules on col F)
- format_stakeholder_tracker() includes borders
- format_stakeholder_tracker() includes alternating row banding
- format_stakeholder_tracker() includes fixed column widths
- format_stakeholder_tracker() includes text wrapping on Description and Notes
- format_stakeholder_tracker() includes data validation dropdowns
- format_stakeholder_tracker() clears existing conditional format rules
"""

import pytest
from unittest.mock import MagicMock, patch


def _make_service():
    """Create a GoogleSheetsService with a mocked Sheets API service."""
    from services.google_sheets import GoogleSheetsService

    svc = GoogleSheetsService()
    mock_api = MagicMock()
    svc._service = mock_api

    # Mock _get_first_sheet_id to return 0 and avoid real API calls
    # Mock the metadata response for _clear_conditional_format_rules
    mock_api.spreadsheets().get().execute.return_value = {
        "sheets": [
            {
                "properties": {"sheetId": 0},
                "conditionalFormats": [],
            }
        ]
    }

    return svc, mock_api


def _extract_requests(mock_api) -> list[dict]:
    """Pull the requests list from the batchUpdate call."""
    call_args = mock_api.spreadsheets().batchUpdate.call_args
    body = call_args.kwargs.get("body") or call_args[1].get("body")
    return body["requests"]


def _get_cond_rules(requests: list[dict]) -> list[dict]:
    """Extract all addConditionalFormatRule entries from requests."""
    return [r for r in requests if "addConditionalFormatRule" in r]


def _get_cond_keywords(cond_reqs: list[dict]) -> list[str]:
    """Extract the TEXT_CONTAINS keywords from conditional format rules."""
    keywords = []
    for cr in cond_reqs:
        rule = cr["addConditionalFormatRule"]["rule"]
        values = rule["booleanRule"]["condition"]["values"]
        keywords.append(values[0]["userEnteredValue"])
    return keywords


def _get_cond_rules_for_column(cond_reqs: list[dict], col_index: int) -> list[dict]:
    """Filter conditional format rules that target a specific column index."""
    result = []
    for cr in cond_reqs:
        ranges = cr["addConditionalFormatRule"]["rule"]["ranges"]
        if ranges[0]["startColumnIndex"] == col_index:
            result.append(cr)
    return result


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
            # Find the header repeatCell (row 0-1, has backgroundColor)
            header_reqs = [
                r for r in requests
                if "repeatCell" in r
                and r["repeatCell"]["range"].get("startRowIndex") == 0
                and r["repeatCell"]["range"].get("endRowIndex") == 1
            ]
            assert len(header_reqs) == 1, "Expected one header repeatCell request"

            cell_fmt = header_reqs[0]["repeatCell"]["cell"]["userEnteredFormat"]
            assert cell_fmt["textFormat"]["bold"] is True
            assert cell_fmt["textFormat"]["foregroundColor"]["red"] == 1.0
            bg = cell_fmt["backgroundColor"]
            assert bg["red"] == pytest.approx(0x1A / 255, abs=0.01)
            assert bg["blue"] == pytest.approx(0x7E / 255, abs=0.01)

    @pytest.mark.asyncio
    async def test_status_conditional_formatting(self):
        """Status column (F=5) should have 4 rules: overdue, done, in_progress, pending."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            cond_reqs = _get_cond_rules(requests)
            status_rules = _get_cond_rules_for_column(cond_reqs, 5)
            assert len(status_rules) == 4, "Expected 4 status conditional format rules"

            keywords = _get_cond_keywords(status_rules)
            assert "overdue" in keywords
            assert "done" in keywords
            assert "in_progress" in keywords
            assert "pending" in keywords

    @pytest.mark.asyncio
    async def test_category_conditional_formatting(self):
        """Category column (G=6) should have 6 rules, one per category."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            cond_reqs = _get_cond_rules(requests)
            # Phase 10: Category moved to column G (index 6)
            category_rules = _get_cond_rules_for_column(cond_reqs, 6)
            assert len(category_rules) == 6, "Expected 6 category conditional format rules"

            keywords = _get_cond_keywords(category_rules)
            assert "Product & Tech" in keywords
            assert "BD & Sales" in keywords
            assert "Strategy & Research" in keywords
            assert "Finance & Fundraising" in keywords
            assert "Legal & Compliance" in keywords
            assert "Operations & HR" in keywords

    @pytest.mark.asyncio
    async def test_priority_conditional_formatting(self):
        """Priority column (A=0) should have 3 rules: H, M, L."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            cond_reqs = _get_cond_rules(requests)
            # Phase 10: Priority moved to column A (index 0)
            priority_rules = _get_cond_rules_for_column(cond_reqs, 0)
            assert len(priority_rules) == 3, "Expected 3 priority conditional format rules"

            keywords = _get_cond_keywords(priority_rules)
            assert "H" in keywords
            assert "M" in keywords
            assert "L" in keywords

    @pytest.mark.asyncio
    async def test_fixed_column_widths_no_auto_resize(self):
        """Should use updateDimensionProperties for column widths, NOT autoResizeDimensions."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)

            # No autoResize
            auto_resize = [r for r in requests if "autoResizeDimensions" in r]
            assert len(auto_resize) == 0, "Should NOT have autoResizeDimensions"

            # Should have 9 column width requests (A-I)
            dim_reqs = [r for r in requests if "updateDimensionProperties" in r]
            assert len(dim_reqs) == 9, "Expected 9 column width requests"

            # Phase 10: Priority column (A=0) is 50px, Task column (C=2) is 350px
            col_a = [
                r for r in dim_reqs
                if r["updateDimensionProperties"]["range"]["startIndex"] == 0
            ]
            assert col_a[0]["updateDimensionProperties"]["properties"]["pixelSize"] == 50

            col_c = [
                r for r in dim_reqs
                if r["updateDimensionProperties"]["range"]["startIndex"] == 2
            ]
            assert col_c[0]["updateDimensionProperties"]["properties"]["pixelSize"] == 350

    @pytest.mark.asyncio
    async def test_text_wrap_on_task_column(self):
        """Task column (C=2) should have wrapStrategy: WRAP."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)

            # Find repeatCell requests that set wrapStrategy (not the header one)
            wrap_reqs = [
                r for r in requests
                if "repeatCell" in r
                and r["repeatCell"].get("cell", {})
                    .get("userEnteredFormat", {})
                    .get("wrapStrategy") == "WRAP"
            ]
            assert len(wrap_reqs) >= 1, "Expected at least one text wrap request"

            # Phase 10: Task moved to column C (index 2)
            col_indices = [
                r["repeatCell"]["range"]["startColumnIndex"]
                for r in wrap_reqs
            ]
            assert 2 in col_indices, "Text wrap should include column C (index 2)"

    @pytest.mark.asyncio
    async def test_no_data_validation_dropdowns(self):
        """Phase 10: Data validation dropdowns removed (cause errors with existing data)."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            validation_reqs = [r for r in requests if "setDataValidation" in r]
            assert len(validation_reqs) == 0, "Data validation should be removed from Task Tracker"

    @pytest.mark.asyncio
    async def test_alternating_row_banding(self):
        """Should include an addBanding request for zebra striping."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            banding_reqs = [r for r in requests if "addBanding" in r]
            assert len(banding_reqs) == 1, "Expected one addBanding request"

            # Should start from row 1 (skip header)
            banded = banding_reqs[0]["addBanding"]["bandedRange"]
            assert banded["range"]["startRowIndex"] == 1

    @pytest.mark.asyncio
    async def test_clears_existing_conditional_rules(self):
        """Should delete existing conditional format rules before adding new ones."""
        svc, mock_api = _make_service()

        # Mock metadata with 3 existing rules
        mock_api.spreadsheets().get().execute.return_value = {
            "sheets": [
                {
                    "properties": {"sheetId": 0},
                    "conditionalFormats": [
                        {"booleanRule": {}},
                        {"booleanRule": {}},
                        {"booleanRule": {}},
                    ],
                }
            ]
        }

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "task-sheet-123"

            await svc.format_task_tracker()

            requests = _extract_requests(mock_api)
            delete_reqs = [
                r for r in requests if "deleteConditionalFormatRule" in r
            ]
            assert len(delete_reqs) == 3, "Should delete 3 existing rules"

            # Verify they're in reverse order (2, 1, 0) so indices stay valid
            indices = [
                r["deleteConditionalFormatRule"]["index"] for r in delete_reqs
            ]
            assert indices == [2, 1, 0], "Delete should be in reverse index order"

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

    @pytest.mark.asyncio
    async def test_status_conditional_formatting(self):
        """Status column (O=14) should have 4 rules: New, Active, Inactive, Completed."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)
            cond_reqs = _get_cond_rules(requests)
            status_rules = _get_cond_rules_for_column(cond_reqs, 14)
            assert len(status_rules) == 4, "Expected 4 status conditional format rules"

            keywords = _get_cond_keywords(status_rules)
            assert "New" in keywords
            assert "Active" in keywords
            assert "Inactive" in keywords
            assert "Completed" in keywords

    @pytest.mark.asyncio
    async def test_priority_conditional_formatting(self):
        """Priority column (F=5) should have 3 rules: H, M, L."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)
            cond_reqs = _get_cond_rules(requests)
            priority_rules = _get_cond_rules_for_column(cond_reqs, 5)
            assert len(priority_rules) == 3, "Expected 3 priority conditional format rules"

            keywords = _get_cond_keywords(priority_rules)
            assert "H" in keywords
            assert "M" in keywords
            assert "L" in keywords

    @pytest.mark.asyncio
    async def test_includes_borders(self):
        """Should include an updateBorders request covering all 19 columns."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)
            border_reqs = [r for r in requests if "updateBorders" in r]
            assert len(border_reqs) == 1, "Expected one updateBorders request"
            assert border_reqs[0]["updateBorders"]["range"]["endColumnIndex"] == 19

    @pytest.mark.asyncio
    async def test_alternating_row_banding(self):
        """Should include an addBanding request for zebra striping."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)
            banding_reqs = [r for r in requests if "addBanding" in r]
            assert len(banding_reqs) == 1, "Expected one addBanding request"

            banded = banding_reqs[0]["addBanding"]["bandedRange"]
            assert banded["range"]["startRowIndex"] == 1
            assert banded["range"]["endColumnIndex"] == 19

    @pytest.mark.asyncio
    async def test_fixed_column_widths(self):
        """Should use updateDimensionProperties for 16 columns, NOT autoResizeDimensions."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)

            auto_resize = [r for r in requests if "autoResizeDimensions" in r]
            assert len(auto_resize) == 0, "Should NOT have autoResizeDimensions"

            dim_reqs = [r for r in requests if "updateDimensionProperties" in r]
            assert len(dim_reqs) == 16, "Expected 16 column width requests"

    @pytest.mark.asyncio
    async def test_text_wrap_on_description_and_notes(self):
        """Description (C=2) and Notes (P=15) should have wrapStrategy: WRAP."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)
            wrap_reqs = [
                r for r in requests
                if "repeatCell" in r
                and r["repeatCell"].get("cell", {})
                    .get("userEnteredFormat", {})
                    .get("wrapStrategy") == "WRAP"
            ]
            assert len(wrap_reqs) == 2, "Expected 2 text wrap requests"

            col_indices = sorted([
                r["repeatCell"]["range"]["startColumnIndex"]
                for r in wrap_reqs
            ])
            assert col_indices == [2, 15], "Text wrap should be on C (2) and P (15)"

    @pytest.mark.asyncio
    async def test_data_validation_dropdowns(self):
        """Should have setDataValidation for Status (O=14) and Priority (F=5)."""
        svc, mock_api = _make_service()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)
            validation_reqs = [r for r in requests if "setDataValidation" in r]
            assert len(validation_reqs) == 2, "Expected 2 data validation requests"

            col_indices = [
                r["setDataValidation"]["range"]["startColumnIndex"]
                for r in validation_reqs
            ]
            assert 14 in col_indices, "Status column (O=14) should have validation"
            assert 5 in col_indices, "Priority column (F=5) should have validation"

    @pytest.mark.asyncio
    async def test_clears_existing_conditional_rules(self):
        """Should delete existing conditional format rules before adding new ones."""
        svc, mock_api = _make_service()

        # Mock metadata with 2 existing rules
        mock_api.spreadsheets().get().execute.return_value = {
            "sheets": [
                {
                    "properties": {"sheetId": 0},
                    "conditionalFormats": [
                        {"booleanRule": {}},
                        {"booleanRule": {}},
                    ],
                }
            ]
        }

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.STAKEHOLDER_TRACKER_SHEET_ID = "stakeholder-sheet-456"

            await svc.format_stakeholder_tracker()

            requests = _extract_requests(mock_api)
            delete_reqs = [
                r for r in requests if "deleteConditionalFormatRule" in r
            ]
            assert len(delete_reqs) == 2, "Should delete 2 existing rules"

            indices = [
                r["deleteConditionalFormatRule"]["index"] for r in delete_reqs
            ]
            assert indices == [1, 0], "Delete should be in reverse index order"
