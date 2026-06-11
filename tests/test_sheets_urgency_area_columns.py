"""PR5 — Tasks sheet gains Urgency (K), appended AFTER the col-J UUID.

Since the 2026-06 category realignment there is NO separate Area column —
the Gantt-area taxonomy lives in the existing Category column (G).

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
            # appended AFTER it: urgency only — no Area column post-realignment
            assert gs.TASK_COLUMNS["urgency"] == "K"
            assert "area" not in gs.TASK_COLUMNS
            assert gs.TASK_COL_INDEX["urgency"] == 10
            assert gs.TASK_TRACKER_HEADERS[-1] == "Urgency"
            assert len(gs.TASK_TRACKER_HEADERS) == 11
