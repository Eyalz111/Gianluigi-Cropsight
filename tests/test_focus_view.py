"""The Focus tab — one re-sliceable view of everything open.

Two things carry the weight here. `TestBuckets` pins that urgency is computed
from the DATE and never from `tasks.status`: on the day this was built 17 open
tasks were past their deadline while only 5 carried `status='overdue'`, so a
view trusting the status field would have under-reported the backlog 3x.

`TestTheEngineIgnoresTheView` is the structural one. Both tabs are generated,
and the reconcile enumerates every tab in the workbook that is not named as a
non-area resident. If that registration is ever lost, the engine parses a view
as an area tab and the formatting pass strips its rules — which is precisely
what happened to the meetings pool (defect #9, 2026-08-09).
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from processors import focus_view as fv


class TestBuckets:
    TODAY = date(2026, 8, 11)

    def test_a_past_date_is_overdue(self):
        assert fv.bucket_for(date(2026, 8, 4), self.TODAY)[0] == "OVERDUE"

    def test_today_is_its_own_bucket(self):
        assert fv.bucket_for(self.TODAY, self.TODAY)[0] == "TODAY"

    def test_the_week_boundary(self):
        assert fv.bucket_for(date(2026, 8, 18), self.TODAY)[0] == "THIS WEEK"
        assert fv.bucket_for(date(2026, 8, 19), self.TODAY)[0] == "THIS MONTH"

    def test_the_month_boundary(self):
        assert fv.bucket_for(date(2026, 9, 10), self.TODAY)[0] == "THIS MONTH"
        assert fv.bucket_for(date(2026, 9, 11), self.TODAY)[0] == "LATER"

    def test_no_date_sorts_last_but_is_never_dropped(self):
        """Undated work is the backlog that ages silently. It sorts to the
        bottom; it is never filtered out."""
        label, sort = fv.bucket_for(None, self.TODAY)
        assert label == "NO DATE"
        assert sort == max(s for _l, s in fv.BUCKETS)

    def test_buckets_sort_in_urgency_order(self):
        order = [fv.bucket_for(d, self.TODAY)[1] for d in (
            date(2026, 8, 1), self.TODAY, date(2026, 8, 14),
            date(2026, 9, 1), date(2027, 1, 1), None)]
        assert order == sorted(order)


class TestRowsComeFromTheDateNotTheStatus:
    def _client(self, tasks, meetings=()):
        def _table(name):
            data = {"areas": [{"id": "a1", "name": "PRODUCT & TECHNOLOGY"}],
                    "canonical_projects": [{"id": "p1", "name": "Pilot", "area_id": "a1"}],
                    "tasks": list(tasks),
                    "follow_up_meetings": list(meetings)}[name]
            q = SimpleNamespace()
            q.select = lambda *a, **k: q
            q.eq = lambda *a, **k: q
            q.limit = lambda *a, **k: q
            q.execute = lambda: SimpleNamespace(data=data)
            return q
        return SimpleNamespace(table=_table)

    def _rows(self, tasks, meetings=()):
        with patch.object(fv, "supabase_client",
                          SimpleNamespace(client=self._client(tasks, meetings))):
            return fv.build_rows(today=date(2026, 8, 11))

    def test_a_late_task_reads_overdue_even_when_status_says_pending(self):
        """The 3x under-report. status='pending', deadline last week."""
        rows = self._rows([{"id": "t1", "title": "Send the SoW", "status": "pending",
                            "approval_status": "approved", "deadline": "2026-08-04",
                            "assignee": "Paolo Vailetti", "priority": "U",
                            "project_id": "p1"}])
        assert len(rows) == 1
        assert rows[0][9] == "OVERDUE"
        assert rows[0][5] == "Urgent"
        assert rows[0][6] == "Pilot"
        assert rows[0][7] == "PRODUCT & TECHNOLOGY"

    def test_closed_and_suppressed_work_is_excluded(self):
        rows = self._rows([
            {"id": "1", "title": "done", "status": "done", "approval_status": "approved"},
            {"id": "2", "title": "archived", "status": "archived", "approval_status": "approved"},
            {"id": "3", "title": "hidden", "status": "pending", "ps_suppressed": True,
             "approval_status": "approved"},
            {"id": "4", "title": "open", "status": "pending", "approval_status": "approved"},
        ])
        assert [r[2] for r in rows] == ["open"]

    def test_meetings_appear_alongside_tasks(self):
        rows = self._rows(
            [{"id": "t", "title": "a task", "status": "pending", "approval_status": "approved",
              "deadline": "2026-08-12"}],
            [{"id": "m", "title": "Follow up with Ido", "status": "not_scheduled",
              "approval_status": "approved", "led_by": ""}])
        kinds = {r[0] for r in rows}
        assert kinds == {"Task", "Meeting"}
        meeting = [r for r in rows if r[0] == "Meeting"][0]
        assert meeting[9] == "NO DATE"
        assert meeting[3] == "(no lead)"      # never blank — blank reads as a bug

    def test_held_and_recurring_meetings_are_excluded(self):
        rows = self._rows([], [
            {"id": "1", "title": "held", "status": "held", "approval_status": "approved"},
            {"id": "2", "title": "weekly", "status": "recurring", "approval_status": "approved"},
            {"id": "3", "title": "real", "status": "not_scheduled", "approval_status": "approved"},
        ])
        assert [r[2] for r in rows] == ["real"]

    def test_rows_arrive_sorted_most_urgent_first(self):
        rows = self._rows([
            {"id": "1", "title": "later", "status": "pending", "approval_status": "approved",
             "deadline": "2027-01-01"},
            {"id": "2", "title": "late", "status": "pending", "approval_status": "approved",
             "deadline": "2026-08-01"},
            {"id": "3", "title": "undated", "status": "pending", "approval_status": "approved"},
        ])
        assert [r[2] for r in rows] == ["late", "later", "undated"]


class TestTheActionCarriesItsTopic:
    """The action alone is often meaningless. [2026-08-13]

    Eyal, reading the live tab: *"some are 'context missing' such as 'Yoram to
    reach out - Eyal to articulate a message for him' … cannot understand
    exactly what is the task without moving to the right tab."* On the area tab
    that row reads `Eitan Zemel` in Topic beside the action, and the Topic is
    what names the person, the grant or the counterparty. Focus was showing
    Project and dropping Topic entirely.

    `tasks.label` IS the topic — the 226 label values are topics, not projects.
    """

    def test_the_topic_prefixes_the_action(self):
        assert fv._what("Eitan Zemel", "Coordinate a meeting for Yoram",
                        "Investor Outreach") == \
            "Eitan Zemel — Coordinate a meeting for Yoram"

    def test_a_topic_that_only_repeats_the_project_is_suppressed(self):
        """A word with no information is worse than nothing in a narrow column."""
        assert fv._what("Business Plan", "Update the P&L", "Business Plan") == \
            "Update the P&L"

    def test_a_topic_already_inside_the_title_does_not_stutter(self):
        assert fv._what("Investor Materials", "Send Investor Materials NDA",
                        "Investors Materials") == "Send Investor Materials NDA"

    def test_no_topic_leaves_the_title_alone(self):
        assert fv._what("", "Bare title", "P") == "Bare title"
        assert fv._what(None, "Bare title", None) == "Bare title"

    def test_a_topic_with_no_title_is_better_than_an_empty_cell(self):
        assert fv._what("Eitan Zemel", "", "P") == "Eitan Zemel"

    def test_it_reaches_the_built_row(self):
        rows = TestRowsComeFromTheDateNotTheStatus()._rows(
            [{"id": "t1", "title": "Coordinate a meeting", "status": "pending",
              "approval_status": "approved", "deadline": "2026-08-12",
              "label": "Eitan Zemel", "project_id": "p1"}])
        assert rows[0][2] == "Eitan Zemel — Coordinate a meeting"


class TestMeetingsCarryTheirProject:
    """Every meeting row rendered a blank Project, so they all sat under
    "(no project)" and the Project filter could never find one.
    `follow_up_meetings.label` is already the canonical project name."""

    def test_a_meeting_shows_its_project_and_area(self):
        rows = TestRowsComeFromTheDateNotTheStatus()._rows(
            [], [{"id": "m", "title": "Sync", "status": "scheduled",
                  "approval_status": "approved", "proposed_date": "2026-08-12",
                  "label": "Pilot"}])
        assert rows[0][6] == "Pilot"
        assert rows[0][7] == "PRODUCT & TECHNOLOGY"

    def test_an_unmatched_label_yields_no_area_rather_than_a_guess(self):
        """A meeting filed under the wrong area is worse than one filed under
        none — the same rule the Timeline's meetings lanes follow."""
        rows = TestRowsComeFromTheDateNotTheStatus()._rows(
            [], [{"id": "m", "title": "Sync", "status": "scheduled",
                  "approval_status": "approved", "proposed_date": "2026-08-12",
                  "label": "Something nobody has heard of"}])
        assert rows[0][7] == ""


class TestTheWeekIsColoured:
    """Eyal: *"lets think if we should color also this week as the meetings with
    nechama will probably be weekly ones."* The weekly review is the operative
    rhythm, so THIS WEEK is the actionable set and TODAY is a subset of it."""

    def _rules(self):
        from services.focus_sheet import _bucket_colour_rules
        return _bucket_colour_rules(7)

    def _formulas(self):
        return [r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]
                ["values"][0]["userEnteredValue"] for r in self._rules()]

    def test_this_week_now_has_a_rule(self):
        assert any('"THIS WEEK"' in f for f in self._formulas())

    def test_it_is_a_lighter_shade_of_today_not_a_new_colour(self):
        """A different hue would read as a different KIND of urgency and compete
        with TODAY instead of nesting under it."""
        from services.focus_sheet import _TODAY_BG, _WEEK_BG
        assert _WEEK_BG != _TODAY_BG
        for ch in ("red", "green", "blue"):
            assert _WEEK_BG[ch] >= _TODAY_BG[ch], f"{ch} must be lighter"
        # Same hue family: the red-to-blue fall-off keeps its direction.
        assert _WEEK_BG["red"] > _WEEK_BG["blue"]

    def test_this_month_and_later_stay_white(self):
        """Those two would flatten the contrast, and neither is something you
        act on in a weekly meeting."""
        joined = " ".join(self._formulas())
        assert "THIS MONTH" not in joined
        assert "LATER" not in joined

    def test_the_rule_indexes_are_distinct(self):
        idx = [r["addConditionalFormatRule"]["index"] for r in self._rules()]
        assert len(set(idx)) == len(idx)


class TestTheEngineIgnoresTheView:
    def test_both_tabs_are_registered_as_non_area(self):
        from processors.project_status_reconcile import NON_AREA_TABS
        assert fv.FOCUS_TAB in NON_AREA_TABS
        assert fv.FOCUS_DATA_TAB in NON_AREA_TABS

    def test_the_view_would_not_pass_the_area_layout_check(self):
        """Belt to the registration's braces: even if the name were dropped,
        the Focus header must not accidentally satisfy an area tab's columns."""
        from services.project_status_rows import unresolved_columns
        assert unresolved_columns(fv.HEADERS)


class TestTheQueryFormula:
    def test_it_reads_the_data_tab_and_selects_every_display_column(self):
        f = fv._query_formula()
        assert f.startswith("=IFERROR(QUERY(")
        assert f"'{fv.FOCUS_DATA_TAB}'!A2:K" in f
        assert "select J, B, C, D, F, G, H, A" in f

    def test_every_grouping_choice_has_a_sort(self):
        """A dropdown choice with no matching sort would silently fall through
        to the default and look like the control was ignored."""
        for choice in fv.GROUP_CHOICES:
            assert choice in fv._GROUP_SORT
            assert choice in fv._query_formula()

    def test_every_show_choice_appears(self):
        f = fv._query_formula()
        for choice in fv.SHOW_CHOICES:
            if choice != "Everything open":     # the empty-clause default
                assert choice in f

    def test_it_degrades_to_a_sentence_not_an_error_cell(self):
        assert "Nothing matches" in fv._query_formula()

    def test_filtering_by_area_still_shows_meetings(self):
        """Meetings carry no area. A strict `H = area` would hide all of them
        the moment an area was chosen — and most already have no date, so they
        would vanish from the one view meant to catch them."""
        f = fv._query_formula()
        assert "or A = 'Meeting'" in f

    def test_filtering_by_owner_is_strict(self):
        """Owner is different: a meeting HAS a lead, so 'show me Paolo's work'
        should not quietly include meetings led by someone else."""
        f = fv._query_formula()
        assert " and D = '" in f
        assert "D = '\"&D2&\"' or" not in f

    def test_the_layout_puts_controls_and_query_where_the_formula_expects(self):
        layout = fv.focus_layout()
        assert layout[1][1] == fv.GROUP_CHOICES[0]      # B2 group
        assert layout[1][3] == "All"                     # D2 owner
        assert layout[1][5] == "All"                     # F2 area
        assert layout[1][7] == fv.SHOW_CHOICES[0]        # H2 show
        assert layout[3] == fv.HEADERS                   # row 4
        assert layout[4][0].startswith("=IFERROR(QUERY(")

    def test_data_headers_and_query_letters_stay_in_step(self):
        """The QUERY addresses columns by letter, so a column inserted in the
        middle of DATA_HEADERS silently re-points every selector."""
        assert fv.DATA_HEADERS.index("Bucket") == 9        # J
        assert fv.DATA_HEADERS.index("BucketSort") == 10   # K
        assert fv.DATA_HEADERS.index("Due") == 1           # B
        assert fv.DATA_HEADERS.index("What") == 2          # C
