"""The Tasks tab as a read-only mirror.

From 2026-08-08 the Project Status sheet is the only editable surface for
tasks. Two writers on the same rows produced every cross-surface defect this
week — the rename-revert loop, the per-task manual_set_at recency bug, and
three labels left permanently divergent because this tab pulled its own stale
cell over a value Project Status had just written:

    DB           label='Italy'              set 17:48
    PS snapshot  label='Salesman In Italy'  at 18:50
    Tasks snap   label=None                 at 18:49

Retiring the second writer removes the whole class rather than patching the
merge rules.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from processors import sheets_sync


def _sheet_task(**kw):
    row = {"id": "t1", "row_number": 2, "task": "Ship the API", "label": "Italy",
           "status": "pending", "deadline": "", "priority": "M", "owner": "Eyal Zror"}
    row.update(kw)
    return row


def _db_task(**kw):
    row = {"id": "t1", "title": "Ship the API", "label": "Italy",
           "status": "pending", "deadline": None, "priority": "M",
           "assignee": "Eyal Zror"}
    row.update(kw)
    return row


async def _run(sheet_rows, db_rows, snapshots, read_only):
    sb = MagicMock()
    sb.get_tasks.return_value = db_rows
    sb.get_sheet_snapshots.return_value = snapshots
    sb.get_areas.return_value = []
    sb.list_team_members.return_value = []
    sb.get_canonical_projects.return_value = []
    sb.resolve_assignee.side_effect = lambda v, roster=None: v
    svc = MagicMock()
    svc.get_all_tasks = AsyncMock(return_value=sheet_rows)
    svc._update_cell = AsyncMock()
    svc.batch_update_cells = AsyncMock()

    with patch.object(sheets_sync, "supabase_client", sb), \
         patch("services.google_sheets.sheets_service", svc), \
         patch.object(sheets_sync.settings, "RECONCILE_ENABLED", True), \
         patch.object(sheets_sync.settings, "TASKS_TAB_READ_ONLY", read_only), \
         patch.object(sheets_sync.settings, "TASK_TRACKER_SHEET_ID", "sid"):
        summary = await sheets_sync.reconcile_tasks(dry_run=True)
    return summary, sb


class TestMirrorNeverReadsBack:
    @pytest.mark.asyncio
    async def test_an_edited_cell_is_not_pulled(self):
        """The exact shape that corrupted the labels: this tab holds a value
        Project Status has already moved past."""
        summary, _ = await _run(
            [_sheet_task(owner="Someone Else")],
            [_db_task(assignee="Eyal Zror")],
            {"t1": {"assignee": "Eyal Zror", "status": "pending"}},
            read_only=True,
        )
        assert summary["pulled"] == 0

    @pytest.mark.asyncio
    async def test_edited_text_is_not_pulled_either(self):
        summary, _ = await _run(
            [_sheet_task(task="Completely different text")],
            [_db_task()],
            {"t1": {"title": "Ship the API", "status": "pending"}},
            read_only=True,
        )
        assert summary["pulled"] == 0

    @pytest.mark.asyncio
    async def test_a_hand_typed_row_is_reported_not_created(self):
        """Silently ignoring it would be worse than saying so."""
        summary, _ = await _run(
            [_sheet_task(), {"row_number": 9, "id": "", "task": "New thing"}],
            [_db_task()],
            {"t1": {"title": "Ship the API", "status": "pending"}},
            read_only=True,
        )
        assert summary["created"] == 0
        assert summary.get("ignored_creates") == 1

    @pytest.mark.asyncio
    async def test_the_db_is_still_mirrored_out(self):
        """It stays useful as the flat sortable view."""
        summary, _ = await _run(
            [_sheet_task(owner="Stale Name")],
            [_db_task(assignee="Roye Tadmor")],
            {"t1": {"assignee": "Stale Name", "status": "pending"}},
            read_only=True,
        )
        assert summary["pushed"] >= 1


class TestWritableModeUnchanged:
    @pytest.mark.asyncio
    async def test_an_edit_is_still_pulled_when_not_read_only(self):
        summary, _ = await _run(
            [_sheet_task(owner="Roye Tadmor")],
            [_db_task(assignee="Eyal Zror")],
            {"t1": {"assignee": "Eyal Zror", "status": "pending"}},
            read_only=False,
        )
        assert summary["pulled"] >= 1

    @pytest.mark.asyncio
    async def test_a_hand_typed_row_is_still_created(self):
        summary, _ = await _run(
            [_sheet_task(), {"row_number": 9, "id": "", "task": "New thing"}],
            [_db_task()],
            {"t1": {"title": "Ship the API", "status": "pending"}},
            read_only=False,
        )
        assert summary["created"] == 1
