"""Lateness is computed from the deadline, never read from `status`.

`overdue` was a member of TaskStatus, competing with pending/in_progress for
the same field. "How far along is this" and "is it late" are independent: a
task can be in progress AND late, which that vocabulary cannot express, so
stamping 'overdue' destroyed the progress state.

It had also stopped being maintained. The two schedulers that used to set it
(TASK_REMINDER_ENABLED, ALERT_SCHEDULER_ENABLED) are both off, so on
2026-08-11 eighteen open tasks were past their deadline and five carried the
status — every reader trusting the field saw a quarter of its input.
"""
import ast
import pathlib
from datetime import date

import pytest

from models.schemas import (
    CLOSED_TASK_STATUSES, OPEN_TASK_STATUSES, TaskStatus, is_overdue,
)

TODAY = date(2026, 8, 11)


class TestIsOverdue:
    def test_a_past_deadline_on_an_open_task_is_late(self):
        for st in ("pending", "in_progress"):
            assert is_overdue({"status": st, "deadline": "2026-08-04"}, TODAY)

    def test_progress_state_is_irrelevant_to_lateness(self):
        """The whole point: in_progress AND late is now sayable."""
        t = {"status": "in_progress", "deadline": "2026-06-30"}
        assert is_overdue(t, TODAY)
        assert t["status"] == "in_progress"      # progress survives the question

    def test_a_closed_task_is_never_late(self):
        for st in sorted(CLOSED_TASK_STATUSES):
            assert not is_overdue({"status": st, "deadline": "2026-01-01"}, TODAY)

    def test_today_is_not_late(self):
        assert not is_overdue({"status": "pending", "deadline": "2026-08-11"}, TODAY)

    def test_future_is_not_late(self):
        assert not is_overdue({"status": "pending", "deadline": "2026-12-01"}, TODAY)

    def test_no_deadline_is_not_late(self):
        """Unscheduled is a different problem, surfaced separately by Focus."""
        assert not is_overdue({"status": "pending"}, TODAY)
        assert not is_overdue({"status": "pending", "deadline": ""}, TODAY)
        assert not is_overdue({"status": "pending", "deadline": None}, TODAY)

    def test_an_unparseable_deadline_is_not_late(self):
        """A data problem, not a late task — calling it late puts it in a list
        nobody can action."""
        assert not is_overdue({"status": "pending", "deadline": "next Tuesday"}, TODAY)

    def test_a_legacy_stamped_row_still_reads_as_late_when_it_is(self):
        assert is_overdue({"status": "overdue", "deadline": "2026-08-01"}, TODAY)

    def test_a_stamped_row_with_a_future_deadline_is_not_late(self):
        """The field lies; the date does not."""
        assert not is_overdue({"status": "overdue", "deadline": "2026-12-01"}, TODAY)


class TestStatusSets:
    def test_overdue_still_counts_as_open(self):
        """Rows carry it historically — dropping it from OPEN would make them
        vanish from every working view at once."""
        assert "overdue" in OPEN_TASK_STATUSES

    def test_open_and_closed_do_not_overlap(self):
        assert not (OPEN_TASK_STATUSES & CLOSED_TASK_STATUSES)

    def test_every_enum_member_is_classified(self):
        for m in TaskStatus:
            assert m.value in OPEN_TASK_STATUSES | CLOSED_TASK_STATUSES, m.value


class TestReadersUseTheHelper:
    """These parse the AST rather than grep the text — the first version of
    this file matched its own explanatory comments and failed on prose."""

    @staticmethod
    def _calls(path):
        import ast
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def test_no_reader_asks_the_status_field_for_lateness(self):
        """`get_tasks(status="overdue")` is the call that saw 5 of 18."""
        import pathlib as _pl
        offenders = []
        for p in _pl.Path("processors").rglob("*.py"):
            for call in self._calls(p):
                fn = getattr(call.func, "attr", getattr(call.func, "id", ""))
                if fn != "get_tasks":
                    continue
                for kw in call.keywords:
                    if (kw.arg == "status"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value == "overdue"):
                        offenders.append(f"{p}:{call.lineno}")
        assert not offenders, f"still trusting the status field: {offenders}"

    def test_meeting_prep_does_not_compare_against_a_nonexistent_status(self):
        """It filtered on != "completed"; the enum says "done", so finished
        tasks with a past deadline were read out as overdue."""
        import ast as _ast
        tree = _ast.parse(
            pathlib.Path("processors/meeting_prep.py").read_text(encoding="utf-8"))
        literals = {n.value for n in _ast.walk(tree)
                    if isinstance(n, _ast.Constant) and isinstance(n.value, str)}
        valid = {m.value for m in TaskStatus}
        assert "completed" not in literals, (
            f"'completed' is not a TaskStatus; valid values are {sorted(valid)}")
