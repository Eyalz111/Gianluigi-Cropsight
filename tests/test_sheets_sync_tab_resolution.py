"""
Regression tests for the 2026-04-11 sheets-sync hardening pass.

The original bug: get_all_tasks() and ensure_headers() called the Sheets
read API with a bare A1 range like "A:I" — no tab prefix. The Sheets API
resolves bare ranges against the sheet at index 0, so the moment any
backup or other tab landed in front of "Tasks" the read silently returned
the wrong data, breaking /sync, the task reminder scheduler, MCP task
updates, and the Telegram task buttons.

Also covers the defensive guard added to rebuild_tasks_sheet() and
rebuild_decisions_sheet() that refuses to clear a populated sheet when
fed an empty list (safety net for silent Supabase read failures).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =============================================================================
# Bare-range bug — get_all_tasks must specify the tab name
# =============================================================================


class TestGetAllTasksUsesExplicitTabName:
    @pytest.mark.asyncio
    async def test_get_all_tasks_passes_tab_qualified_range(self):
        """get_all_tasks must call _read_sheet_range with 'Tasks'!A:I, not bare A:I."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._read_sheet_range = AsyncMock(return_value=[])

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet-abc"
            mock_settings.TASK_TRACKER_TAB_NAME = "Tasks"

            await svc.get_all_tasks()

        svc._read_sheet_range.assert_called_once()
        kwargs = svc._read_sheet_range.call_args.kwargs
        assert "Tasks" in kwargs["range_name"], (
            f"get_all_tasks must qualify the range with the tab name to avoid "
            f"reading from whichever sheet sits at index 0. Got: {kwargs['range_name']!r}"
        )
        assert kwargs["range_name"].startswith("'Tasks'!") or kwargs["range_name"].startswith("Tasks!")

    @pytest.mark.asyncio
    async def test_get_all_tasks_honors_custom_tab_name(self):
        """If TASK_TRACKER_TAB_NAME is overridden, the range must use it."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._read_sheet_range = AsyncMock(return_value=[])

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet-abc"
            mock_settings.TASK_TRACKER_TAB_NAME = "CustomTasks"

            await svc.get_all_tasks()

        kwargs = svc._read_sheet_range.call_args.kwargs
        assert "CustomTasks" in kwargs["range_name"]

    @pytest.mark.asyncio
    async def test_ensure_headers_passes_tab_qualified_range(self):
        """ensure_headers has the same bare-range hazard and must be tab-qualified."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._read_sheet_range = AsyncMock(return_value=[["Priority"]])  # non-empty so no write
        svc._write_sheet_range = AsyncMock()

        with patch("services.google_sheets.settings") as mock_settings:
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet-abc"
            mock_settings.TASK_TRACKER_TAB_NAME = "Tasks"

            await svc.ensure_task_tracker_headers()

        svc._read_sheet_range.assert_called_once()
        kwargs = svc._read_sheet_range.call_args.kwargs
        assert "Tasks" in kwargs["range_name"]


# =============================================================================
# Defensive guard — rebuild refuses to clear when fed an empty list
# =============================================================================


class TestRebuildRefusesEmptyByDefault:
    @pytest.mark.asyncio
    async def test_rebuild_tasks_sheet_refuses_empty_input(self):
        """An empty tasks_from_db must NOT clear the live sheet (safety net)."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._service = MagicMock()  # would explode if accessed
        svc.format_task_tracker = AsyncMock()

        with patch("services.google_sheets.settings") as mock_settings, \
             patch("services.supabase_client.supabase_client") as mock_sb:
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet-abc"
            mock_settings.TASK_TRACKER_TAB_NAME = "Tasks"

            ok = await svc.rebuild_tasks_sheet([])

        assert ok is False, "rebuild_tasks_sheet must refuse empty input by default"
        # The Sheets API must not have been touched
        svc._service.spreadsheets().values().clear.assert_not_called()
        # And the refusal should be audit-logged
        mock_sb.log_action.assert_called_once()
        assert mock_sb.log_action.call_args.kwargs["action"] == "sheets_rebuild_refused_empty"

    @pytest.mark.asyncio
    async def test_rebuild_tasks_sheet_allows_empty_with_force(self):
        """force_empty=True is the explicit opt-in for legitimate resets."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._service = MagicMock()
        svc.format_task_tracker = AsyncMock()

        with patch("services.google_sheets.settings") as mock_settings, \
             patch("services.supabase_client.supabase_client"):
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet-abc"
            mock_settings.TASK_TRACKER_TAB_NAME = "Tasks"

            ok = await svc.rebuild_tasks_sheet([], force_empty=True)

        assert ok is True
        svc._service.spreadsheets().values().clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_rebuild_decisions_sheet_refuses_empty_input(self):
        """Same defensive guard for the Decisions tab."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._service = MagicMock()

        with patch("services.google_sheets.settings") as mock_settings, \
             patch("services.supabase_client.supabase_client") as mock_sb:
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet-abc"

            ok = await svc.rebuild_decisions_sheet([])

        assert ok is False
        svc._service.spreadsheets().values().clear.assert_not_called()
        mock_sb.log_action.assert_called_once()
        assert mock_sb.log_action.call_args.kwargs["action"] == "sheets_rebuild_refused_empty"

    @pytest.mark.asyncio
    async def test_rebuild_tasks_sheet_with_data_audit_logs(self):
        """A successful rebuild must record an audit_log entry."""
        from services.google_sheets import GoogleSheetsService

        svc = GoogleSheetsService()
        svc._service = MagicMock()
        svc.format_task_tracker = AsyncMock()

        sample = [{"title": "T1", "assignee": "Eyal", "priority": "M",
                   "status": "pending", "category": "BD", "label": "",
                   "deadline": None, "created_at": "2026-04-11"}]

        with patch("services.google_sheets.settings") as mock_settings, \
             patch("services.supabase_client.supabase_client") as mock_sb:
            mock_settings.TASK_TRACKER_SHEET_ID = "sheet-abc"
            mock_settings.TASK_TRACKER_TAB_NAME = "Tasks"

            ok = await svc.rebuild_tasks_sheet(sample)

        assert ok is True
        # Audit log must record the successful rebuild
        log_calls = [c for c in mock_sb.log_action.call_args_list
                     if c.kwargs.get("action") == "sheets_rebuild_tasks"]
        assert len(log_calls) == 1
        assert log_calls[0].kwargs["details"]["row_count"] == 1
