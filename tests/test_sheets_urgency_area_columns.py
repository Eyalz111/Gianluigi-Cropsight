"""PR5 — Tasks sheet gains Urgency (K) + Area (L), appended AFTER the col-J UUID.

The load-bearing invariant: the UUID stays in column J (index 9) in BOTH layouts,
so reconcile's Sheet<->DB identity match can't break. Flag off = today's A:J layout.
"""
import importlib

import pytest
from unittest.mock import patch

from config.settings import settings
import services.google_sheets as gs


@pytest.fixture
def restore_gs():
    """Reload google_sheets back to the flag-off layout after a flag-on test."""
    yield
    with patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
        importlib.reload(gs)


class TestLayout:
    def test_flag_off_is_ten_columns(self):
        # default state (flag off)
        assert "urgency" not in gs.TASK_COLUMNS
        assert "area" not in gs.TASK_COLUMNS
        assert gs.TASK_COLUMNS["id"] == "J"
        assert gs.TASK_COL_INDEX["id"] == 9
        assert len(gs.TASK_TRACKER_HEADERS) == 10

    def test_flag_on_appends_after_col_j(self, restore_gs):
        with patch.object(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", True):
            importlib.reload(gs)
            # UUID identity unchanged — the load-bearing invariant
            assert gs.TASK_COLUMNS["id"] == "J"
            assert gs.TASK_COL_INDEX["id"] == 9
            # appended AFTER it
            assert gs.TASK_COLUMNS["urgency"] == "K"
            assert gs.TASK_COLUMNS["area"] == "L"
            assert gs.TASK_COL_INDEX["urgency"] == 10
            assert gs.TASK_COL_INDEX["area"] == 11
            assert gs.TASK_TRACKER_HEADERS[-2:] == ["Urgency", "Area"]
            assert len(gs.TASK_TRACKER_HEADERS) == 12
