"""Tests for gantt_weeks module."""

import pytest
from datetime import date, timedelta
from services.gantt_weeks import (
    column_to_index, index_to_column, week_to_column,
    column_to_week, current_week_number,
)


# --- column_to_index ---

def test_column_to_index_a():
    assert column_to_index("A") == 0


def test_column_to_index_b():
    assert column_to_index("B") == 1


def test_column_to_index_z():
    assert column_to_index("Z") == 25


def test_column_to_index_aa():
    assert column_to_index("AA") == 26


def test_column_to_index_az():
    assert column_to_index("AZ") == 51


def test_column_to_index_ba():
    assert column_to_index("BA") == 52


def test_column_to_index_case_insensitive():
    assert column_to_index("aa") == column_to_index("AA")


# --- index_to_column ---

def test_index_to_column_0():
    assert index_to_column(0) == "A"


def test_index_to_column_1():
    assert index_to_column(1) == "B"


def test_index_to_column_25():
    assert index_to_column(25) == "Z"


def test_index_to_column_26():
    assert index_to_column(26) == "AA"


def test_index_to_column_51():
    assert index_to_column(51) == "AZ"


def test_index_to_column_52():
    assert index_to_column(52) == "BA"


# --- round-trip ---

def test_round_trip_single_letters():
    for col in ["A", "E", "M", "Z"]:
        assert index_to_column(column_to_index(col)) == col


def test_round_trip_double_letters():
    for col in ["AA", "AZ", "BA", "BZ", "ZZ"]:
        assert index_to_column(column_to_index(col)) == col


# --- week_to_column ---

def test_week_to_column_first_week():
    # W9 is the first week column, maps to "E"
    assert week_to_column(9) == "E"


def test_week_to_column_next_week():
    assert week_to_column(10) == "F"


def test_week_to_column_past_z():
    # W9→E (index 4), W35 is 26 weeks after W9 → index 4+26=30 → "AE"
    assert week_to_column(35) == "AE"


def test_week_to_column_at_z_boundary():
    # W9→E (index 4), to reach Z (index 25) we need W9 + 21 = W30
    assert week_to_column(30) == "Z"


def test_week_to_column_into_aa():
    # W31 → one past Z → "AA"
    assert week_to_column(31) == "AA"


def test_week_to_column_raises_for_week_before_offset():
    with pytest.raises(ValueError):
        week_to_column(8)


def test_week_to_column_raises_for_week_zero():
    with pytest.raises(ValueError):
        week_to_column(0)


def test_week_to_column_custom_offset_and_col():
    # offset=1, first_week_col="A" → W1→"A", W2→"B"
    assert week_to_column(1, week_offset=1, first_week_col="A") == "A"
    assert week_to_column(2, week_offset=1, first_week_col="A") == "B"


# --- column_to_week ---

def test_column_to_week_first_col():
    assert column_to_week("E") == 9


def test_column_to_week_next_col():
    assert column_to_week("F") == 10


def test_column_to_week_z():
    assert column_to_week("Z") == 30


def test_column_to_week_aa():
    assert column_to_week("AA") == 31


def test_column_to_week_custom_offset():
    assert column_to_week("A", week_offset=1, first_week_col="A") == 1
    assert column_to_week("B", week_offset=1, first_week_col="A") == 2


# --- week_to_column / column_to_week round-trip ---

def test_week_column_round_trip():
    for week in [9, 10, 20, 30, 31, 40, 52]:
        col = week_to_column(week)
        assert column_to_week(col) == week


# --- current_week_number ---

def test_current_week_number_with_start_date_same_day():
    today = date.today()
    result = current_week_number(start_date=today, week_offset=9)
    assert result == 9


def test_current_week_number_with_start_date_one_week_ago():
    one_week_ago = date.today() - timedelta(weeks=1)
    result = current_week_number(start_date=one_week_ago, week_offset=9)
    assert result == 10


def test_current_week_number_with_start_date_three_weeks_ago():
    three_weeks_ago = date.today() - timedelta(weeks=3)
    result = current_week_number(start_date=three_weeks_ago, week_offset=9)
    assert result == 12


def test_current_week_number_without_start_date_uses_iso_week():
    today = date.today()
    expected_iso_week = today.isocalendar()[1]
    result = current_week_number()
    assert result == expected_iso_week


def test_current_week_number_custom_offset_with_start_date():
    start = date.today() - timedelta(weeks=5)
    result = current_week_number(start_date=start, week_offset=1)
    assert result == 6
