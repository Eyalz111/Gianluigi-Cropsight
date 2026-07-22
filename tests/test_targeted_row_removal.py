"""
Targeted Sheet row removal (delete_task_rows_by_id / delete_decision_rows_by_id).

Replaces the clear-and-rewrite that the reject cascade used to perform. A full
rebuild repaints every cell from the DB, destroying any human edit not yet pulled
by reconcile — and because the rebuild leaves `sheet_snapshots` untouched, the
next reconcile sees cell == snapshot and never detects the loss. [2026-07-22]

The subtle requirement is BOTTOM-UP deletion: deleting row 3 before row 7 shifts
row 7 up by one, so a top-down pass deletes the wrong rows.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _svc(rows, sheet_id=42):
    from services.google_sheets import GoogleSheetsService

    svc = GoogleSheetsService()
    svc.get_all_tasks = AsyncMock(return_value=rows)
    svc.get_all_decisions = AsyncMock(return_value=rows)
    svc._get_sheet_id_by_name = MagicMock(return_value=sheet_id)
    svc._execute_with_retry = MagicMock(side_effect=lambda fn: fn())
    # `service` is a lazy property with no setter — seed the cached handle and
    # neutralise the freshness check so the property returns our mock.
    svc._service = MagicMock()
    svc._ensure_fresh_credentials = MagicMock(return_value=None)
    return svc


def _delete_indices(svc):
    """Extract (startIndex, endIndex) pairs from the batchUpdate call, in order."""
    call = svc._service.spreadsheets.return_value.batchUpdate.call_args
    if call is None:
        return []
    return [
        (r["deleteDimension"]["range"]["startIndex"],
         r["deleteDimension"]["range"]["endIndex"])
        for r in call.kwargs["body"]["requests"]
    ]


class TestTargetedTaskRowRemoval:
    @pytest.mark.asyncio
    async def test_deletes_only_matching_rows_bottom_up(self):
        rows = [
            {"id": "keep-1", "row_number": 2},
            {"id": "drop-a", "row_number": 3},
            {"id": "keep-2", "row_number": 4},
            {"id": "drop-b", "row_number": 7},
        ]
        svc = _svc(rows)

        with patch("services.google_sheets.settings") as s:
            s.TASK_TRACKER_SHEET_ID = "sheet-abc"
            s.TASK_TRACKER_TAB_NAME = "Tasks"
            removed = await svc.delete_task_rows_by_id(["drop-a", "drop-b"])

        assert removed == 2
        # bottom-up: row 7 before row 3, else deleting 3 shifts 7 up to 6
        assert _delete_indices(svc) == [(6, 7), (2, 3)]

    @pytest.mark.asyncio
    async def test_no_ids_is_a_noop(self):
        svc = _svc([{"id": "x", "row_number": 2}])
        with patch("services.google_sheets.settings") as s:
            s.TASK_TRACKER_SHEET_ID = "sheet-abc"
            s.TASK_TRACKER_TAB_NAME = "Tasks"
            assert await svc.delete_task_rows_by_id([]) == 0
            assert await svc.delete_task_rows_by_id(["", "  "]) == 0
        svc._service.spreadsheets.return_value.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_ids_delete_nothing(self):
        """An id the sheet doesn't carry must never delete a row by position."""
        svc = _svc([{"id": "a", "row_number": 2}, {"id": "b", "row_number": 3}])
        with patch("services.google_sheets.settings") as s:
            s.TASK_TRACKER_SHEET_ID = "sheet-abc"
            s.TASK_TRACKER_TAB_NAME = "Tasks"
            assert await svc.delete_task_rows_by_id(["not-in-sheet"]) == 0
        svc._service.spreadsheets.return_value.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_tab_is_safe(self):
        svc = _svc([{"id": "a", "row_number": 2}])
        svc._get_sheet_id_by_name = MagicMock(return_value=None)
        with patch("services.google_sheets.settings") as s:
            s.TASK_TRACKER_SHEET_ID = "sheet-abc"
            s.TASK_TRACKER_TAB_NAME = "Tasks"
            assert await svc.delete_task_rows_by_id(["a"]) == 0
        svc._service.spreadsheets.return_value.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_failure_never_raises(self):
        """Sheet upkeep is best-effort — it must not fail the reject it follows."""
        svc = _svc([{"id": "a", "row_number": 2}])
        svc._execute_with_retry = MagicMock(side_effect=RuntimeError("boom"))
        with patch("services.google_sheets.settings") as s:
            s.TASK_TRACKER_SHEET_ID = "sheet-abc"
            s.TASK_TRACKER_TAB_NAME = "Tasks"
            assert await svc.delete_task_rows_by_id(["a"]) == 0


class TestTargetedDecisionRowRemoval:
    @pytest.mark.asyncio
    async def test_deletes_matching_decision_rows(self):
        svc = _svc([{"id": "d1", "row_number": 5}, {"id": "d2", "row_number": 9}])
        with patch("services.google_sheets.settings") as s, \
             patch("services.google_sheets._decision_id_enabled", return_value=True):
            s.TASK_TRACKER_SHEET_ID = "sheet-abc"
            removed = await svc.delete_decision_rows_by_id(["d2"])
        assert removed == 1
        assert _delete_indices(svc) == [(8, 9)]

    @pytest.mark.asyncio
    async def test_noop_when_id_column_disabled(self):
        """Without the col-H id there is no safe way to identify rows."""
        svc = _svc([{"id": "", "row_number": 5}])
        with patch("services.google_sheets.settings") as s, \
             patch("services.google_sheets._decision_id_enabled", return_value=False):
            s.TASK_TRACKER_SHEET_ID = "sheet-abc"
            assert await svc.delete_decision_rows_by_id(["d1"]) == 0
        svc._service.spreadsheets.return_value.batchUpdate.assert_not_called()
