"""PR10 — add_task writes the col-J UUID (+ K when the urgency column is enabled).

The load-bearing fix: a row appended without its DB UUID is invisible to the
v3 reconcile's identity match, so a write-mode reconcile re-creates it as a
DUPLICATE task. add_task now writes the UUID in column J (the 10-column base
layout) and urgency in K when that column exists. There is no Area column —
the Gantt-area taxonomy lives in Category (2026-06 realignment).
"""
from unittest.mock import patch, AsyncMock

import pytest

from config.settings import settings
import services.google_sheets as gs


async def _capture_add_task(**over):
    """Call add_task with a mocked _append_row and return the values list written."""
    mock_append = AsyncMock(return_value=True)
    kwargs = dict(
        task="Ship pilot", assignee="Roye", source_meeting="Sync",
        deadline="2026-06-20", status="pending", priority="H",
        created_date="2026-06-10", category="PRODUCT & TECHNOLOGY", label="Lbl",
        task_id="uuid-1", urgency="H",
    )
    kwargs.update(over)
    with patch.object(gs.sheets_service, "_append_row", mock_append), \
         patch.object(settings, "TASK_TRACKER_SHEET_ID", "sheet123"):
        await gs.sheets_service.add_task(**kwargs)
    return mock_append.call_args.kwargs["values"]


class TestAddTaskWritesUuid:
    async def test_base_layout_writes_uuid_in_col_j(self):
        # default TASK_COLUMNS has no urgency → 10 values, J carries the UUID
        vals = await _capture_add_task()
        assert len(vals) == 10
        assert vals[9] == "uuid-1"   # column J = task UUID (the fix)

    async def test_empty_id_still_ten_columns(self):
        # a caller without an id appends an empty J (matches today's blank cell)
        vals = await _capture_add_task(task_id="")
        assert len(vals) == 10
        assert vals[9] == ""

    async def test_appends_urgency_when_column_enabled(self):
        col_on = dict(gs.TASK_COLUMNS)
        col_on["urgency"] = "K"
        with patch.object(gs, "TASK_COLUMNS", col_on):
            vals = await _capture_add_task()
        assert len(vals) == 11
        assert vals[9] == "uuid-1"   # J unchanged
        assert vals[10] == "H"       # K = urgency (no L/area cell post-realignment)
