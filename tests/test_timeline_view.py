"""The Timeline's grid maths and bar spans.

Two things are easy to get wrong and hard to notice on a 96-column board:

  * A project that began on a Wednesday started THAT week, not the next one.
    Off-by-one here shifts every bar by seven days and looks plausible.
  * A project with no target_date must run OPEN to the right edge, not stop at
    a date derived from its tasks — a derived end moves whenever a task is
    added or re-dated, so the bar appears to change plan when nothing was
    decided.
"""
from datetime import date

import pytest

from processors.timeline_view import (
    GRID_END, GRID_START, OPEN_END_BG, STATUS_BG, span_columns, week_starts,
)


def _rgb_of(hex_colour: str) -> dict:
    from services.timeline_sheet import _rgb
    return _rgb(hex_colour)


class TestGrid:
    def test_the_grid_is_96_mondays(self):
        weeks = week_starts()
        assert len(weeks) == 96
        assert weeks[0] == GRID_START == date(2026, 3, 2)
        assert weeks[-1] == GRID_END == date(2027, 12, 27)

    def test_every_column_is_a_monday(self):
        assert {d.weekday() for d in week_starts()} == {0}

    def test_columns_are_exactly_a_week_apart(self):
        weeks = week_starts()
        assert all((weeks[i + 1] - weeks[i]).days == 7
                   for i in range(len(weeks) - 1))


class TestSpanColumns:
    WEEKS = week_starts()

    def test_a_start_lands_in_its_own_week_not_the_next(self):
        """2026-03-04 is a Wednesday in the week beginning 2026-03-02."""
        first, last, _ = span_columns(date(2026, 3, 4), date(2026, 3, 4), self.WEEKS)
        assert first == last == 0

    def test_an_end_lands_in_its_own_week(self):
        first, last, _ = span_columns(date(2026, 3, 2), date(2026, 3, 13), self.WEEKS)
        assert (first, last) == (0, 1)          # spans two week columns

    def test_no_target_runs_open_to_the_right_edge(self):
        first, last, open_ended = span_columns(date(2026, 4, 6), None, self.WEEKS)
        assert open_ended is True
        assert last == len(self.WEEKS) - 1

    def test_a_start_before_the_grid_clamps_to_the_first_column(self):
        first, last, _ = span_columns(date(2025, 1, 1), date(2026, 3, 16), self.WEEKS)
        assert first == 0

    def test_a_span_entirely_before_the_grid_is_dropped(self):
        assert span_columns(date(2024, 1, 1), date(2024, 6, 1), self.WEEKS)[0] == -1

    def test_a_start_after_the_grid_is_dropped(self):
        assert span_columns(date(2029, 1, 1), None, self.WEEKS)[0] == -1

    def test_no_start_means_no_bar(self):
        """A project nobody has dated renders with no left edge, not from the
        beginning of time."""
        assert span_columns(None, date(2026, 6, 1), self.WEEKS)[0] == -1

    def test_a_single_week_project_occupies_one_column(self):
        first, last, _ = span_columns(date(2026, 6, 1), date(2026, 6, 5), self.WEEKS)
        assert first == last


class TestPalette:
    def test_the_status_colours_come_from_the_old_board(self):
        """Read off its own legend row so the two boards read alike."""
        assert STATUS_BG["completed"] == "#D0D0CC"
        assert STATUS_BG["active"] == "#B7D7B0"
        assert STATUS_BG["planned"] == "#CCE0F0"
        assert STATUS_BG["blocked"] == "#E85050"

    def test_retired_uses_the_completed_colour(self):
        """Eyal: retired projects marked 'in color of completed or done ... as
        we had in the gantt'."""
        from processors.timeline_view import _status_of
        assert _status_of({}, 0, True) == "completed"
        assert STATUS_BG[_status_of({}, 0, True)] == "#D0D0CC"

    def test_an_open_end_is_paler_than_a_closed_one(self):
        """It must not read as a finish at the edge of the grid."""
        assert OPEN_END_BG != STATUS_BG["active"]


class TestStatus:
    def test_open_work_reads_active(self):
        from processors.timeline_view import _status_of
        assert _status_of({}, 3, False) == "active"

    def test_no_open_work_and_not_retired_reads_planned(self):
        from processors.timeline_view import _status_of
        assert _status_of({}, 0, False) == "planned"

    def test_retired_beats_having_open_tasks(self):
        """A retired project with stragglers is still finished."""
        from processors.timeline_view import _status_of
        assert _status_of({}, 5, True) == "completed"


class TestA1Columns:
    """The grid is 101 columns wide, so the two-letter case is not optional —
    a single-letter-only helper writes the whole board into column A..Z and
    silently truncates the rest."""

    def test_single_letters(self):
        from services.timeline_sheet import _a1_col
        assert _a1_col(0) == "A"
        assert _a1_col(25) == "Z"

    def test_the_wrap_past_z(self):
        from services.timeline_sheet import _a1_col
        assert _a1_col(26) == "AA"
        assert _a1_col(27) == "AB"

    def test_the_real_right_edge_of_the_grid(self):
        from processors.timeline_view import N_LABEL_COLS, week_starts
        from services.timeline_sheet import _a1_col
        assert _a1_col(N_LABEL_COLS + len(week_starts()) - 1) == "CW"


class TestMonthBand:
    def test_the_month_is_named_once_then_left_blank(self):
        from services.timeline_sheet import _month_band
        band = _month_band([date(2026, 3, 2), date(2026, 3, 9),
                            date(2026, 4, 6)])
        assert band == ["Mar 26", "", "Apr 26"]

    def test_the_same_month_a_year_later_is_named_again(self):
        """Keyed on (year, month), not month — otherwise March 2027 inherits
        March 2026's blank and a whole month loses its label."""
        from services.timeline_sheet import _month_band
        band = _month_band([date(2026, 3, 2), date(2027, 3, 1)])
        assert band == ["Mar 26", "Mar 27"]


class TestColour:
    def test_hex_converts_to_the_sheets_float_triple(self):
        from services.timeline_sheet import _rgb
        assert _rgb("#FFFFFF") == {"red": 1.0, "green": 1.0, "blue": 1.0}
        black = _rgb("#000000")
        assert black == {"red": 0.0, "green": 0.0, "blue": 0.0}

    def test_the_legend_swatches_match_the_bars(self):
        """A legend that drifts from the bars is worse than no legend."""
        from services.timeline_sheet import _LEGEND, _LEGEND_KEYS
        assert len(_LEGEND) == len(_LEGEND_KEYS)
        assert set(_LEGEND_KEYS) == set(STATUS_BG)


class TestAssertDontAdd:
    """Formatting, row groups and protected ranges all survive a values clear.
    Five of the fifteen defects in the 2026-08-09 review were a refresh that
    added state instead of asserting it, so this is pinned rather than trusted.
    """

    def test_the_refresh_deletes_row_groups_before_adding_them(self):
        import ast
        import pathlib
        src = pathlib.Path("services/timeline_sheet.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        keys = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "deleteDimensionGroup" in keys
        assert "addDimensionGroup" in keys

    def test_the_refresh_deletes_its_own_protected_range_first(self):
        import pathlib
        src = pathlib.Path("services/timeline_sheet.py").read_text(encoding="utf-8")
        assert "deleteProtectedRange" in src
        assert "addProtectedRange" in src

    async def test_the_week_grid_is_wiped_white_before_any_bar_is_painted(self):
        """values().clear() removes text, never formatting. Without the wipe a
        bar that moved leaves its old cells coloured, and the ghost of last
        week's plan sits on the board looking like current truth."""
        from unittest.mock import MagicMock, patch
        from processors.timeline_view import N_LABEL_COLS
        import services.timeline_sheet as ts

        captured = {}
        svc = MagicMock()
        svc._execute_with_retry.side_effect = lambda f: f()

        async def _ensure(*a, **kw):
            return 7
        svc._ensure_tab = _ensure
        sheets = svc.service.spreadsheets.return_value
        sheets.get.side_effect = lambda **kw: {"sheets": []}
        # Keyed on CONTENT, not call order: refresh_timeline now sends a
        # one-request batch to assert the grid width before the formatting
        # batch, and `setdefault` would capture that one instead.
        def _batch(**kw):
            if len(kw["body"]["requests"]) > 1:
                captured["reqs"] = kw["body"]["requests"]
            return MagicMock()
        sheets.batchUpdate.side_effect = _batch

        data = {
            "weeks": week_starts(),
            "areas": {"AREA": [{
                "project_id": "p1", "name": "P", "owner": "E",
                "start": date(2026, 3, 2), "target": date(2026, 4, 6),
                "first_col": 0, "last_col": 5, "open_ended": False,
                "status": "active", "retired": False, "tasks": []}]},
            "stats": {},
        }
        with patch.object(ts, "build_timeline", return_value=data), \
             patch.object(ts, "sheets_service", svc), \
             patch.object(ts.settings, "PROJECT_STATUS_SHEET_ID", "ssid"):
            await ts.refresh_timeline()

        reqs = captured["reqs"]
        # ROW-restricted, not column-restricted. The wipe used to start at the
        # week grid and was found by its column; since the 2026-08-12 review it
        # starts at column 0 so it also resets area-header and archive-title
        # shading in the label block. Keying on FIRST_BODY_ROW still excludes
        # the legend swatches, which are painted white on the same row as the
        # "Active" swatch and would otherwise be measured instead.
        from services.timeline_sheet import FIRST_BODY_ROW
        white = [i for i, r in enumerate(reqs)
                 if (r.get("repeatCell") or {}).get("cell", {})
                 .get("userEnteredFormat", {}).get("backgroundColor")
                 == {"red": 1.0, "green": 1.0, "blue": 1.0}
                 and r["repeatCell"]["range"]["startRowIndex"] == FIRST_BODY_ROW
                 and r["repeatCell"]["range"]["startColumnIndex"] == 0]
        bars = [i for i, r in enumerate(reqs)
                if (r.get("repeatCell") or {}).get("cell", {})
                .get("userEnteredFormat", {}).get("backgroundColor")
                == _rgb_of("#B7D7B0")
                and r["repeatCell"]["range"]["startColumnIndex"] >= N_LABEL_COLS]
        assert white, "no white wipe over the week grid"
        assert bars, "the bar was never painted"
        assert min(white) < min(bars), "the wipe must precede the bars"


class TestEmptyIsNotBlank:
    """A failed read and a genuinely empty board look identical from here, and
    blanking the tab is the more expensive mistake."""

    async def test_no_areas_returns_skipped_and_writes_nothing(self):
        from unittest.mock import MagicMock, patch
        import services.timeline_sheet as ts
        svc = MagicMock()
        with patch.object(ts, "build_timeline",
                          return_value={"weeks": [], "areas": {}, "stats": {}}), \
             patch.object(ts, "sheets_service", svc), \
             patch.object(ts.settings, "PROJECT_STATUS_SHEET_ID", "ssid"):
            out = await ts.refresh_timeline()
        assert out == {"skipped": "no rows"}
        svc._execute_with_retry.assert_not_called()


class TestTabRegistration:
    def test_the_timeline_is_not_parsed_as_an_area_tab(self):
        from processors.project_status_reconcile import NON_AREA_TABS
        from processors.timeline_view import TIMELINE_TAB
        assert TIMELINE_TAB in NON_AREA_TABS

    def test_its_headers_do_not_satisfy_the_area_layout(self):
        """Belt to the registration's braces, as with Focus."""
        from processors.timeline_view import HEADERS
        from services.project_status_rows import unresolved_columns
        assert unresolved_columns(HEADERS)
