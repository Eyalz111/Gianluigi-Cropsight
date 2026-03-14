"""
Week calculation utilities for Gantt chart column mapping.

Converts between week numbers (W9, W12...) and spreadsheet column letters
(E, H...). The Gantt has ~96 week columns starting at column E, reaching
past column Z into AA/AB/etc.

Internal helpers used only by gantt_manager.py and gantt_guard.py.
"""

from datetime import date, timedelta


def column_to_index(col: str) -> int:
    """Convert column letter(s) to 0-based index. A=0, B=1, ..., Z=25, AA=26."""
    result = 0
    for char in col.upper():
        result = result * 26 + (ord(char) - ord('A') + 1)
    return result - 1


def index_to_column(idx: int) -> str:
    """Convert 0-based index to column letter(s). 0=A, 25=Z, 26=AA."""
    result = ""
    idx += 1  # 1-based
    while idx > 0:
        idx, remainder = divmod(idx - 1, 26)
        result = chr(ord('A') + remainder) + result
    return result


def week_to_column(week_number: int, week_offset: int = 9, first_week_col: str = "E") -> str:
    """
    Convert a week number to a spreadsheet column letter.

    Args:
        week_number: The week number (e.g., 9, 12, 52).
        week_offset: The week number of the first week column (default 9).
        first_week_col: The column letter of the first week (default "E").

    Returns:
        Column letter string (e.g., "E", "H", "AA").

    Raises:
        ValueError: If week_number is before the offset.
    """
    if week_number < week_offset:
        raise ValueError(f"Week {week_number} is before offset {week_offset}")
    delta = week_number - week_offset
    base_index = column_to_index(first_week_col)
    return index_to_column(base_index + delta)


def column_to_week(column: str, week_offset: int = 9, first_week_col: str = "E") -> int:
    """
    Convert a spreadsheet column letter to a week number.

    Args:
        column: Column letter string (e.g., "E", "H", "AA").
        week_offset: The week number of the first week column (default 9).
        first_week_col: The column letter of the first week (default "E").

    Returns:
        Week number (int).
    """
    col_index = column_to_index(column)
    base_index = column_to_index(first_week_col)
    return week_offset + (col_index - base_index)


def current_week_number(start_date: date | None = None, week_offset: int = 9) -> int:
    """
    Calculate the current ISO week number adjusted by the Gantt's week offset.

    If start_date is provided, calculates weeks elapsed since that date.
    Otherwise uses ISO week numbering.

    Args:
        start_date: The date corresponding to week_offset (e.g., the Monday of W9).
        week_offset: The week number of that start date.

    Returns:
        Current week number.
    """
    today = date.today()
    if start_date:
        days_elapsed = (today - start_date).days
        weeks_elapsed = days_elapsed // 7
        return week_offset + weeks_elapsed
    return today.isocalendar()[1]
