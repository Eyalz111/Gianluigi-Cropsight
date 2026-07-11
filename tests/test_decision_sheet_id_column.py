"""Phase 2 (editable Decisions sheet) — the id column (col H) + protection.

The id is the reconcile identity key, gated on DECISION_RECONCILE_ENABLED and
resolved at RUNTIME (no module reload needed, unlike the task urgency column).
Flag off => historical A:G 7-column layout, untouched. Flag on => A:H with ids
and the system-owned columns (E source, F date, H id) protected — Status (G)
stays editable, so E:F and H are two separate protected ranges.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
import services.google_sheets as gs


class TestLayout:
    def test_flag_off_is_seven_columns(self):
        with patch.object(settings, "DECISION_RECONCILE_ENABLED", False):
            assert gs._decision_id_enabled() is False
            assert gs._decision_headers() == list(gs.DECISION_TRACKER_HEADERS)
            assert len(gs._decision_headers()) == 7
            assert "ID" not in gs._decision_headers()

    def test_flag_on_appends_id(self):
        with patch.object(settings, "DECISION_RECONCILE_ENABLED", True):
            assert gs._decision_id_enabled() is True
            headers = gs._decision_headers()
            assert len(headers) == 8
            assert headers[-1] == "ID"
            # base constant is never mutated
            assert len(gs.DECISION_TRACKER_HEADERS) == 7


def _make_service():
    svc = gs.GoogleSheetsService()
    mock_api = MagicMock()
    svc._service = mock_api
    return svc, mock_api


def _last_update_values(mock_api) -> list[list]:
    call = mock_api.spreadsheets().values().update.call_args
    body = call.kwargs.get("body") or call[1].get("body")
    return body["values"]


class TestRebuildWritesId:
    async def test_flag_on_writes_id_cell(self):
        svc, mock_api = _make_service()
        decisions = [{"id": "dec-uuid-1", "label": "Product", "description": "Ship MVP",
                      "rationale": "signal", "confidence": 4, "created_at": "2026-07-11",
                      "decision_status": "active"}]
        with patch.object(settings, "DECISION_RECONCILE_ENABLED", True), \
             patch.object(settings, "TASK_TRACKER_SHEET_ID", "sheet-x"), \
             patch.object(svc, "ensure_decisions_tab", AsyncMock(return_value=100)), \
             patch.object(svc, "format_decision_tracker", AsyncMock(return_value=True)), \
             patch("services.supabase_client.supabase_client.log_action", MagicMock()):
            ok = await svc.rebuild_decisions_sheet(decisions)
        assert ok is True
        values = _last_update_values(mock_api)
        assert values[0][-1] == "ID"                 # header row carries ID
        assert values[1][-1] == "dec-uuid-1"         # data row carries the UUID in col H
        assert len(values[1]) == 8

    async def test_flag_off_no_id_cell(self):
        svc, mock_api = _make_service()
        decisions = [{"id": "dec-uuid-1", "label": "Product", "description": "Ship MVP",
                      "confidence": 4, "created_at": "2026-07-11", "decision_status": "active"}]
        with patch.object(settings, "DECISION_RECONCILE_ENABLED", False), \
             patch.object(settings, "TASK_TRACKER_SHEET_ID", "sheet-x"), \
             patch.object(svc, "ensure_decisions_tab", AsyncMock(return_value=100)), \
             patch("services.supabase_client.supabase_client.log_action", MagicMock()):
            ok = await svc.rebuild_decisions_sheet(decisions)
        assert ok is True
        values = _last_update_values(mock_api)
        assert len(values[1]) == 7                   # A:G, no id
        assert "dec-uuid-1" not in values[1]


class TestProtection:
    async def test_noop_when_flag_off(self):
        svc, mock_api = _make_service()
        with patch.object(settings, "DECISION_RECONCILE_ENABLED", False), \
             patch.object(settings, "TASK_TRACKER_SHEET_ID", "sheet-x"):
            assert await svc.format_decision_tracker(sheet_id=0) is True
        mock_api.spreadsheets().batchUpdate.assert_not_called()

    async def test_protects_ef_and_h_when_on(self):
        svc, mock_api = _make_service()
        mock_api.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {"sheetId": 0}, "protectedRanges": []}]
        }
        with patch.object(settings, "DECISION_RECONCILE_ENABLED", True), \
             patch.object(settings, "TASK_TRACKER_SHEET_ID", "sheet-x"):
            ok = await svc.format_decision_tracker(sheet_id=0)
        assert ok is True
        call = mock_api.spreadsheets().batchUpdate.call_args
        reqs = (call.kwargs.get("body") or call[1].get("body"))["requests"]
        protects = [r["addProtectedRange"]["protectedRange"]["range"] for r in reqs
                    if "addProtectedRange" in r]
        cols = {(r["startColumnIndex"], r["endColumnIndex"]) for r in protects}
        assert (4, 6) in cols    # E:F  (source_meeting, date)
        assert (7, 8) in cols    # H    (id)
        assert all(r["startRowIndex"] == 1 for r in protects)  # header row skipped
