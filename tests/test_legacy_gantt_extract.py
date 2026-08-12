"""Recovering the old Gantt's timelines as real dates.

The board is a weekly-column grid: a bar is the same text repeated across
consecutive week cells, and row 5 carries "W9 02/03" headers. Two things are
easy to get silently wrong, and both would corrupt the archive rather than
fail loudly:

  * The headers give day and month but never a YEAR. Weeks run left to right,
    so the year rolls over exactly where the month goes backwards. Miss it and
    every column after New Year is dated a year early — a plausible-looking
    archive that is wrong by 52 weeks.
  * A bar is "the same text", but the board appends per-week notes to otherwise
    identical labels. Comparing labels in full shatters one 17-week bar into 17
    one-week fragments.
"""
from datetime import date

import pytest

from scripts.extract_legacy_gantt import recover_bars, week_columns


def _grid(headers: list[str], body: list[list[str]]) -> list:
    """A board: 4 filler rows, the header row, then body rows."""
    grid = [[] for _ in range(4)]
    grid.append(["Section", "Sub-category", "Own", "Due"] + headers)
    grid.extend(body)
    return grid


class TestWeekColumns:
    def test_reads_real_dates_from_the_headers(self):
        weeks = week_columns(_grid(["W9\n02/03", "W10\n09/03"], []), 2026)
        assert weeks == {4: date(2026, 3, 2), 5: date(2026, 3, 9)}

    def test_the_year_rolls_over_when_the_month_goes_backwards(self):
        """December -> January is the whole reason this is a named function."""
        weeks = week_columns(
            _grid(["W52\n21/12", "W53\n28/12", "W1\n04/01", "W2\n11/01"], []), 2026)
        assert list(weeks.values()) == [
            date(2026, 12, 21), date(2026, 12, 28),
            date(2027, 1, 4), date(2027, 1, 11),
        ]

    def test_it_does_not_roll_over_within_a_year(self):
        weeks = week_columns(_grid(["W9\n02/03", "W30\n27/07", "W48\n30/11"], []), 2026)
        assert {d.year for d in weeks.values()} == {2026}

    def test_an_impossible_date_is_skipped_not_crashed(self):
        weeks = week_columns(_grid(["W9\n02/03", "W10\n30/02", "W11\n16/03"], []), 2026)
        assert list(weeks.values()) == [date(2026, 3, 2), date(2026, 3, 16)]

    def test_columns_without_a_date_are_ignored(self):
        weeks = week_columns(_grid(["", "notes", "W9\n02/03"], []), 2026)
        assert weeks == {6: date(2026, 3, 2)}


class TestRecoverBars:
    HEADERS = ["W9\n02/03", "W10\n09/03", "W11\n16/03", "W12\n23/03"]

    def _bars(self, body):
        grid = _grid(self.HEADERS, body)
        return recover_bars(grid, week_columns(grid, 2026), "2026-2027")

    def test_consecutive_cells_become_one_dated_bar(self):
        bars = self._bars([
            ["PRODUCT & TECHNOLOGY", "", "", ""],
            ["", "Execution #1", "", "", "MVP Work", "MVP Work", "MVP Work"],
        ])
        assert len(bars) == 1
        b = bars[0]
        assert (b["start_date"], b["end_date"]) == ("2026-03-02", "2026-03-16")
        assert b["weeks"] == 3
        assert b["section"] == "PRODUCT & TECHNOLOGY"
        assert b["lane"] == "Execution #1"

    def test_a_gap_splits_one_lane_into_two_bars(self):
        bars = self._bars([
            ["SALES", "", "", ""],
            ["", "Execution #1", "", "", "Italy", "", "Italy", "Italy"],
        ])
        assert [(b["start_date"], b["weeks"]) for b in bars] == [
            ("2026-03-02", 1), ("2026-03-16", 2)]

    def test_a_per_week_note_does_not_shatter_the_bar(self):
        """The board appends notes to an otherwise identical label."""
        bars = self._bars([
            ["PRODUCT & TECHNOLOGY", "", "", ""],
            ["", "Execution #1", "", "",
             "MVP Work: agreed SOW",
             "MVP Work: agreed SOW - week 2 note",
             "MVP Work: agreed SOW - and another"],
        ])
        assert len(bars) == 1
        assert bars[0]["weeks"] == 3

    def test_a_genuinely_different_label_starts_a_new_bar(self):
        bars = self._bars([
            ["SALES", "", "", ""],
            ["", "Execution #1", "", "", "BP development", "Aquiring MVP Clients"],
        ])
        assert len(bars) == 2

    def test_the_owner_prefix_is_captured_raw_not_resolved(self):
        """'[E]' is almost certainly Eyal, but the board never says so — and a
        guess does not belong in an archive."""
        bars = self._bars([
            ["SALES", "", "", ""],
            ["", "Execution #1", "", "", "[E/P] BP development"],
        ])
        assert bars[0]["owner_tag"] == "[E/P]"
        assert bars[0]["label"] == "[E/P] BP development"   # label keeps the tag

    def test_no_owner_prefix_is_none_not_empty_string(self):
        bars = self._bars([["SALES", "", "", ""],
                           ["", "Execution #1", "", "", "BP development"]])
        assert bars[0]["owner_tag"] is None

    def test_section_carries_down_to_following_lanes(self):
        bars = self._bars([
            ["PRODUCT & TECHNOLOGY", "", "", ""],
            ["", "Execution #1", "", "", "one"],
            ["", "Execution #2", "", "", "two"],
            ["SALES", "", "", ""],
            ["", "Execution #1", "", "", "three"],
        ])
        assert [b["section"] for b in bars] == [
            "PRODUCT & TECHNOLOGY", "PRODUCT & TECHNOLOGY", "SALES"]

    def test_row_number_is_1_indexed_for_spot_checking(self):
        """The exit criterion is checking bars against the sheet by eye."""
        bars = self._bars([
            ["PRODUCT & TECHNOLOGY", "", "", ""],
            ["", "Execution #1", "", "", "MVP Work"],
        ])
        assert bars[0]["row_number"] == 7      # 4 filler + header + 2 body

    def test_an_empty_lane_produces_nothing(self):
        assert self._bars([["SALES", "", "", ""],
                           ["", "Execution #1", "", ""]]) == []

    def test_numeric_helper_rows_are_kept_for_the_reader_to_filter(self):
        """The archive is dumb by design — dropping rows here is interpretation."""
        bars = self._bars([
            ["MANAGEMENT", "", "", ""],
            ["", "Meeting Count (helper)", "", "", "3", "3"],
        ])
        assert len(bars) == 1
        assert bars[0]["lane"] == "Meeting Count (helper)"
