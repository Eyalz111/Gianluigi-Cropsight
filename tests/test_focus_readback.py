"""Reading the Focus tab back — and everything it refuses to do. [2026-08-13]

Phase C. Eyal: *"this tab i believe will be the one we are working on in our
meetings in the end … lets say i want to delete or sign as done in this focus
tab, can it be done?"*

This is the phase that puts a FOURTH writer near live task rows, so most of what
is pinned here is a refusal rather than an action. The one rule with no
equivalent anywhere else in the codebase is that ABSENCE IS NEVER A DELETE:
every other readback sees a complete set, and Focus is a filtered view where
choosing "Overdue only" hides 70 of 84 rows.
"""

import pytest

from processors.focus_readback import build_focus_plan, layout_ok, parse_sheet_date
from processors.focus_view import (
    FCOL_DONE, FCOL_DUE, FCOL_PRIORITY, FOCUS_HIDDEN_HEADERS, HEADERS,
    N_FOCUS_HIDDEN, ROW_MEETING, ROW_TASK,
)

N_COLS = len(HEADERS) + N_FOCUS_HIDDEN


def _header():
    return list(HEADERS) + list(FOCUS_HIDDEN_HEADERS)


def _row(kind=ROW_TASK, uid="t1", done=False, due="", priority=""):
    row = [""] * N_COLS
    row[FCOL_DONE] = done
    row[FCOL_DUE] = due
    row[FCOL_PRIORITY] = priority
    row[-N_FOCUS_HIDDEN], row[-N_FOCUS_HIDDEN + 1] = uid, kind
    return row


def _grid(*rows):
    """Rows 1-3 are chrome, row 4 is the header, the body follows."""
    blank = [""] * N_COLS
    return [blank, blank, blank, _header(), *rows]


def _task(uid="t1", **kw):
    base = {"id": uid, "title": "Send the SoW", "deadline": "2026-08-20",
            "priority": "M", "status": "pending"}
    base.update(kw)
    return {uid: base}


def _snap(uid="t1", kind=ROW_TASK, **kw):
    base = {"due": "2026-08-20", "priority": "M"}
    base.update(kw)
    return {(kind, uid): base}


class TestTheLayoutGate:
    def test_the_expected_shape_passes(self):
        assert layout_ok(_header())

    def test_a_reordered_tab_is_refused(self):
        """Focus is the most exposed surface for this — its column order is the
        one thing a person might rearrange to suit a meeting."""
        bad = _header()
        bad[FCOL_DUE], bad[FCOL_PRIORITY] = bad[FCOL_PRIORITY], bad[FCOL_DUE]
        assert not layout_ok(bad)

    def test_a_tab_without_the_identity_columns_is_refused(self):
        assert not layout_ok(list(HEADERS))

    def test_a_mismatched_layout_changes_nothing(self):
        bad = _header()
        bad[0] = "Something else"
        grid = [[""] * N_COLS] * 3 + [bad, _row(due="21/08/2026")]
        plan = build_focus_plan(grid, _task(), {}, _snap())
        assert plan.task_updates == {} and plan.closed == []
        assert plan.counters.get("layout_mismatch") == 1


class TestAbsenceIsNeverADelete:
    """THE RULE THAT MAKES FOCUS DIFFERENT. Every other readback in this codebase
    sees a COMPLETE set, so a vanished row means somebody removed it — the
    meetings reconcile reads exactly that as a drop. Focus is FILTERED: choose
    "Overdue only" and 70 of 84 rows disappear."""

    def test_a_task_absent_from_the_tab_is_untouched(self):
        plan = build_focus_plan(_grid(), _task("t1"), {}, _snap("t1"))
        assert plan.task_updates == {}
        assert plan.closed == []

    def test_the_plan_has_no_delete_path_at_all(self):
        """Not "we skip it" — there is no mechanism. A future edit cannot
        accidentally re-enable one."""
        plan = build_focus_plan(_grid(_row()), _task(), {}, _snap())
        assert not hasattr(plan, "deleted")
        assert not hasattr(plan, "drops")

    def test_a_row_whose_task_is_gone_is_reported_not_actioned(self):
        plan = build_focus_plan(_grid(_row(uid="ghost")), _task("t1"), {},
                                _snap("t1"))
        assert plan.counters.get("orphan_rows") == 1
        assert plan.task_updates == {}


class TestDoneIsATransition:
    """Focus lists only OPEN work, so a row being here means it is not closed and
    the render always draws the box unticked. There is no database value to push
    back — an item with one worth pushing would not be on the tab."""

    def test_ticking_closes_it(self):
        plan = build_focus_plan(_grid(_row(done=True)), _task(), {}, _snap())
        assert plan.closed == [(ROW_TASK, "t1")]

    @pytest.mark.parametrize("value", [True, "TRUE", "true", "1"])
    def test_every_shape_the_api_returns_a_tick_in(self, value):
        plan = build_focus_plan(_grid(_row(done=value)), _task(), {}, _snap())
        assert plan.closed == [(ROW_TASK, "t1")]

    @pytest.mark.parametrize("value", [False, "FALSE", "false", "", None])
    def test_unticking_does_nothing(self, value):
        """Unticking a box the render just drew is not a request to reopen — the
        item was never closed. Treating it as one would let a stray click
        resurrect work on a tab where rows move under the cursor every time a
        dropdown changes."""
        plan = build_focus_plan(_grid(_row(done=value)), _task(), {}, _snap())
        assert plan.closed == []

    def test_closing_does_not_also_merge_the_other_cells(self):
        """The values are about to stop mattering, and pulling a half-typed date
        on the way out is how a tidy-up writes something nobody meant."""
        plan = build_focus_plan(
            _grid(_row(done=True, due="21/08/2026", priority="Urgent")),
            _task(), {}, _snap())
        assert plan.closed == [(ROW_TASK, "t1")]
        assert plan.task_updates == {}

    def test_a_meeting_closes_as_a_meeting(self):
        meetings = {"m1": {"id": "m1", "proposed_date": "2026-08-20",
                           "priority": "M", "status": "scheduled"}}
        plan = build_focus_plan(_grid(_row(kind=ROW_MEETING, uid="m1", done=True)),
                                {}, meetings, _snap("m1", kind=ROW_MEETING))
        assert plan.closed == [(ROW_MEETING, "m1")]


class TestDueAndPriorityMerge:
    def test_a_typed_date_is_pulled_and_marked_manual(self):
        plan = build_focus_plan(_grid(_row(due="21/08/2026", priority="M")),
                                _task(), {}, _snap())
        assert plan.task_updates == {"t1": {"deadline": "2026-08-21"}}
        assert (ROW_TASK, "t1", "deadline") in plan.manual_marks

    def test_a_typed_priority_is_translated_to_the_db_letter(self):
        """The sheet says Urgent, the database stores U. Comparing raw would
        make every row differ on every cycle and churn the cell forever."""
        plan = build_focus_plan(_grid(_row(due="20/08/2026", priority="Urgent")),
                                _task(), {}, _snap())
        assert plan.task_updates == {"t1": {"priority": "U"}}

    def test_no_merge_base_means_no_edit(self):
        """With no snapshot every populated cell differs from "", so the whole
        row would be pulled in and frozen as human decisions — the guard the
        sibling rail shipped without."""
        plan = build_focus_plan(_grid(_row(due="21/08/2026", priority="Urgent")),
                                _task(), {}, {})
        assert plan.task_updates == {}

    def test_a_blank_due_date_is_never_an_erase(self):
        plan = build_focus_plan(_grid(_row(due="", priority="M")),
                                _task(), {}, _snap())
        assert plan.task_updates == {}
        assert plan.counters.get("blanks_refused") == 1

    def test_an_unreadable_date_is_reported_not_guessed(self):
        """Pulling the literal string poisons a DATE column; treating it as
        blank silently discards what was typed."""
        plan = build_focus_plan(_grid(_row(due="next tuesday", priority="M")),
                                _task(), {}, _snap())
        assert plan.task_updates == {}
        assert plan.counters.get("unparsed_dates") == 1
        assert any("next tuesday" in c for c in plan.conflicts)

    def test_a_bad_priority_costs_the_cell_not_the_row(self):
        """A legitimate date edit on the same line must still land."""
        plan = build_focus_plan(_grid(_row(due="21/08/2026", priority="VERY")),
                                _task(), {}, _snap())
        assert plan.task_updates == {"t1": {"deadline": "2026-08-21"}}
        assert plan.counters.get("bad_priorities") == 1

    def test_a_manual_field_is_not_walked_back(self):
        """Rule 2 — a value a person decided elsewhere survives."""
        plan = build_focus_plan(
            _grid(_row(due="20/08/2026", priority="M")),
            _task(deadline="2026-09-09", manual_deadline=True), {}, _snap())
        assert plan.task_updates == {}
        assert plan.counters.get("manual_held") == 1

    def test_an_unchanged_row_writes_nothing(self):
        """No churn: the common case must be silent."""
        plan = build_focus_plan(_grid(_row(due="20/08/2026", priority="M")),
                                _task(), {}, _snap())
        assert plan.task_updates == {}
        assert plan.counters.get("pulled") is None

    def test_a_meeting_date_maps_to_proposed_date_not_deadline(self):
        """A task and a meeting keep their dates in different tables — which is
        why `_kind` is STORED on the row rather than read off the visible cell."""
        meetings = {"m1": {"id": "m1", "proposed_date": "2026-08-20",
                           "priority": "M", "status": "scheduled"}}
        plan = build_focus_plan(
            _grid(_row(kind=ROW_MEETING, uid="m1", due="25/08/2026", priority="M")),
            {}, meetings, _snap("m1", kind=ROW_MEETING))
        assert plan.meeting_updates == {"m1": {"proposed_date": "2026-08-25"}}
        assert plan.task_updates == {}


class TestIdentityIsStoredNotInferred:
    def test_a_row_with_no_uid_is_skipped(self):
        plan = build_focus_plan(_grid(_row(uid="", due="21/08/2026")),
                                _task(), {}, _snap())
        assert plan.task_updates == {}

    def test_a_row_with_an_unknown_kind_is_skipped(self):
        plan = build_focus_plan(_grid(_row(kind="banana", due="21/08/2026")),
                                _task(), {}, _snap())
        assert plan.task_updates == {}

    def test_the_chrome_rows_are_never_parsed(self):
        """Rows 1-4 hold the title, the dropdowns and the header. A control cell
        read as data would pull "All" into somebody's deadline."""
        plan = build_focus_plan(_grid(), _task(), {}, _snap())
        assert plan.snapshots == []


class TestDateFormats:
    @pytest.mark.parametrize("typed,iso", [
        ("2026-08-21", "2026-08-21"), ("21/08/2026", "2026-08-21"),
        ("21.08.2026", "2026-08-21"), ("21 Aug 2026", "2026-08-21"),
    ])
    def test_the_forms_people_actually_type(self, typed, iso):
        assert parse_sheet_date(typed) == iso

    def test_rubbish_is_refused(self):
        assert parse_sheet_date("next tuesday") is None
        assert parse_sheet_date("") is None
        assert parse_sheet_date(None) is None


class TestTheWiringReadsBeforeItRenders:
    """`refresh_focus` rewrites every cell from the database, so a refresh that
    ran first would erase the tick before anything looked at it — the close would
    vanish and never reach the DB. It matters more here than on the Timeline: the
    webhook fires the instant a box is ticked, so the render would overwrite it
    inside the same call."""

    async def test_the_sync_path_reads_first(self, monkeypatch):
        from unittest.mock import AsyncMock, patch
        from config.settings import settings
        import services.sheet_sync as ss

        monkeypatch.setattr(settings, "FOCUS_READBACK_ENABLED", True, raising=False)
        order = []
        with patch("processors.focus_readback.reconcile_focus",
                   AsyncMock(side_effect=lambda *a, **k: order.append("read") or {})), \
             patch("services.focus_sheet.refresh_focus",
                   AsyncMock(side_effect=lambda *a, **k: order.append("render") or {})):
            await ss._run_surface(ss.SURFACE_FOCUS)
        assert order == ["read", "render"]

    async def test_the_render_still_runs_when_the_readback_is_off(self, monkeypatch):
        """The tab must keep refreshing whether or not it is writable."""
        from unittest.mock import AsyncMock, patch
        from config.settings import settings
        import services.sheet_sync as ss

        monkeypatch.setattr(settings, "FOCUS_READBACK_ENABLED", False, raising=False)
        with patch("processors.focus_readback.reconcile_focus",
                   AsyncMock()) as read, \
             patch("services.focus_sheet.refresh_focus",
                   AsyncMock(return_value={})) as render:
            await ss._run_surface(ss.SURFACE_FOCUS)
        read.assert_not_awaited()
        render.assert_awaited_once()

    def test_the_scheduler_reads_before_it_renders_too(self):
        """Walked as an AST, never grepped. A text search matches the explanatory
        COMMENT above the call — which names refresh_focus first — and so failed
        on correct code. It would equally have PASSED on broken code that happened
        to be commented the other way round. This codebase has made that mistake
        before, which is why the rule is: never grep source text to prove a call
        site changed."""
        import ast
        import inspect
        import textwrap
        import schedulers.reconcile_scheduler as rs

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(rs.ReconcileScheduler._run_locked)))
        # SORTED BY LINE NUMBER. `ast.walk` is breadth-first, so its raw order
        # says nothing about which call runs first — the second way this one
        # test managed to measure something other than the thing it names.
        calls = [name for _ln, name in sorted(
            (n.lineno, n.func.id) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name))]
        assert "reconcile_focus" in calls, "the readback is not wired in at all"
        assert "refresh_focus" in calls
        assert calls.index("reconcile_focus") < calls.index("refresh_focus"),             "the readback must be CALLED before the render"


class TestItShipsSafe:
    def test_both_flags_default_off_and_shadowed(self):
        """This is a FOURTH writer on tasks.deadline, and the three existing ones
        produced every cross-surface defect of 2026-08."""
        from config.settings import Settings
        s = Settings.model_fields
        assert s["FOCUS_READBACK_ENABLED"].default is False
        assert s["FOCUS_SHADOW_MODE"].default is True

    async def test_shadow_mode_writes_nothing(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        from config.settings import settings
        import processors.focus_readback as fr

        monkeypatch.setattr(settings, "FOCUS_SHADOW_MODE", True, raising=False)
        monkeypatch.setattr(settings, "PROJECT_STATUS_SHEET_ID", "x", raising=False)
        grid = _grid(_row(done=True))
        svc = MagicMock()
        svc._execute_with_retry.side_effect = lambda f: f()
        svc.service.spreadsheets.return_value.values.return_value.get\
            .side_effect = lambda **k: {"values": grid}
        sc = MagicMock()
        sc.client.table.return_value.select.return_value.limit.return_value\
            .execute.return_value.data = []
        sc.get_focus_snapshots.return_value = {}
        with patch.dict("sys.modules",
                        {"services.google_sheets": MagicMock(sheets_service=svc)}), \
             patch("services.supabase_client.supabase_client", sc):
            out = await fr.reconcile_focus()
        assert out["shadow"] is True
        sc.update_task.assert_not_called()
        sc.upsert_focus_snapshot.assert_not_called()

    async def test_an_empty_read_changes_nothing(self, monkeypatch):
        """A transient read returns empty without raising."""
        from unittest.mock import MagicMock, patch
        from config.settings import settings
        import processors.focus_readback as fr

        monkeypatch.setattr(settings, "PROJECT_STATUS_SHEET_ID", "x", raising=False)
        svc = MagicMock()
        svc._execute_with_retry.side_effect = lambda f: f()
        svc.service.spreadsheets.return_value.values.return_value.get\
            .side_effect = lambda **k: {"values": []}
        with patch.dict("sys.modules",
                        {"services.google_sheets": MagicMock(sheets_service=svc)}):
            out = await fr.reconcile_focus()
        assert out == {"skipped": "empty read"}


class TestTheSnapshotUsesTheRightColumn:
    """`sheet_snapshots` has no `due` column — a task mirrors `deadline`, a
    meeting `proposed_date`. Writing `due` raised PGRST204 into a broad except,
    so no base was ever written and "no merge base means no edit" would have
    refused every edit on this tab, silently, forever."""

    def test_a_task_snapshot_writes_deadline(self):
        from unittest.mock import MagicMock, patch
        from services.supabase_client import supabase_client as sc
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq\
            .return_value.execute.return_value.data = []
        with patch.object(type(sc), "client", property(lambda self: client)):
            sc.upsert_focus_snapshot("task", "t1", due="2026-08-20")
        payload = client.table.return_value.insert.call_args[0][0]
        assert payload["deadline"] == "2026-08-20"
        assert "due" not in payload
        assert payload["entity_type"] == "focus_task"

    def test_a_meeting_snapshot_writes_proposed_date(self):
        from unittest.mock import MagicMock, patch
        from services.supabase_client import supabase_client as sc
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.eq\
            .return_value.execute.return_value.data = []
        with patch.object(type(sc), "client", property(lambda self: client)):
            sc.upsert_focus_snapshot("meeting", "m1", due="2026-08-20")
        payload = client.table.return_value.insert.call_args[0][0]
        assert payload["proposed_date"] == "2026-08-20"
        assert payload["entity_type"] == "focus_meeting"
