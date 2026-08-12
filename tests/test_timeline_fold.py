"""Option A — the board must not grow with finished work. [2026-08-13]

Decisions 8 and 9 of docs/GANTT_V2_PLAN.md, after Eyal reviewed the live tab:
*"once we will add more and more projects, it will be too big… in the previous
format we just had the certain weeks that the project is there, and then once it
finished the timeline keeps on going and we kind of get rid of it."*

The old board never grew because its rows were LANES and work flowed through
them. Project-per-row makes every project a permanent row — 27 today, ~60 in
eighteen months — and closing one ADDS to the pile. Option A keeps projects as
the rows and folds the finished ones into a single collapsed line per area, so
visible height tracks OPEN work.

None of it is possible while status is COUNTED: a project with no open tasks
read as 'planned', the same colour as one that had never started, so nothing
could tell finished from not-yet-begun. Hence decision 9, pinned here too.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from processors.timeline_view import (
    FOLD_STATUSES, PROJECT_STATUSES, ROW_FOLD, ROW_MEETINGS, ROW_MILESTONE,
    ROW_PROJECT, is_folded, meetings_lanes, milestone_band, nearest_action,
    week_starts,
)

TODAY = date(2026, 8, 13)


def _ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def _proj(**kw) -> dict:
    base = {"project_id": "p1", "name": "P", "owner": "E",
            "start": date(2026, 3, 2), "target": date(2026, 4, 6),
            "first_col": 0, "last_col": 5, "open_ended": False,
            "declared": "active", "status": "active", "folded": False,
            "action": "", "tasks": []}
    base.update(kw)
    return base


def _data(**kw) -> dict:
    base = {"weeks": week_starts(), "areas": {"AREA": [_proj()]},
            "folded": {}, "lanes": {}, "milestones": [], "archive": [],
            "stats": {}}
    base.update(kw)
    return base


class _Chain:
    """A PostgREST query builder that ignores every filter and returns `data`.

    Keyed on the TABLE, never on call order — three separate test breakages this
    month came from positional `side_effect` lists.
    """

    def __init__(self, data):
        self._data = data

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return MagicMock(data=self._data)


def _build(rows: dict) -> dict:
    """Run build_timeline against a fake database."""
    import processors.timeline_view as tv

    client = MagicMock()
    client.table.side_effect = lambda name: _Chain(rows.get(name, []))
    sc = MagicMock()
    sc.client = client
    sc.list_follow_up_meetings.return_value = rows.get("follow_up_meetings", [])

    with patch.object(tv, "supabase_client", sc), \
         patch.object(tv.settings, "TIMELINE_LEGACY_OVERLAY_ENABLED", False), \
         patch.object(tv, "_today", lambda: TODAY):
        return tv.build_timeline()


async def _render(data):
    """Run refresh_timeline against a fake Sheets and return (grid, requests)."""
    import services.timeline_sheet as ts

    captured = {}
    svc = MagicMock()
    svc._execute_with_retry.side_effect = lambda f: f()

    async def _ensure(*a, **kw):
        return 7
    svc._ensure_tab = _ensure
    sheets = svc.service.spreadsheets.return_value
    sheets.get.side_effect = lambda **kw: {"sheets": []}

    def _batch(**kw):
        # Keyed on CONTENT: the grid-width assertion is its own one-request
        # batch and would otherwise be captured instead of the formatting one.
        if len(kw["body"]["requests"]) > 1:
            captured["reqs"] = kw["body"]["requests"]
        return MagicMock()
    sheets.batchUpdate.side_effect = _batch

    def _update(**kw):
        captured["grid"] = kw["body"]["values"]
        return MagicMock()
    sheets.values.return_value.update.side_effect = _update
    sheets.values.return_value.clear.side_effect = lambda **kw: MagicMock()

    with patch.object(ts, "build_timeline", return_value=data), \
         patch.object(ts, "sheets_service", svc), \
         patch.object(ts.settings, "PROJECT_STATUS_SHEET_ID", "ssid"), \
         patch.object(ts.settings, "TIMELINE_READBACK_ENABLED", False):
        await ts.refresh_timeline()
    return captured.get("grid", []), captured.get("reqs", [])


def _kinds(grid):
    """The `_kind` hidden column, one entry per rendered row."""
    from processors.timeline_view import N_HIDDEN
    return [str(r[-N_HIDDEN + 1]).strip() for r in grid]


class TestTheFoldRule:
    """A month's grace for `done`, none for `retired`."""

    def test_active_and_blocked_never_fold(self):
        assert not is_folded({"status": "active"}, TODAY)
        assert not is_folded({"status": "blocked"}, TODAY)

    def test_retired_folds_at_once(self):
        """Retired is not a win — there is nothing to leave on show."""
        assert is_folded({"status": "retired"}, TODAY)

    def test_done_past_the_grace_folds(self):
        assert is_folded({"status": "done", "done_at": _ago(40)}, TODAY)

    def test_done_inside_the_grace_stays(self):
        """Eyal's month, so recent wins are still visible where the work is."""
        assert not is_folded({"status": "done", "done_at": _ago(5)}, TODAY)

    def test_a_MISSING_done_at_never_folds(self):
        """THE GUARD THAT MATTERS. Before migrate_project_declared_status.sql
        runs, every row reads None. Treating an absent timestamp as infinitely
        old would fold every finished project the first time this ran."""
        assert not is_folded({"status": "done"}, TODAY)

    def test_an_explicit_null_done_at_never_folds(self):
        """A present-but-null column, not just a missing key — the distinction
        that broke every meeting edit for two days on 2026-08-11."""
        assert not is_folded({"status": "done", "done_at": None}, TODAY)

    def test_the_grace_is_configurable(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "PROJECT_FOLD_GRACE_DAYS", 90,
                            raising=False)
        assert not is_folded({"status": "done", "done_at": _ago(40)}, TODAY)

    def test_an_unknown_status_never_folds(self):
        """Falling back to `active` means a typo costs a row on the board, not
        a live project disappearing from it."""
        assert not is_folded({"status": "banana"}, TODAY)

    def test_the_fold_set_is_exactly_done_and_retired(self):
        assert FOLD_STATUSES == {"done", "retired"}
        assert FOLD_STATUSES < set(PROJECT_STATUSES)


class TestTheFoldRenders:
    async def test_an_area_gets_one_collapsed_line_for_its_finished_work(self):
        grid, reqs = await _render(_data(
            areas={"AREA": [_proj(project_id="a", name="Open")]},
            folded={"AREA": [_proj(project_id="b", name="Shipped",
                                   declared="done", folded=True),
                             _proj(project_id="c", name="Dropped",
                                   declared="retired", folded=True)]}))
        labels = [r[0] for r in grid]
        assert any("Completed (2)" in str(x) for x in labels), labels[:12]
        # ONE line, however many projects are behind it.
        assert sum(1 for k in _kinds(grid) if k == ROW_FOLD) == 1

    async def test_the_folded_rows_arrive_collapsed(self):
        """addDimensionGroup CREATES a group and never closes it, so without the
        second request the fold is a fold in name only."""
        grid, reqs = await _render(_data(
            folded={"AREA": [_proj(project_id="b", declared="done",
                                   folded=True)]}))
        collapses = [r for r in reqs if "updateDimensionGroup" in r]
        assert collapses, "no group was ever collapsed"
        assert all(r["updateDimensionGroup"]["dimensionGroup"]["collapsed"]
                   for r in collapses)

    async def test_every_group_is_collapsed_not_only_the_archive(self):
        """The task waterfall claimed 'collapsed by default' in a comment and
        never sent the request. With the fold in place that stops being
        cosmetic: an expanded waterfall puts every task back on the board and
        undoes exactly the height the fold reclaims."""
        grid, reqs = await _render(_data(
            areas={"AREA": [_proj(tasks=[
                {"title": "t1", "assignee": "R", "deadline": None,
                 "priority": "M"}])]}))
        adds = [r for r in reqs if "addDimensionGroup" in r]
        collapses = [r for r in reqs if "updateDimensionGroup" in r]
        assert len(adds) == 1, "the task waterfall should be one group"
        assert len(collapses) == len(adds), "every group must be closed"

    def test_a_folded_bar_is_never_open_ended(self):
        """An open end means 'still going, end unknown' — a contradiction on a
        project somebody declared finished, and it would wash the pale tint from
        its start to the right edge of the grid, so the archive of what we
        shipped would mostly claim ongoing work."""
        out = _build({
            "areas": [{"id": "A1", "name": "AREA"}],
            "canonical_projects": [{"id": "p1", "name": "Done thing",
                                    "area_id": "A1", "status": "done",
                                    "done_at": _ago(60),
                                    "start_date": "2026-03-02",
                                    "target_date": None}],
        })
        folded = out["folded"]["AREA"][0]
        assert folded["folded"] is True
        assert folded["open_ended"] is False

    def test_an_open_project_with_no_target_still_runs_open(self):
        """The converse — decision 1 is untouched by the fold."""
        out = _build({
            "areas": [{"id": "A1", "name": "AREA"}],
            "canonical_projects": [{"id": "p1", "name": "Live thing",
                                    "area_id": "A1", "status": "active",
                                    "start_date": "2026-03-02",
                                    "target_date": None}],
        })
        assert out["areas"]["AREA"][0]["open_ended"] is True

    def test_an_area_whose_projects_are_all_finished_keeps_its_key(self):
        """Otherwise the area silently vanishes from the tab and its history is
        unreachable — the render iterates `areas`, not `folded`."""
        out = _build({
            "areas": [{"id": "A1", "name": "AREA"}],
            "canonical_projects": [{"id": "p1", "name": "Done thing",
                                    "area_id": "A1", "status": "retired",
                                    "start_date": "2026-03-02"}],
        })
        assert out["areas"] == {"AREA": []}
        assert len(out["folded"]["AREA"]) == 1

    async def test_that_area_still_renders_its_header_and_its_fold(self):
        grid, _ = await _render(_data(
            areas={"AREA": []},
            folded={"AREA": [_proj(declared="retired", folded=True)]}))
        labels = [str(r[0]).strip() for r in grid]
        assert "AREA" in labels
        assert any("Completed (1)" in x for x in labels)


class TestBarsCarryText:
    def test_the_nearest_action_is_the_first_task(self):
        """`tasks` arrives sorted by deadline with undated ones last."""
        assert nearest_action([{"title": "Call the lawyer"},
                               {"title": "Later thing"}]) == "Call the lawyer"

    def test_no_tasks_means_no_text(self):
        assert nearest_action([]) == ""

    async def test_the_text_lands_in_the_bars_first_cell(self):
        """Sheets overflows text rightwards into empty cells, so it reads across
        its own bar and stops at the next one — the same mechanism the old board
        used to label its bars."""
        from processors.timeline_view import N_LABEL_COLS
        grid, _ = await _render(_data(areas={"AREA": [
            _proj(first_col=3, last_col=9, action="Send the term sheet")]}))
        row = next(r for r, k in zip(grid, _kinds(grid)) if k == ROW_PROJECT)
        assert row[N_LABEL_COLS + 3] == "Send the term sheet"


class TestMeetingsLanes:
    """Six rows for the company, not six hundred. The question a Gantt answers
    about meetings is how often an area meets, not which Tuesday the third sync
    was."""

    WEEKS = week_starts()
    AREAS = {"legal": "Legal", "italy": "Sales"}

    def test_a_recurring_meeting_is_a_band_not_a_marker(self):
        lanes = meetings_lanes(
            [{"label": "Legal", "status": "recurring", "proposed_date": None}],
            self.AREAS, self.WEEKS)
        assert lanes["Legal"]["recurring"] == 1
        assert lanes["Legal"]["markers"] == {}

    def test_a_booked_one_off_is_a_marker(self):
        lanes = meetings_lanes(
            [{"label": "Legal", "status": "scheduled",
              "proposed_date": "2026-03-02"}], self.AREAS, self.WEEKS)
        assert lanes["Legal"]["markers"] == {0: 1}

    def test_several_in_one_week_collapse_to_one_marker_with_a_count(self):
        """Two markers cannot share a cell, and dropping all but one would
        quietly under-report a busy week."""
        lanes = meetings_lanes(
            [{"label": "Legal", "status": "held", "proposed_date": "2026-03-02"},
             {"label": "Legal", "status": "scheduled",
              "proposed_date": "2026-03-05"}], self.AREAS, self.WEEKS)
        assert lanes["Legal"]["markers"] == {0: 2}

    @pytest.mark.parametrize("status", ["parked", "to_schedule", "dropped"])
    def test_a_meeting_with_no_agreed_date_draws_nothing(self, status):
        """A marker is a claim that something happens in a particular week."""
        lanes = meetings_lanes(
            [{"label": "Legal", "status": status,
              "proposed_date": "2026-03-02"}], self.AREAS, self.WEEKS)
        assert lanes == {}

    def test_an_old_spelling_still_reaches_its_lane(self):
        lanes = meetings_lanes(
            [{"label": "Legal", "status": "not_scheduled",
              "proposed_date": "2026-03-02"}], self.AREAS, self.WEEKS)
        assert lanes == {}, "to_schedule has no agreed date, so no marker"

    def test_an_unknown_label_contributes_to_no_lane(self):
        """Guessing an area from free text is the same name matching that drew
        2 correct ghosts out of 27 attempts. A meeting under the wrong area is
        worse than one drawn nowhere."""
        lanes = meetings_lanes(
            [{"label": "Something else", "status": "scheduled",
              "proposed_date": "2026-03-02"}], self.AREAS, self.WEEKS)
        assert lanes == {}

    def test_a_date_off_the_grid_draws_nothing(self):
        lanes = meetings_lanes(
            [{"label": "Legal", "status": "scheduled",
              "proposed_date": "2020-01-01"}], self.AREAS, self.WEEKS)
        assert lanes == {}

    async def test_the_lane_is_one_row_per_area(self):
        grid, _ = await _render(_data(
            lanes={"AREA": {"recurring": 2, "markers": {4: 1}}}))
        assert sum(1 for k in _kinds(grid) if k == ROW_MEETINGS) == 1


class TestMilestoneBand:
    """Read-only here; the CEO tab stays their editable home. The band earns its
    row by being the one place a commitment can be read against the work under
    it."""

    WEEKS = week_starts()

    def test_a_dated_milestone_becomes_a_marker(self):
        band = milestone_band(
            [{"title": "MVP", "target_date": "2026-03-02"}], self.WEEKS)
        assert len(band) == 1 and band[0]["col"] == 0

    def test_an_undated_milestone_draws_nothing(self):
        """An undated commitment is not a commitment in a week, and landing it
        at the edge of the grid would invent one."""
        assert milestone_band([{"title": "Someday"}], self.WEEKS) == []

    def test_a_move_is_recorded_as_a_move(self):
        band = milestone_band([{"title": "Signing #1", "target_date": "2026-07-06",
                                "original_date": "2026-06-01"}], self.WEEKS)
        assert band[0]["moved"] is True
        assert band[0]["original"] == date(2026, 6, 1)

    def test_an_unmoved_milestone_is_not_flagged(self):
        band = milestone_band([{"title": "MVP", "target_date": "2026-06-01",
                                "original_date": "2026-06-01"}], self.WEEKS)
        assert band[0]["moved"] is False

    async def test_the_note_never_says_slipped(self):
        """Whether a move was a slip or a re-plan is Eyal's read, and the board
        does not put a word in his mouth. A test asserts no verdict word appears
        at all."""
        _, reqs = await _render(_data(milestones=[
            {"col": 3, "title": "Signing #1", "status": "open", "moved": True,
             "original": date(2026, 6, 1), "target": date(2026, 7, 6)}]))
        notes = [v["note"]
                 for r in reqs if "updateCells" in r
                 for row in r["updateCells"]["rows"]
                 for v in row["values"] if "note" in v]
        assert notes, "the milestone marker carries no note"
        joined = " ".join(notes).lower()
        assert "moved" in joined
        for verdict in ("slipped", "late", "missed", "overdue", "delayed"):
            assert verdict not in joined

    async def test_the_band_is_one_row_above_the_areas(self):
        grid, _ = await _render(_data(milestones=[
            {"col": 3, "title": "MVP", "status": "open", "moved": False,
             "original": None, "target": date(2026, 3, 23)}]))
        kinds = _kinds(grid)
        assert kinds.count(ROW_MILESTONE) == 1
        from processors.timeline_view import ROW_AREA
        assert kinds.index(ROW_MILESTONE) < kinds.index(ROW_AREA)

    async def test_no_milestones_means_no_band_row(self):
        grid, _ = await _render(_data(milestones=[]))
        assert ROW_MILESTONE not in _kinds(grid)


class TestAssertDontAddForTheNewState:
    """Data validation and cell notes survive values().clear() exactly like
    formatting and protected ranges do."""

    async def test_the_status_dropdown_is_wiped_before_it_is_set(self):
        """Otherwise a dropdown stays on a row that is now a task."""
        _, reqs = await _render(_data())
        vals = [i for i, r in enumerate(reqs) if "setDataValidation" in r]
        assert len(vals) >= 2, "expected a wipe and at least one set"
        assert "rule" not in reqs[vals[0]]["setDataValidation"], \
            "the first validation request must be the ruleless wipe"

    async def test_the_dropdown_offers_exactly_the_declared_vocabulary(self):
        _, reqs = await _render(_data())
        rules = [r["setDataValidation"]["rule"] for r in reqs
                 if "setDataValidation" in r and "rule" in r["setDataValidation"]]
        assert rules
        offered = [v["userEnteredValue"]
                   for v in rules[0]["condition"]["values"]]
        assert offered == list(PROJECT_STATUSES)

    async def test_notes_are_wiped_before_they_are_set(self):
        """Last week's milestone note must not hover over a cell that no longer
        has a marker in it."""
        _, reqs = await _render(_data(milestones=[
            {"col": 3, "title": "MVP", "status": "open", "moved": False,
             "original": None, "target": date(2026, 3, 23)}]))
        wipes = [i for i, r in enumerate(reqs)
                 if (r.get("repeatCell") or {}).get("fields") == "note"]
        sets = [i for i, r in enumerate(reqs) if "updateCells" in r]
        assert wipes and sets
        assert min(wipes) < min(sets), "the wipe must precede the notes"
