"""Year-less and relative date forms.

The Project Status how-to promises "12/8 · 12 Aug · next Tuesday · 2026-08-12
— all are understood". Before 2026-08-07 only the last one was: the parser's
underspecified-input guard, which correctly rejects "June" and "2026", was also
rejecting day+month forms that name a specific day. The document was making a
promise the code did not keep.
"""

from datetime import date

import pytest

from core.dates import parse_human_date

MON = date(2026, 8, 10)          # a Monday


class TestYearless:
    def test_numeric_day_month(self):
        assert parse_human_date("12/8", today=MON) == "2026-08-12"

    def test_dotted_day_month(self):
        assert parse_human_date("12.8", today=MON) == "2026-08-12"

    def test_day_then_month_name(self):
        assert parse_human_date("12 Aug", today=MON) == "2026-08-12"
        assert parse_human_date("12 August", today=MON) == "2026-08-12"

    def test_month_name_then_day(self):
        assert parse_human_date("Aug 12", today=MON) == "2026-08-12"

    def test_a_date_already_well_past_rolls_to_next_year(self):
        """"12/1" written in August means the coming January."""
        assert parse_human_date("12/1", today=MON) == "2027-01-12"

    def test_a_recently_missed_deadline_stays_in_this_year(self):
        """Two weeks late is a missed deadline, not next year's."""
        assert parse_human_date("1/8", today=MON) == "2026-08-01"

    def test_an_impossible_day_month_is_rejected(self):
        assert parse_human_date("31/2", today=MON) is None


class TestRelative:
    def test_today_and_tomorrow(self):
        assert parse_human_date("today", today=MON) == "2026-08-10"
        assert parse_human_date("tomorrow", today=MON) == "2026-08-11"

    def test_next_weekday(self):
        assert parse_human_date("next Tuesday", today=MON) == "2026-08-11"
        assert parse_human_date("next Friday", today=MON) == "2026-08-14"

    def test_a_bare_weekday_means_the_coming_one(self):
        assert parse_human_date("Friday", today=MON) == "2026-08-14"

    def test_the_same_weekday_means_next_week(self):
        """Nobody sets a deadline for the meeting they are sitting in."""
        assert parse_human_date("Monday", today=MON) == "2026-08-17"

    def test_in_n_units(self):
        assert parse_human_date("in 3 days", today=MON) == "2026-08-13"
        assert parse_human_date("in 2 weeks", today=MON) == "2026-08-24"

    def test_abbreviated_weekday(self):
        assert parse_human_date("next Tue", today=MON) == "2026-08-11"


class TestTheGuardStillHolds:
    """Underspecified input that names no DAY must stay rejected — a task with
    an invented deadline is worse than one with none."""

    @pytest.mark.parametrize("value", ["June", "2026", "30", "", "   ",
                                       "whenever", "dont mind", "next month-ish"])
    def test_rejected(self, value):
        assert parse_human_date(value, today=MON) is None


class TestExistingFormsUnchanged:
    @pytest.mark.parametrize("value,expected", [
        ("2026-08-12", "2026-08-12"),
        ("12/8/2026", "2026-08-12"),
        ("20.6.26", "2026-06-20"),
        ("20-6-26", "2026-06-20"),
        ("2026-08-12T10:00:00Z", "2026-08-12"),
        ("20 June 2026", "2026-06-20"),
    ])
    def test_unchanged(self, value, expected):
        assert parse_human_date(value, today=MON) == expected

    def test_date_objects_still_pass_through(self):
        assert parse_human_date(date(2026, 8, 12)) == "2026-08-12"
