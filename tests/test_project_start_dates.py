"""A project's start date: derived once, then frozen, or set by a human.

Every date in this database is an END. A Gantt bar needs a left edge, and the
obvious source is wrong: `canonical_projects.created_at` clusters 23 projects
onto 3 days — 15 of them on the day the Projects table was built. The earliest
TASK spreads across 14 days and reads plausibly.

The freezing is the subtle part. "Earliest task, evaluated on every render"
moves backwards the moment an older task is attached, so the bar silently
redraws and reads as a plan change nobody made.

And the old Gantt is shown but never recommended: matching its labels to
projects by name is confidently wrong — "Corporate" matches a recurring admin
line dated 2027-05-31, and four different projects match one "MVP Product
Delivery" bar.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from processors.project_start_dates import (
    apply_project_start, earliest_task_start, gantt_candidate,
)


class TestEarliestTaskStart:
    TASKS = [
        {"project_id": "p1", "created_at": "2026-04-09T10:00:00Z"},
        {"project_id": "p1", "created_at": "2026-03-26T08:00:00Z"},
        {"project_id": "p1", "created_at": "2026-07-22T09:00:00Z"},
        {"project_id": "p2", "created_at": "2026-06-13T09:00:00Z"},
    ]

    def test_picks_the_earliest(self):
        assert earliest_task_start("p1", self.TASKS) == date(2026, 3, 26)

    def test_scopes_to_the_project(self):
        assert earliest_task_start("p2", self.TASKS) == date(2026, 6, 13)

    def test_a_project_with_no_tasks_has_no_start(self):
        assert earliest_task_start("p9", self.TASKS) is None

    def test_unparseable_timestamps_are_skipped_not_crashed(self):
        tasks = [{"project_id": "p1", "created_at": "sometime last spring"},
                 {"project_id": "p1", "created_at": "2026-05-01T00:00:00Z"}]
        assert earliest_task_start("p1", tasks) == date(2026, 5, 1)

    def test_pending_tasks_count(self):
        """A task awaiting approval still marks when the work surfaced.
        Filtering to approved-only would push the start later for any project
        whose first extraction is still in the queue."""
        tasks = [{"project_id": "p1", "created_at": "2026-01-05T00:00:00Z",
                  "approval_status": "pending"}]
        assert earliest_task_start("p1", tasks) == date(2026, 1, 5)


class TestGanttCandidate:
    BARS = [
        {"section": "LEGAL, CORPORATE & FINANCE", "lane": "Execution #1",
         "label": "[E] Legal entity Establishment", "start_date": "2026-03-02"},
        {"section": "LEGAL, CORPORATE & FINANCE", "lane": "Finance & Admin",
         "label": "Monthly close - send docs to Shimony", "start_date": "2027-05-31"},
        {"section": "MANAGEMENT", "lane": "Meeting Count (helper)",
         "label": "4", "start_date": "2026-03-02"},
    ]

    def test_matches_within_the_area(self):
        got = gantt_candidate("Legal", "LEGAL, CORPORATE & FINANCE", self.BARS)
        assert got["date"] == "2026-03-02"
        assert "Legal entity" in got["label"]

    def test_returns_none_when_nothing_overlaps(self):
        assert gantt_candidate("Cloud Infrastructure",
                               "PRODUCT & TECHNOLOGY", self.BARS) is None

    def test_noise_lanes_are_never_candidates(self):
        """Meeting-count cells hold numbers, not work."""
        got = gantt_candidate("Meeting", "MANAGEMENT", self.BARS)
        assert got is None or "Meeting Count" not in (got.get("lane") or "")

    def test_a_bar_from_another_area_is_not_borrowed(self):
        got = gantt_candidate("Legal", "PRODUCT & TECHNOLOGY", self.BARS)
        assert got is None

    def test_stopwords_do_not_create_a_match(self):
        """'Others - Team & HR' must not match on 'others'."""
        bars = [{"section": "TEAM & HUMAN RESOURCES", "lane": "Execution #1",
                 "label": "Others stuff entirely", "start_date": "2026-01-01"}]
        assert gantt_candidate("Others - Team & HR",
                               "TEAM & HUMAN RESOURCES", bars) is None


class TestApply:
    def _client(self, captured):
        class _Tbl:
            def update(self, data):
                captured.update(data)
                return self

            def eq(self, *a):
                return self

            def execute(self):
                return SimpleNamespace(data=[{"id": "p1"}])

        return SimpleNamespace(table=lambda _n: _Tbl())

    def test_approving_freezes_the_date_and_marks_it_manual(self):
        captured = {}
        with patch("processors.project_start_dates.supabase_client",
                   SimpleNamespace(client=self._client(captured))):
            out = apply_project_start(
                {"project_id": "p1", "recommended": "2026-03-26"})
        assert out["ok"] is True
        assert captured["start_date"] == "2026-03-26"
        assert captured["manual_start_date"] is True

    def test_a_proposal_with_no_date_is_refused_not_written_as_null(self):
        """Writing NULL would look like a decision to have no start."""
        captured = {}
        with patch("processors.project_start_dates.supabase_client",
                   SimpleNamespace(client=self._client(captured))):
            out = apply_project_start({"project_id": "p1", "recommended": None})
        assert out["ok"] is False
        assert captured == {}

    def test_a_proposal_with_no_project_is_refused(self):
        out = apply_project_start({"recommended": "2026-03-26"})
        assert out["ok"] is False


class TestTheManualRail:
    def test_start_date_is_defended_by_rule_2(self):
        """Without this the derive-then-freeze step could overwrite a date a
        human chose."""
        from services.supabase_client import SupabaseClient
        assert "start_date" in SupabaseClient._PROJECT_MANUAL_FIELDS


class TestWiring:
    def test_the_proposal_type_renders_a_card(self):
        from processors.proposal_review import _label
        card = _label("project_start_proposal", {
            "project_name": "Legal", "recommended": "2026-04-09",
            "gantt_date": "2026-03-02", "gantt_label": "Legal entity Establishment"})
        assert "Legal" in card and "2026-04-09" in card
        assert "2026-03-02" in card       # the evidence is shown

    def test_the_card_survives_a_missing_gantt_candidate(self):
        from processors.proposal_review import _label
        card = _label("project_start_proposal",
                      {"project_name": "Cloud", "recommended": "2026-07-22"})
        assert "Cloud" in card
        assert "old Gantt" not in card


class TestRetiredProjectsAreProposedToo:
    """Eyal asked for retired projects to stay on the Timeline in the Completed
    colour. A bar still needs a left edge, so skipping them at proposal time
    meant they could never be drawn at all — they would sit as dotted lines
    forever. They are also the case where the archived board is the only
    possible source, since a retired project usually has no open tasks.
    """

    def test_the_proposal_query_does_not_filter_out_retired(self):
        import ast
        import pathlib

        src = pathlib.Path("processors/project_start_dates.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "propose_project_starts")
        # No comparison against the literal "retired" anywhere in the function:
        # that is what excluded them.
        literals = {n.value for n in ast.walk(fn)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "retired" not in literals, (
            "propose_project_starts is filtering on status again — retired "
            "projects would lose their bars")
