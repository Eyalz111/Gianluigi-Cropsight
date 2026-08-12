"""Adding a recurring meeting by hand — the #4 path.

A recurring meeting is a STANDING commitment: the weekly R&D, the Monday
roadmap. It is a status rather than a priority because "how much does this
matter" and "does this need booking at all" are different questions, and it is
deliberately NOT terminal — it is the most live state there is.

The bug these pin: the Meetings tab has a Priority column, but a value typed on
a NEW row was never forwarded to the insert, so the database default 'M'
overwrote it within 30 minutes — in the cell she had just typed. That is defect
#3 of the 2026-08-09 review, fixed for tasks and missed here.
"""
from types import SimpleNamespace

import pytest

from services.google_sheets import (
    MEETING_STATUSES, MEETING_TERMINAL_STATUSES, MEETING_PRIORITIES,
)
from services.supabase_client import SupabaseClient


def _client_capturing(captured):
    class _Tbl:
        def insert(self, data):
            captured.update(data)
            return self

        def execute(self):
            return SimpleNamespace(data=[{"id": "fu-1", **captured}])

    sc = SupabaseClient.__new__(SupabaseClient)
    sc._client = SimpleNamespace(table=lambda _n: _Tbl())
    sc.resolve_assignee = lambda v: v or ""
    sc.resolve_label = lambda v: v or ""
    return sc


class TestRecurringIsAFirstClassStatus:
    def test_recurring_is_offered_on_the_sheet(self):
        assert "recurring" in MEETING_STATUSES

    def test_recurring_is_never_terminal(self):
        """A standing meeting must never be archived off the pool."""
        assert "recurring" not in MEETING_TERMINAL_STATUSES

    def test_a_typed_recurring_row_keeps_that_status(self):
        captured = {}
        _client_capturing(captured).create_follow_up_meeting_manual(
            title="Weekly R&D", status="recurring")
        assert captured["status"] == "recurring"
        assert captured["approval_status"] == "approved"   # a human typed it

    def test_status_defaults_only_when_blank(self):
        captured = {}
        _client_capturing(captured).create_follow_up_meeting_manual(title="X", status="")
        assert captured["status"] == "to_schedule"


class TestPriorityOnANewRowSurvives:
    def test_a_typed_priority_reaches_the_insert(self):
        captured = {}
        _client_capturing(captured).create_follow_up_meeting_manual(
            title="Weekly R&D", status="recurring", priority="U")
        assert captured["priority"] == "U"

    def test_a_typed_priority_is_marked_manual(self):
        """Rule 2 must know a human chose it, or inference walks it back."""
        captured = {}
        _client_capturing(captured).create_follow_up_meeting_manual(
            title="Weekly R&D", priority="U")
        assert captured["manual_priority"] is True

    def test_a_blank_priority_is_left_alone(self):
        """An untouched default is NOT a decision — claiming it as one is what
        stamped 'M' across 122 meetings on the morning of 2026-08-09."""
        captured = {}
        _client_capturing(captured).create_follow_up_meeting_manual(title="X")
        assert "priority" not in captured
        assert "manual_priority" not in captured

    def test_the_reconcile_forwards_it(self):
        """The call site, not just the function — the parameter existing is no
        use if nobody passes it. Checked on the AST so a comment cannot pass."""
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path("processors/sheets_sync.py").read_text(
            encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            if getattr(call.func, "attr", "") != "create_follow_up_meeting_manual":
                continue
            assert "priority" in {kw.arg for kw in call.keywords}, (
                "the meetings reconcile creates a row without forwarding priority")
            return
        pytest.fail("create_follow_up_meeting_manual is never called by the reconcile")


class TestSheetVocabulary:
    def test_the_sheet_spells_urgent_but_the_db_stores_a_letter(self):
        """The pairing that produced an invalid-entry triangle on the most
        important meeting in the pool (defect #6)."""
        from services.google_sheets import _MEETING_PRIORITY_TO_SHEET
        assert "Urgent" in MEETING_PRIORITIES
        assert _MEETING_PRIORITY_TO_SHEET["U"] == "Urgent"
