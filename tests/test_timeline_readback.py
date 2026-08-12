"""Reading the Timeline back — the merge rule, and everything it refuses to do.

Phase 4b of docs/GANTT_V2_PLAN.md. This is the phase that puts a second writer
near live rows, so most of these tests assert a REFUSAL rather than an action:
no merge base means no edit, a blank is never an erase, an unreadable date is
never a value, and two surfaces disagreeing is a question rather than a merge.
"""
from unittest.mock import MagicMock, patch

import pytest

from processors.timeline_readback import (
    EDITABLE, build_plan, layout_ok, parse_sheet_date,
)
from processors.timeline_view import (
    HEADERS, HIDDEN_HEADERS, N_HIDDEN, N_LABEL_COLS, ROW_ARCHIVE, ROW_AREA,
    ROW_CHROME, ROW_PROJECT, ROW_TASK,
)

N_WEEKS = 96
N_COLS = N_LABEL_COLS + N_WEEKS + N_HIDDEN


def _header():
    row = [""] * N_COLS
    row[:N_LABEL_COLS] = HEADERS
    row[-N_HIDDEN:] = HIDDEN_HEADERS
    return row


def _row(kind, uid="", owner="", start="", target=""):
    row = [""] * N_COLS
    row[1], row[2], row[3] = owner, start, target
    row[-N_HIDDEN], row[-N_HIDDEN + 1] = uid, kind
    return row


def _grid(*rows):
    """Chrome rows 0-2, header at index 3, body after — the real shape."""
    return [_row(ROW_CHROME), _row(ROW_CHROME), _row(ROW_CHROME),
            _header(), *rows]


def _project(pid="p1", **kw):
    base = {"id": pid, "name": "Legal", "owner": "Eyal",
            "start_date": "2026-03-02", "target_date": None}
    base.update(kw)
    return {pid: base}


def _snap(pid="p1", **kw):
    base = {"canonical_project_id": pid, "owner": "Eyal",
            "start_date": "2026-03-02", "target_date": None}
    base.update(kw)
    return {pid: base}


class TestLayoutGate:
    def test_the_expected_layout_passes(self):
        assert layout_ok(_header())

    def test_a_tab_on_an_older_shape_is_refused(self):
        """Parsing it anyway resolves the wrong column onto the wrong field and
        writes confident nonsense on every row of every cycle."""
        bad = _header()
        bad[2] = "Deadline"          # was "Start"
        assert not layout_ok(bad)

    def test_a_tab_without_the_identity_columns_is_refused(self):
        assert not layout_ok(HEADERS + [""] * N_WEEKS)

    def test_a_mismatched_layout_changes_nothing(self):
        bad = _header()
        bad[0] = "Something else"
        grid = [_row(ROW_CHROME)] * 3 + [bad, _row(ROW_PROJECT, "p1", start="2026-05-05")]
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == [] and plan.cell_writes == []
        assert plan.counters.get("layout_mismatch") == 1


class TestRule1Pull:
    def test_a_typed_start_date_is_pulled_and_marked_manual(self):
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Eyal", start="2026-05-05"))
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == [("p1", {"start_date": "2026-05-05"})]
        assert ("p1", "start_date") in plan.manual_marks

    def test_a_typed_owner_is_pulled(self):
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Roye", start="2026-03-02"))
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == [("p1", {"owner": "Roye"})]

    @pytest.mark.parametrize("typed,iso", [
        ("2026-05-05", "2026-05-05"), ("5/5/2026", "2026-05-05"),
        ("05.05.2026", "2026-05-05"), ("5 May 2026", "2026-05-05"),
    ])
    def test_the_forms_a_person_actually_types_are_understood(self, typed, iso):
        assert parse_sheet_date(typed) == iso

    def test_a_sloppy_date_is_canonicalised_in_the_same_cycle(self):
        """Otherwise the answer to "does it know what I meant?" arrives thirty
        minutes after the question."""
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Eyal", start="5/5/2026"))
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == [("p1", {"start_date": "2026-05-05"})]
        assert plan.cell_writes == [(5, 2, "2026-05-05")]


class TestNoMergeBaseNoEdit:
    def test_a_row_with_no_snapshot_is_never_pulled(self):
        """With snap == {} every populated cell differs from None, so the whole
        row would be pulled in and frozen as human decisions. The sibling rail
        shipped without this guard — found in the 2026-08-08 review."""
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Roye", start="2026-05-05"))
        plan = build_plan(grid, _project(), snaps={})
        assert plan.updates == []
        assert plan.manual_marks == []

    def test_without_a_base_the_db_is_still_pushed_to_the_sheet(self):
        """Refusing to PULL is not refusing to render."""
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Roye", start="2026-05-05"))
        plan = build_plan(grid, _project(), snaps={})
        assert plan.counters.get("pushed")


class TestBlanksAreNeverErasures:
    def test_clearing_a_start_date_is_refused_and_counted(self):
        """Clearing a start date removes the bar's left edge entirely — far more
        likely a slip than a decision, and Rule 1 would freeze it as manual."""
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Eyal", start=""))
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == []
        assert plan.counters.get("blanks_refused") == 1

    def test_clearing_an_owner_is_refused(self):
        grid = _grid(_row(ROW_PROJECT, "p1", owner="", start="2026-03-02"))
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == []
        assert plan.counters.get("blanks_refused") == 1


class TestUnreadableDates:
    def test_an_unparseable_date_is_neither_pulled_nor_blanked(self):
        """Pulling the literal string poisons a DATE column; treating it as
        blank silently discards what was typed."""
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Eyal", start="next Tuesday-ish"))
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == []
        assert plan.counters.get("unparsed_dates") == 1
        assert any("cannot read" in c for c in plan.conflicts)

    def test_it_is_surfaced_rather_than_swallowed(self):
        grid = _grid(_row(ROW_PROJECT, "p1", start="garbage"))
        plan = build_plan(grid, _project(), _snap())
        assert plan.conflicts, "an unreadable cell must be reported"


class TestRule2Hold:
    def test_a_manual_db_value_is_not_overwritten_by_the_render(self):
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Eyal", start="2026-03-02"))
        projects = _project(start_date="2026-01-05", manual_start_date=True)
        plan = build_plan(grid, projects, _snap())
        assert plan.updates == []
        assert plan.counters.get("manual_held") == 1
        assert not any(cw[1] == 2 for cw in plan.cell_writes)


class TestRule4Push:
    def test_a_stale_sheet_cell_is_refreshed_from_the_db(self):
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Eyal", start="2026-03-02"))
        projects = _project(start_date="2026-04-01")
        plan = build_plan(grid, projects, _snap(start_date="2026-03-02"))
        # sheet == snapshot, so no human edit; the DB moved and wins.
        assert plan.updates == []
        assert (5, 2, "2026-04-01") in plan.cell_writes


class TestConflictsAreReported:
    def test_two_surfaces_disagreeing_writes_neither(self):
        """Both sheets carry a human intention and nothing here can rank them.
        Two people disagreeing is a question, not a merge."""
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Roye", start="2026-03-02"))
        others = ({"p1": {"owner": "Paolo"}},)
        plan = build_plan(grid, _project(), _snap(), others)
        assert plan.updates == []
        assert plan.counters.get("conflicts_held") == 1
        assert any("another surface" in c for c in plan.conflicts)

    def test_agreement_across_surfaces_is_not_a_conflict(self):
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Roye", start="2026-03-02"))
        others = ({"p1": {"owner": "Roye"}},)
        plan = build_plan(grid, _project(), _snap(), others)
        assert plan.updates == [("p1", {"owner": "Roye"})]


class TestOnlyProjectRows:
    @pytest.mark.parametrize("kind", [ROW_TASK, ROW_AREA, ROW_ARCHIVE, ROW_CHROME])
    def test_no_other_row_kind_is_ever_read(self, kind):
        """Task rows especially: tasks.deadline and tasks.assignee are edited
        daily on the area tabs, and a second writer there is the collision every
        2026-08 cross-surface defect came from."""
        grid = _grid(_row(kind, "p1", owner="Roye", start="2026-05-05"))
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == [] and plan.cell_writes == []

    def test_a_project_row_without_a_uid_is_skipped_not_guessed(self):
        """Falling back to name matching here is the untrusted step this whole
        plan works around."""
        grid = _grid(_row(ROW_PROJECT, "", owner="Roye", start="2026-05-05"))
        plan = build_plan(grid, _project(), _snap())
        assert plan.updates == []
        assert plan.counters.get("rows_without_uid") == 1

    def test_only_three_fields_are_editable(self):
        """Renaming a project from a Gantt row is a different decision, and
        column 4 shows a status derived elsewhere."""
        assert set(EDITABLE.values()) == {"owner", "start_date", "target_date"}
        assert 0 not in EDITABLE and 4 not in EDITABLE


class TestShadowMode:
    async def _run(self, shadow, grid):
        import processors.timeline_readback as tr

        svc = MagicMock()
        svc._execute_with_retry.side_effect = lambda f: f()
        sheets = svc.service.spreadsheets.return_value
        sheets.values.return_value.get.side_effect = \
            lambda **kw: {"values": grid}
        writes = []
        sheets.values.return_value.batchUpdate.side_effect = \
            lambda **kw: writes.append(kw)

        db_writes = []

        def _table(name):
            t = MagicMock()
            t.select.return_value = t
            t.eq.return_value = t
            t.execute.return_value = MagicMock(
                data=[{"id": "p1", "name": "Legal", "owner": "Eyal",
                       "start_date": "2026-03-02", "target_date": None}])
            t.update.side_effect = lambda payload: db_writes.append(payload) or t
            return t

        client = MagicMock()
        client.table.side_effect = _table

        marked = []
        with patch.dict("sys.modules", {"services.google_sheets":
                                        MagicMock(sheets_service=svc)}), \
             patch.object(type(tr.supabase_client), "client",
                          property(lambda self: client)), \
             patch.object(tr.supabase_client, "get_timeline_snapshots",
                          return_value=_snap()), \
             patch.object(tr.supabase_client, "get_ps_project_snapshots",
                          return_value={}), \
             patch.object(tr.supabase_client, "mark_project_field_manual",
                          side_effect=lambda *a, **k: marked.append(a)), \
             patch.object(tr.supabase_client, "upsert_timeline_snapshot"), \
             patch.object(tr.settings, "PROJECT_STATUS_SHEET_ID", "ssid"), \
             patch.object(tr.settings, "TIMELINE_SHADOW_MODE", shadow):
            out = await tr.reconcile_timeline()
        return out, db_writes, writes, marked

    async def test_shadow_computes_the_same_plan_but_writes_nothing(self):
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Eyal", start="2026-05-05"))
        out, db_writes, sheet_writes, marked = await self._run(True, grid)
        assert out["pulled"] == 1, "shadow must still compute the plan"
        assert out["shadow"] is True
        assert db_writes == [] and sheet_writes == [] and marked == []

    async def test_live_applies_the_plan(self):
        grid = _grid(_row(ROW_PROJECT, "p1", owner="Eyal", start="2026-05-05"))
        out, db_writes, _, marked = await self._run(False, grid)
        assert out["shadow"] is False
        assert db_writes == [{"start_date": "2026-05-05"}]
        assert marked and marked[0][1] == "start_date"

    async def test_an_empty_read_changes_nothing(self):
        """A transient read can return empty without raising, and treating that
        as "every row was deleted" would be catastrophic."""
        out, db_writes, sheet_writes, _ = await self._run(False, [])
        assert out == {"skipped": "empty read"}
        assert db_writes == [] and sheet_writes == []
