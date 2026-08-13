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


class TestTheFilterMovedToTheServer:
    """The QUERY is gone; `display_rows` does the filtering now. [2026-08-13]

    Not a refactor — a QUERY owns its whole spill range, so its rows cannot be
    typed into, and an edit placed BESIDE one is anchored to a row POSITION and
    re-points at a different task the moment a dropdown changes. Materialising
    is what makes Focus editable at all.

    Every filter SEMANTIC below is carried over from the formula unchanged;
    these are the same behaviours, re-pinned against the new mechanism.
    """

    def _raw(self, *rows):
        """Rows in `build_rows` shape: Kind, Due, What, Who, PriSort, Priority,
        Project, Area, Status, Bucket, BucketSort, Uid."""
        return list(rows)

    def _task(self, what="a task", who="Eyal Zror", area="P&T", bsort=3,
              due="2026-08-20", uid="t1", pri="M", prisort=3):
        return ["Task", due, what, who, prisort, pri, "Proj", area, "pending",
                "THIS WEEK", bsort, uid]

    def _meeting(self, what="a meeting", who="Eyal Zror", uid="m1"):
        return ["Meeting", "", what, who, 3, "M", "", "", "scheduled",
                "NO DATE", 6, uid]

    def test_every_grouping_choice_has_a_sort(self):
        """A dropdown choice with no matching sort would silently fall through
        to the default and look like the control was ignored."""
        for choice in fv.GROUP_CHOICES:
            assert choice in fv._GROUP_KEY

    def test_every_show_choice_filters_or_is_the_default(self):
        for choice in fv.SHOW_CHOICES:
            assert choice == "Everything open" or choice in fv._SHOW_BUCKETS

    def test_filtering_by_area_still_shows_meetings(self):
        """Meetings carry no area. A strict match would hide all of them the
        moment an area was chosen — and most already have no date, so they would
        vanish from the one view meant to catch them."""
        raw = self._raw(self._task(area="P&T"), self._meeting())
        out = fv.display_rows(raw, area="SALES")
        assert [r[3] for r in out] == ["a meeting"]

    def test_filtering_by_owner_is_strict(self):
        """Owner is different: a meeting HAS a lead, so "show me Paolo's work"
        must not quietly include meetings led by someone else."""
        raw = self._raw(self._task(who="Paolo Vailetti"),
                        self._meeting(who="Eyal Zror"))
        out = fv.display_rows(raw, owner="Paolo Vailetti")
        assert [r[3] for r in out] == ["a task"]

    def test_overdue_only_keeps_just_the_late_ones(self):
        raw = self._raw(self._task(what="late", bsort=1),
                        self._task(what="soon", bsort=3, uid="t2"))
        assert [r[3] for r in fv.display_rows(raw, show="Overdue only")] == ["late"]

    def test_due_this_week_includes_overdue_and_today(self):
        """Something already late is not excluded from "this week" — it is the
        most this-week thing there is."""
        raw = self._raw(self._task(what="late", bsort=1),
                        self._task(what="today", bsort=2, uid="t2"),
                        self._task(what="week", bsort=3, uid="t3"),
                        self._task(what="month", bsort=4, uid="t4"))
        got = [r[3] for r in fv.display_rows(raw, show="Due this week")]
        assert got == ["late", "today", "week"]

    def test_a_row_with_no_text_is_dropped(self):
        assert fv.display_rows(self._raw(self._task(what=""))) == []

    def test_undated_work_sorts_last_but_is_never_filtered_out(self):
        """Undated work is the backlog that ages silently, and a view that hides
        it is how a to-do list quietly stops being true."""
        raw = self._raw(self._task(what="dated", bsort=3, due="2026-08-20"),
                        self._task(what="undated", bsort=6, due="", uid="t2"))
        got = [r[3] for r in fv.display_rows(raw)]
        assert got == ["dated", "undated"]

    def test_the_controls_are_preserved_not_reset(self):
        """The server reads these to filter now, so writing the defaults back
        every refresh would drop Nechama to "Everything open" every thirty
        minutes — mid-meeting, while she was looking at it."""
        layout = fv.focus_layout({"group": "Owner", "owner": "Paolo Vailetti",
                                  "area": "SALES", "show": "Overdue only"})
        assert layout[1][1] == "Owner"
        assert layout[1][3] == "Paolo Vailetti"
        assert layout[1][5] == "SALES"
        assert layout[1][7] == "Overdue only"

    def test_the_layout_defaults_when_it_is_given_nothing(self):
        layout = fv.focus_layout()
        assert layout[1][1] == fv.GROUP_CHOICES[0]
        assert layout[1][7] == fv.SHOW_CHOICES[0]

    def test_the_header_row_carries_the_hidden_identity_columns(self):
        layout = fv.focus_layout()
        assert layout[3] == fv.HEADERS + fv.FOCUS_HIDDEN_HEADERS

    def test_a_stale_control_value_falls_back_rather_than_returning_nothing(self):
        """An owner with no open work left would filter to zero rows and read as
        "nothing is open" — the one wrong answer this tab must never give."""
        assert fv.valid_control("owner", "Somebody Gone", ["Eyal Zror"]) == "All"
        assert fv.valid_control("area", "OLD AREA", areas=["P&T"]) == "All"
        assert fv.valid_control("group", "nonsense") == fv.GROUP_CHOICES[0]
        assert fv.valid_control("show", "nonsense") == fv.SHOW_CHOICES[0]

    def test_a_valid_control_value_is_kept(self):
        assert fv.valid_control("owner", "Eyal Zror", ["Eyal Zror"]) == "Eyal Zror"
        assert fv.valid_control("group", "Owner") == "Owner"

    def test_data_headers_and_the_column_indexes_stay_in_step(self):
        """Everything downstream indexes these by position."""
        assert fv.DATA_HEADERS.index("Bucket") == fv.DCOL_BUCKET
        assert fv.DATA_HEADERS.index("BucketSort") == fv.DCOL_BUCKETSORT
        assert fv.DATA_HEADERS.index("Due") == fv.DCOL_DUE
        assert fv.DATA_HEADERS.index("What") == fv.DCOL_WHAT
        assert fv.DATA_HEADERS.index("Uid") == fv.DCOL_UID


class TestTheHeaderAndTheDataAgreeOnWidth:
    """This misalignment has now been introduced TWICE by the same hand, and the
    full suite passed both times because nothing compared the two. [2026-08-13]

    The header row and the rows beneath it are produced by different code — the
    header from HEADERS, the body from either the QUERY's select list or the
    materialised renderer. When Phase C added the Done column to HEADERS without
    the body following, the header sat one column out of step with its own data
    on a live tab, and no test noticed.

    A tab whose columns are labelled wrong is worse than one that fails to
    render: it looks correct and every value is attributed to the wrong field.
    """

    def test_the_visible_body_has_exactly_as_many_columns_as_HEADERS(self):
        from processors.focus_view import (
            HEADERS, FOCUS_HIDDEN_HEADERS, DATA_HEADERS, display_rows)

        raw = [["Task", "2026-08-20", "a thing", "Eyal", 3, "M", "P", "A",
                "pending", "THIS WEEK", 3, "uid-1"]]
        assert len(raw[0]) == len(DATA_HEADERS), "the fixture drifted"
        body = display_rows(raw)
        assert body, "display_rows produced nothing"
        assert len(body[0]) == len(HEADERS) + len(FOCUS_HIDDEN_HEADERS), (
            f"HEADERS declares {len(HEADERS)} visible columns plus "
            f"{len(FOCUS_HIDDEN_HEADERS)} hidden, but the body produces "
            f"{len(body[0])}. The header row would label the wrong data.")
