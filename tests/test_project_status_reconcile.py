"""The Project Status reconcile engine.

Two groups here carry most of the weight:

`TestNeverTouchesHerRows` is the mechanical form of the whole design promise —
the system only ever writes into lines it authored. If that ever breaks, the
file stops being safe to work in, whatever else passes.

`TestDestructiveInput` encodes "a mistake in the sheet cannot destroy data": a
bad read, a bulk delete, a 200-row paste. Those are the cases where a merge
engine does real damage, and they are the ones nobody exercises by hand.
"""

from unittest.mock import MagicMock, patch

import pytest

from processors import project_status_reconcile as psr
from services.project_status_rows import (
    ALL_HEADERS, FIRST_BODY_ROW, KIND_ACTION, KIND_PROJECT,
)

TAB = "PRODUCT & TECHNOLOGY"
COLS = {name: i for i, name in enumerate(ALL_HEADERS)}


def _grid(*body):
    return [["Area", "", "", "Confidential"], ["Distribution:", "", "", ""],
            list(ALL_HEADERS), *body]


def _prow(uid="p1", name="Product V1", num=10, todo="", date="", resp="", notes=""):
    return [num, name, "", todo, date, resp, notes, KIND_PROJECT, uid, uid, "", ""]


def _arow(uid="t1", parent="p1", action="Ship the API", subject="", date="",
          resp="", notes="", checked=False):
    return [checked, subject, action, "", date, resp, notes, KIND_ACTION, uid,
            parent, "auto", ""]


def _human(action="Call the bank", subject="", date="", resp="", notes=""):
    return ["", subject, action, "", date, resp, notes]


def _db_task(tid="t1", **kw):
    row = {"id": tid, "title": "Ship the API", "status": "pending",
           "deadline": None, "assignee": "", "notes": None, "label": None,
           "project_id": "p1", "ps_suppressed": False}
    row.update(kw)
    return row


def _db_project(pid="p1", **kw):
    row = {"id": pid, "name": "Product V1", "objective": None,
           "target_date": None, "owner": None, "notes": None, "status": "active"}
    row.update(kw)
    return row


def _plan(grids, tasks=None, projects=None, act_snaps=None, proj_snaps=None,
          roster=None):
    """Build a plan against a mocked database."""
    sb = MagicMock()
    sb.get_canonical_projects.return_value = projects if projects is not None else [_db_project()]
    sb.get_ps_tasks.return_value = tasks if tasks is not None else [_db_task()]
    sb.get_ps_project_snapshots.return_value = proj_snaps or {}
    sb.get_sheet_snapshots.return_value = act_snaps or {}
    sb.list_team_members.return_value = roster or [{"name": "Eyal Zror"}]
    sb.resolve_assignee.side_effect = lambda v, roster=None: (
        "Eyal Zror" if str(v).strip().lower() in ("eyal", "eyal zror") else v)
    with patch.object(psr, "supabase_client", sb):
        return psr.build_plan(grids)


class TestIdleCycle:
    def test_untouched_sheet_changes_nothing(self):
        """The property the whole design rests on: at rest, do nothing."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Eyal Zror"))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending"}},
            proj_snaps={"p1": {}},
        )
        assert plan.cell_writes == [] and plan.task_updates == {}
        assert plan.project_updates == {} and plan.creates == []
        assert plan.suppress == []

    def test_assignee_shorthand_is_not_a_divergence(self):
        """'Eyal' and 'Eyal Zror' are one person — comparing raw strings made
        the Tasks reconcile report a phantom hold every 30 minutes forever."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Eyal"))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal",
                              "status": "pending"}},
        )
        assert plan.cell_writes == [] and plan.counters["pulled"] == 0


class TestMergeRules:
    def test_rule1_edit_is_pulled_and_marked_sticky(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Roye Tadmor"))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending"}},
        )
        assert plan.task_updates["t1"]["assignee"] == "Roye Tadmor"
        assert ("task", "t1", "assignee") in plan.manual_marks
        assert plan.counters["pulled"] == 1

    def test_rule2_sticky_field_is_held_not_reverted(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Roye Tadmor"))},
            tasks=[_db_task(assignee="Eyal Zror", manual_assignee=True)],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Roye Tadmor",
                              "status": "pending"}},
        )
        assert plan.cell_writes == []
        assert plan.counters["manual_held"] == 1

    def test_rule4_db_advance_refreshes_the_cell(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Eyal Zror"))},
            tasks=[_db_task(assignee="Roye Tadmor")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending"}},
        )
        assert plan.counters["pushed"] == 1
        tab, row, col, value = plan.cell_writes[0]
        assert col == COLS["Resp."] and value == "Roye Tadmor"

    def test_project_objective_is_pulled(self):
        plan = _plan(
            {TAB: _grid(_prow(todo="Win Lombardy"))},
            tasks=[], proj_snaps={"p1": {"objective": None}},
        )
        assert plan.project_updates["p1"]["objective"] == "Win Lombardy"
        assert ("project", "p1", "objective") in plan.manual_marks


class TestDates:
    def test_unparseable_date_is_never_pulled(self):
        """A typo must not be able to null a real deadline."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(date="whenever"))},
            tasks=[_db_task(deadline="2026-08-12")],
            act_snaps={"t1": {"title": "Ship the API", "deadline": "2026-08-12",
                              "status": "pending"}},
        )
        assert "deadline" not in plan.task_updates.get("t1", {})
        assert plan.counters["bad_dates"] == 1

    def test_unparseable_date_cell_is_left_verbatim(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(date="whenever"))},
            tasks=[_db_task(deadline="2026-08-12")],
            act_snaps={"t1": {"title": "Ship the API", "deadline": "2026-08-12",
                              "status": "pending"}},
        )
        assert not [w for w in plan.cell_writes if w[2] == COLS["Date"]]

    def test_equivalent_date_spellings_are_equal(self):
        """12/08/2026 in the cell, 2026-08-12 in the DB — the same day."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(date="12/08/2026"))},
            tasks=[_db_task(deadline="2026-08-12")],
            act_snaps={"t1": {"title": "Ship the API", "deadline": "2026-08-12",
                              "status": "pending"}},
        )
        assert plan.cell_writes == [] and plan.counters["pulled"] == 0

    def test_sloppy_date_is_rewritten_to_the_canonical_form(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(date="12/8/2026"))},
            tasks=[_db_task(deadline="2026-08-12")],
            act_snaps={"t1": {"title": "Ship the API", "deadline": "2026-08-12",
                              "status": "pending"}},
        )
        assert plan.counters["normalized_dates"] == 1
        assert plan.cell_writes[0][3] == "12/08/2026"
        assert plan.overrides                     # always reported


class TestBlanks:
    def test_a_cleared_title_never_nulls_the_task(self):
        """Clearing a cell is how a human tidies a view, not an instruction to
        erase the task's text."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(action=""))},
            tasks=[_db_task(title="Ship the API")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert "title" not in plan.task_updates.get("t1", {})

    def test_a_cleared_label_never_nulls_it(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(subject=""))},
            tasks=[_db_task(label="AWS Setup")],
            act_snaps={"t1": {"title": "Ship the API", "label": "AWS Setup",
                              "status": "pending"}},
        )
        assert "label" not in plan.task_updates.get("t1", {})


class TestCheckbox:
    def test_tick_marks_the_task_done_and_sticky(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(checked=True))},
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.task_updates["t1"]["status"] == "done"
        assert ("task", "t1", "status") in plan.manual_marks
        assert plan.counters["ticked"] == 1

    def test_untick_returns_it_to_pending(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(checked=False))},
            tasks=[_db_task(status="done")],
            act_snaps={"t1": {"title": "Ship the API", "status": "done"}},
        )
        assert plan.task_updates["t1"]["status"] == "pending"
        assert plan.counters["unticked"] == 1

    def test_an_already_done_row_is_not_re_ticked_every_cycle(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(checked=True))},
            tasks=[_db_task(status="done")],
            act_snaps={"t1": {"title": "Ship the API", "status": "done"}},
        )
        assert "status" not in plan.task_updates.get("t1", {})


class TestReparent:
    def test_dragging_a_row_into_another_block_repoints_it(self):
        plan = _plan(
            {TAB: _grid(_prow(uid="p1"), _prow(uid="p2", name="Cloud", num=20),
                        _arow(uid="t1", parent="p1"))},
            projects=[_db_project("p1"), _db_project("p2", name="Cloud")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.task_updates["t1"]["project_id"] == "p2"
        assert plan.counters["reparented"] == 1

    def test_a_row_in_its_own_block_is_not_reparented(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow())},
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.counters["reparented"] == 0


class TestCreates:
    def test_a_typed_action_becomes_a_task_under_its_project(self):
        plan = _plan({TAB: _grid(_prow(), _human(action="Call the bank",
                                                 resp="Paolo", date="12/08/2026"))},
                     tasks=[])
        create = plan.creates[0]
        assert create["kind"] == "task" and create["project_id"] == "p1"
        assert create["title"] == "Call the bank"
        assert create["deadline"] == "2026-08-12"

    def test_a_subject_with_no_action_becomes_a_project(self):
        plan = _plan({TAB: _grid(["", "New Vertical", "", "Break in", "", "", ""])},
                     tasks=[])
        assert plan.creates[0]["kind"] == "project"
        assert plan.creates[0]["name"] == "New Vertical"

    def test_an_incomplete_row_is_counted_never_written(self):
        """Date and owner but no Action — a task with no title is worse than none."""
        plan = _plan({TAB: _grid(_prow(), _human(action="", resp="Paolo",
                                                 date="12/08/2026"))}, tasks=[])
        assert plan.creates == [] and plan.counters["incomplete"] == 1

    def test_an_orphan_action_is_surfaced_not_dropped(self):
        """Typed above the first project row. Losing a line she typed because it
        sat in the wrong place would be the worst possible behaviour."""
        plan = _plan({TAB: _grid(_human(action="Stray"), _prow())}, tasks=[])
        assert plan.counters["orphans"] == 1
        assert [c["title"] for c in plan.creates] == ["Stray"]

    def test_an_unknown_name_raises_a_proposal_and_still_creates(self):
        plan = _plan({TAB: _grid(_prow(), _human(action="Call", resp="Dana Levi"))},
                     tasks=[])
        assert plan.person_proposals == ["Dana Levi"]
        assert plan.creates[0]["assignee"] == "Dana Levi"

    def test_a_known_name_raises_no_proposal(self):
        plan = _plan({TAB: _grid(_prow(), _human(action="Call", resp="Eyal"))},
                     tasks=[])
        assert plan.person_proposals == []


class TestSuppression:
    def test_a_deleted_row_suppresses_the_view_not_the_task(self):
        plan = _plan(
            {TAB: _grid(_prow())},
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.suppress == ["t1"]
        assert plan.task_updates == {}          # the task itself is untouched

    def test_a_row_still_present_is_not_suppressed(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow())},
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.suppress == []


class TestDestructiveInput:
    def test_an_empty_tab_with_snapshots_is_skipped_entirely(self):
        """A transient Sheets read returns an empty tab WITHOUT raising.
        Treating that as 'everything was deleted' would empty the review."""
        plan = _plan({TAB: []}, act_snaps={"t1": {"status": "pending"}},
                     proj_snaps={"p1": {"sheet_tab": TAB}})
        assert plan.skipped_tabs == [TAB]
        assert plan.suppress == [] and plan.task_updates == {}

    def test_a_genuinely_new_empty_tab_is_not_an_abort(self):
        plan = _plan({TAB: []}, tasks=[], proj_snaps={})
        assert plan.skipped_tabs == []

    def test_bulk_delete_trips_the_cap_and_suppresses_nothing(self):
        snaps = {f"t{i}": {"status": "pending"} for i in range(30)}
        tasks = [_db_task(f"t{i}") for i in range(30)]
        plan = _plan({TAB: _grid(_prow())}, tasks=tasks, act_snaps=snaps)
        assert plan.suppress == []

    def test_a_pasted_block_leaves_the_duplicates_alone(self):
        """A pasted row carries its source's uid. The topmost keeps the
        identity; acting on both would apply one row's edits to the other."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(uid="t1", resp="Roye Tadmor"),
                        _arow(uid="t1", resp="Someone Else"))},
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending"}},
        )
        assert plan.counters["dup_uids"] == 1
        assert plan.task_updates["t1"]["assignee"] == "Roye Tadmor"

    def test_a_uid_unknown_to_the_database_is_left_alone(self):
        plan = _plan({TAB: _grid(_prow(), _arow(uid="ghost"))},
                     act_snaps={})
        assert plan.counters["ghosts"] == 1
        assert plan.task_updates == {} and plan.cell_writes == []


class TestNeverTouchesHerRows:
    """The mechanical form of "the system only writes lines it authored"."""

    def test_no_write_lands_on_a_human_row(self):
        grid = _grid(
            _prow(),
            _arow(uid="t1", resp="Eyal Zror"),
            _human(action="Her own line", resp="Paolo", notes="hers"),
            _human(action="Another", date="12/08/2026"),
        )
        plan = _plan({TAB: grid}, tasks=[_db_task(assignee="Roye Tadmor")],
                     act_snaps={"t1": {"title": "Ship the API",
                                       "assignee": "Eyal Zror",
                                       "status": "pending"}})
        human_rows = {FIRST_BODY_ROW + 2, FIRST_BODY_ROW + 3}
        touched = {row for _, row, _, _ in plan.cell_writes}
        assert not (touched & human_rows), f"wrote into {touched & human_rows}"

    def test_her_rows_are_never_updated_only_created_from(self):
        plan = _plan({TAB: _grid(_prow(), _human(action="Hers"))}, tasks=[])
        assert plan.task_updates == {} and plan.project_updates == {}
        assert len(plan.creates) == 1

    def test_a_human_row_next_to_a_system_row_does_not_borrow_its_identity(self):
        plan = _plan({TAB: _grid(_prow(), _arow(uid="t1"), _human(action="Hers"))},
                     act_snaps={"t1": {"title": "Ship the API",
                                       "status": "pending"}})
        assert plan.creates[0]["title"] == "Hers"
        assert plan.suppress == []


class TestShadow:
    @pytest.mark.asyncio
    async def test_shadow_writes_nothing(self):
        with patch.object(psr, "_read_tabs",
                          return_value={TAB: _grid(_prow(), _arow(resp="X"))}), \
             patch.object(psr, "_apply") as apply_fn, \
             patch.object(psr, "supabase_client") as sb, \
             patch.object(psr.settings, "PROJECT_STATUS_SHEET_ID", "sid"):
            sb.get_canonical_projects.return_value = [_db_project()]
            sb.get_ps_tasks.return_value = [_db_task()]
            sb.get_ps_project_snapshots.return_value = {}
            sb.get_sheet_snapshots.return_value = {}
            sb.list_team_members.return_value = []
            sb.resolve_assignee.side_effect = lambda v, roster=None: v
            result = await psr.reconcile_project_status(shadow=True)

        apply_fn.assert_not_called()
        assert result["shadow"] is True

    @pytest.mark.asyncio
    async def test_missing_sheet_id_is_an_error_not_a_crash(self):
        with patch.object(psr.settings, "PROJECT_STATUS_SHEET_ID", ""):
            result = await psr.reconcile_project_status(shadow=True)
        assert result["error"]


class TestSkippedTabIsolation:
    """A bad read on one tab must protect that tab without freezing the others."""

    OTHER = "SALES & BUSINESS DEVELOPMENT"

    def test_a_skipped_tabs_rows_are_never_suppressed(self):
        """Without this the guard announces it protected the tab and then lets
        the damage through the back door — suppression runs over ALL snapshots."""
        plan = _plan(
            {TAB: [], self.OTHER: _grid(_prow(uid="p2", name="Italy"))},
            projects=[_db_project("p1"), _db_project("p2", name="Italy")],
            tasks=[_db_task("t1"), _db_task("t2", project_id="p2")],
            act_snaps={"t1": {"status": "pending", "sheet_tab": TAB},
                       "t2": {"status": "pending", "sheet_tab": self.OTHER}},
            proj_snaps={"p1": {"sheet_tab": TAB}, "p2": {"sheet_tab": self.OTHER}},
        )
        assert plan.skipped_tabs == [TAB]
        assert "t1" not in plan.suppress          # protected
        assert "t2" in plan.suppress              # the readable tab still works

    def test_unknown_provenance_is_protected_while_anything_is_skipped(self):
        plan = _plan(
            {TAB: [], self.OTHER: _grid(_prow(uid="p2", name="Italy"))},
            projects=[_db_project("p1"), _db_project("p2", name="Italy")],
            tasks=[_db_task("t3")],
            act_snaps={"t3": {"status": "pending"}},   # no sheet_tab recorded
            proj_snaps={"p1": {"sheet_tab": TAB}},
        )
        assert plan.suppress == []


class TestSlotGate:
    """Rows must never shift under a live editor."""

    def test_the_interval_tick_is_never_structural(self):
        with patch.object(psr.settings, "PROJECT_STATUS_STRUCTURAL_SLOTS",
                          "prenightly,predigest"):
            assert psr.structural_allowed("2026-08-07-1748:interval") is False

    def test_the_quiet_slots_are(self):
        with patch.object(psr.settings, "PROJECT_STATUS_STRUCTURAL_SLOTS",
                          "prenightly,predigest"):
            assert psr.structural_allowed("2026-08-07:prenightly") is True
            assert psr.structural_allowed("2026-08-07:predigest") is True

    def test_empty_setting_disables_structural_entirely(self):
        with patch.object(psr.settings, "PROJECT_STATUS_STRUCTURAL_SLOTS", ""):
            assert psr.structural_allowed("2026-08-07:prenightly") is False

    def test_no_slot_is_not_structural(self):
        with patch.object(psr.settings, "PROJECT_STATUS_STRUCTURAL_SLOTS",
                          "prenightly"):
            assert psr.structural_allowed(None) is False


class TestAutoInject:
    def _inject(self, tasks, blocks_grid, act_snaps=None, **flags):
        settings_patches = {"PROJECT_STATUS_AUTO_INJECT_ENABLED": True, **flags}
        ctx = [patch.object(psr.settings, k, v) for k, v in settings_patches.items()]
        for c in ctx:
            c.start()
        try:
            return _plan({TAB: blocks_grid}, tasks=tasks, act_snaps=act_snaps or {})
        finally:
            for c in ctx:
                c.stop()

    def test_a_new_task_is_appended_to_its_project_block(self):
        plan = self._inject([_db_task("t9", title="Brand new")], _grid(_prow()))
        assert len(plan.injects) == 1
        tab, anchor, rows = plan.injects[0]
        assert anchor == FIRST_BODY_ROW + 1          # just after the project row
        assert rows[0][8] == "t9" and rows[0][9] == "p1"
        assert rows[0][0] is False                   # a real checkbox

    def test_a_task_already_on_the_sheet_is_not_re_injected(self):
        plan = self._inject([_db_task("t1")], _grid(_prow(), _arow(uid="t1")),
                            act_snaps={"t1": {"status": "pending"}})
        assert plan.injects == []

    def test_a_suppressed_task_is_never_re_injected(self):
        """Otherwise deleting a row puts her in a resurrection loop."""
        plan = self._inject([_db_task("t9", ps_suppressed=True)], _grid(_prow()))
        assert plan.injects == []

    def test_a_closed_task_is_not_injected(self):
        plan = self._inject([_db_task("t9", status="done")], _grid(_prow()))
        assert plan.injects == []

    def test_a_task_with_no_project_is_not_injected(self):
        plan = self._inject([_db_task("t9", project_id=None)], _grid(_prow()))
        assert plan.injects == []

    def test_the_per_cycle_cap_holds(self):
        tasks = [_db_task(f"t{i}", title=f"New {i}") for i in range(20)]
        plan = self._inject(tasks, _grid(_prow()),
                            PROJECT_STATUS_MAX_AUTO_PER_PROJECT=3)
        assert sum(len(r) for _, _, r in plan.injects) == 3

    def test_a_full_block_takes_nothing_more(self):
        rows = [_arow(uid=f"t{i}") for i in range(6)]
        snaps = {f"t{i}": {"status": "pending"} for i in range(6)}
        plan = self._inject([_db_task(f"t{i}") for i in range(6)] + [_db_task("t99")],
                            _grid(_prow(), *rows), act_snaps=snaps,
                            PROJECT_STATUS_MAX_ACTIONS_PER_PROJECT=6)
        assert plan.injects == []

    def test_injection_is_off_by_default(self):
        plan = _plan({TAB: _grid(_prow())}, tasks=[_db_task("t9")])
        assert plan.injects == []

    def test_a_skipped_tab_gets_no_injections(self):
        plan = self._inject([_db_task("t9")], [], act_snaps={"t1": {}})
        assert plan.injects == []


class TestProjectNameIsSystemOwned:
    """Renaming belongs on the Projects tab, which has its own snapshot rail and
    backfills `label` across five tables. Letting the Project Status sheet rename
    too would give a consequential operation two masters.

    Before this the Subject cell on a project row was neither pulled nor pushed,
    so a stray edit diverged from the database silently and forever.
    """

    def test_a_renamed_project_cell_is_refreshed_from_the_database(self):
        plan = _plan(
            {TAB: _grid(_prow(name="Prodct V1"))},          # typo in the sheet
            tasks=[], proj_snaps={"p1": {}},
        )
        writes = [w for w in plan.cell_writes if w[2] == COLS["Subject"]]
        assert writes and writes[0][3] == "Product V1"
        assert plan.counters["names_refreshed"] == 1

    def test_the_name_is_never_pulled_into_the_database(self):
        plan = _plan(
            {TAB: _grid(_prow(name="Something Else"))},
            tasks=[], proj_snaps={"p1": {}},
        )
        assert "name" not in plan.project_updates.get("p1", {})

    def test_a_matching_name_is_left_alone(self):
        plan = _plan({TAB: _grid(_prow())}, tasks=[], proj_snaps={"p1": {}})
        assert [w for w in plan.cell_writes if w[2] == COLS["Subject"]] == []
        assert plan.counters["names_refreshed"] == 0

    def test_an_action_rows_subject_is_still_the_topic_and_still_pulled(self):
        """Only the PROJECT row's Subject is system-owned; on an action row it
        is the topic and remains hers to set."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(subject="AWS Setup"))},
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.task_updates["t1"]["label"] == "AWS Setup"


class TestUnknownNamesOnEdits:
    """Typing an unknown name onto a row that already exists is the COMMON
    gesture in a review — far more common than adding a whole new row. It used
    to pull the name into the database and raise nothing at all."""

    def test_an_unknown_name_edited_onto_an_existing_row_proposes_a_person(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Ayala"))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending"}},
        )
        assert plan.task_updates["t1"]["assignee"] == "Ayala"
        assert plan.person_proposals == ["Ayala"]

    def test_an_unknown_owner_on_a_project_row_proposes_too(self):
        # A real snapshot row always carries snapshot_at; `{}` means NO merge
        # base, which the engine deliberately refuses to read as an edit.
        plan = _plan(
            {TAB: _grid(_prow(resp="Ayala"))},
            tasks=[], proj_snaps={"p1": {"owner": None,
                                         "snapshot_at": "2026-08-08T10:00:00Z"}},
        )
        assert plan.person_proposals == ["Ayala"]

    def test_a_known_name_edited_in_proposes_nothing(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Eyal Zror"))},
            tasks=[_db_task(assignee="Roye Tadmor")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Roye Tadmor",
                              "status": "pending"}},
        )
        assert plan.person_proposals == []

    def test_the_same_unknown_name_is_proposed_once(self):
        plan = _plan(
            {TAB: _grid(_prow(resp="Ayala"), _arow(resp="Ayala"))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending"}},
            proj_snaps={"p1": {}},
        )
        assert plan.person_proposals == ["Ayala"]


class TestRowsInsertedMidCycle:
    """Eyal asked to verify the system copes when he ADDS rows.

    Row numbers come from a read that already happened. If a row is inserted
    above one we planned to adopt, that number now addresses a different line —
    stamping the uid there would make the wrong row become the task, and the
    line actually typed would be re-created every cycle forever.
    """

    def _svc(self, returned_row):
        svc = MagicMock()
        svc._execute_with_retry.side_effect = lambda fn: {"values": [returned_row]}
        return svc

    def test_an_unshifted_row_is_adopted(self):
        spec = {"kind": "task", "tab": TAB, "row": 5, "title": "Call the bank"}
        svc = self._svc(["", "", "Call the bank", "", "", "", ""])
        assert psr._row_still_matches(svc, "sid", spec) is True

    def test_a_shifted_row_is_deferred_not_adopted(self):
        """Something else is at that number now."""
        spec = {"kind": "task", "tab": TAB, "row": 5, "title": "Call the bank"}
        svc = self._svc(["", "", "A completely different line", "", "", "", ""])
        assert psr._row_still_matches(svc, "sid", spec) is False

    def test_a_row_that_already_has_an_identity_is_refused(self):
        """It was claimed between the read and now — adopting it twice would
        give two tasks the same row."""
        spec = {"kind": "task", "tab": TAB, "row": 5, "title": "Call the bank"}
        svc = self._svc(["", "", "Call the bank", "", "", "", "", "A", "t9", "p1"])
        assert psr._row_still_matches(svc, "sid", spec) is False

    def test_a_project_row_is_matched_on_its_subject(self):
        spec = {"kind": "project", "tab": TAB, "row": 5, "name": "New Vertical"}
        svc = self._svc(["", "New Vertical", "", "", "", "", ""])
        assert psr._row_still_matches(svc, "sid", spec) is True

    def test_a_failed_re_read_never_adopts(self):
        spec = {"kind": "task", "tab": TAB, "row": 5, "title": "Call the bank"}
        svc = MagicMock()
        svc._execute_with_retry.side_effect = RuntimeError("transient")
        assert psr._row_still_matches(svc, "sid", spec) is False


class TestDateFeedbackIsImmediate:
    def test_a_newly_typed_sloppy_date_is_canonicalised_the_same_cycle(self):
        """Normalisation used to run only when nothing diverged, so a freshly
        typed "12.9" was understood but left looking unrecognised until the next
        pass — the answer to "does it know what I meant?" arriving 30 minutes
        after the question."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(date="12.9.2026"))},
            tasks=[_db_task(deadline=None)],
            act_snaps={"t1": {"title": "Ship the API", "deadline": None,
                              "status": "pending"}},
        )
        assert plan.task_updates["t1"]["deadline"] == "2026-09-12"
        writes = [w for w in plan.cell_writes if w[2] == COLS["Date"]]
        assert writes and writes[0][3] == "12/09/2026"
        assert plan.counters["normalized_dates"] == 1

    def test_an_already_canonical_date_is_not_rewritten(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(date="12/09/2026"))},
            tasks=[_db_task(deadline=None)],
            act_snaps={"t1": {"title": "Ship the API", "deadline": None,
                              "status": "pending"}},
        )
        assert [w for w in plan.cell_writes if w[2] == COLS["Date"]] == []
        assert plan.counters["normalized_dates"] == 0

    def test_an_unreadable_date_is_still_left_exactly_as_typed(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(date="dont mind"))},
            tasks=[_db_task(deadline="2026-08-12")],
            act_snaps={"t1": {"title": "Ship the API", "deadline": "2026-08-12",
                              "status": "pending"}},
        )
        assert [w for w in plan.cell_writes if w[2] == COLS["Date"]] == []
        assert plan.counters["bad_dates"] == 1


class TestClearedCellsNeverErase:
    """Eyal's check #20: select ten rows, delete.

    The hidden identity columns survive that gesture, so the rows are still
    recognised — and the engine proposed pulling every blank into the database.
    One keystroke would have erased 8 real deadlines and 10 real assignees.
    """

    def test_a_cleared_date_never_nulls_the_deadline(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(date=""))},
            tasks=[_db_task(deadline="2026-08-12")],
            act_snaps={"t1": {"title": "Ship the API", "deadline": "2026-08-12",
                              "status": "pending"}},
        )
        assert "deadline" not in plan.task_updates.get("t1", {})

    def test_a_cleared_owner_never_nulls_the_assignee(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp=""))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending"}},
        )
        assert "assignee" not in plan.task_updates.get("t1", {})

    def test_a_cleared_project_objective_is_not_erased(self):
        plan = _plan(
            {TAB: _grid(_prow(todo=""))},
            tasks=[], projects=[_db_project(objective="Win Lombardy")],
            proj_snaps={"p1": {"objective": "Win Lombardy"}},
        )
        assert "objective" not in plan.project_updates.get("p1", {})

    def test_the_cleared_cell_is_refreshed_from_the_database(self):
        """Refused is not enough — the value has to come back."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp=""))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending"}},
        )
        writes = [w for w in plan.cell_writes if w[2] == COLS["Resp."]]
        assert writes and writes[0][3] == "Eyal Zror"

    def test_mass_clearing_is_counted_not_silent(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(uid="t1", date="", resp=""))},
            tasks=[_db_task("t1", deadline="2026-08-12", assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "deadline": "2026-08-12",
                              "assignee": "Eyal Zror", "status": "pending"}},
        )
        assert plan.counters["blanks_refused"] == 2


class TestPastedBlocksDoNotDuplicate:
    """Eyal's check #19: copy a whole block, paste it lower down.

    Copying rows does not reliably bring the hidden columns, so the pasted rows
    arrive with NO uid — they look exactly like lines somebody typed, and each
    became a new task duplicating the original. The duplicate-uid path cannot
    help because there is no uid to duplicate.
    """

    def test_a_pasted_action_is_not_recreated(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(uid="t1"),
                        _human(action="Ship the API"))},   # pasted, no identity
            tasks=[_db_task("t1", title="Ship the API")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.creates == []
        assert plan.counters["paste_duplicates"] == 1

    def test_genuinely_new_text_is_still_created(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(uid="t1"),
                        _human(action="Something completely different"))},
            tasks=[_db_task("t1", title="Ship the API")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert [c["title"] for c in plan.creates] == ["Something completely different"]

    def test_the_same_text_under_a_DIFFERENT_project_is_still_created(self):
        """"Call the bank" can legitimately exist under two projects."""
        plan = _plan(
            {TAB: _grid(_prow(uid="p1"), _prow(uid="p2", name="Other", num=20),
                        _human(action="Ship the API"))},
            projects=[_db_project("p1"), _db_project("p2", name="Other")],
            tasks=[_db_task("t1", title="Ship the API", project_id="p1")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert len(plan.creates) == 1

    def test_a_closed_task_does_not_block_re_adding_the_work(self):
        """Finished once, needed again — that is a real new commitment."""
        plan = _plan(
            {TAB: _grid(_prow(), _human(action="Ship the API"))},
            tasks=[_db_task("t1", title="Ship the API", status="done")],
            act_snaps={},
        )
        assert len(plan.creates) == 1


class TestPastedProjectRowResolves:
    def test_a_typed_name_matching_an_existing_project_is_that_project(self):
        """add_canonical_project is idempotent by name, so creating it returns
        the same row anyway. Resolving it HERE is what gives the action rows
        beneath a real parent."""
        plan = _plan(
            {TAB: _grid(["", "Product V1", "", "", "", "", ""])},
            tasks=[],
        )
        assert plan.creates == []
        assert plan.counters["matched_existing_project"] == 1

    def test_a_pasted_block_creates_neither_project_nor_duplicate_actions(self):
        """Check #19 end to end: project row and its actions all pasted without
        identity. Nothing should be created."""
        plan = _plan(
            {TAB: _grid(_prow(uid="p1"), _arow(uid="t1"),
                        ["", "Product V1", "", "", "", "", ""],
                        _human(action="Ship the API"))},
            tasks=[_db_task("t1", title="Ship the API", project_id="p1")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.creates == []
        assert plan.counters["paste_duplicates"] == 1
        assert plan.counters["matched_existing_project"] == 1

    def test_a_genuinely_new_project_name_is_still_created(self):
        plan = _plan(
            {TAB: _grid(["", "Brand New Area", "", "", "", "", ""])}, tasks=[])
        assert [c["name"] for c in plan.creates] == ["Brand New Area"]
        assert plan.counters["matched_existing_project"] == 0


class TestRecencyRuleIsDisabled:
    """The tie-break was reverted on 2026-08-08 after the code review.

    `tasks.manual_set_at` is ONE timestamp per task, bumped whenever any field
    is marked manual. Compared against a snapshot it answers "was something on
    this task edited recently", never "was THIS field edited recently" — so it
    unlocked every sticky field at once. Nechama typing a comment would let the
    next Tasks-tab pass overwrite a deadline Eyal set by hand, silently, on a
    field nobody touched.

    Strictly worse than the problem it solved: permanent divergence is visible
    and keeps both values; overwriting a hand-set value destroys one.
    """

    def _run(self, manual_set_at, snapshot_at):
        return _plan(
            {TAB: _grid(_prow(), _arow(resp="Nechama Tik"))},
            tasks=[_db_task(assignee="Roye Tadmor", manual_assignee=True,
                            manual_set_at=manual_set_at)],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Nechama Tik",
                              "status": "pending", "snapshot_at": snapshot_at}},
        )

    def test_a_sticky_field_is_held_even_when_the_db_edit_looks_newer(self):
        plan = self._run("2026-08-08T12:00:00Z", "2026-08-08T10:00:00Z")
        assert plan.counters["manual_held"] == 1
        assert [w for w in plan.cell_writes if w[2] == COLS["Resp."]] == []

    def test_it_is_held_when_the_db_edit_is_older_too(self):
        plan = self._run("2026-08-08T09:00:00Z", "2026-08-08T10:00:00Z")
        assert plan.counters["manual_held"] == 1

    def test_a_non_sticky_field_still_refreshes_normally(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Nechama Tik"))},
            tasks=[_db_task(assignee="Roye Tadmor")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Nechama Tik",
                              "status": "pending"}},
        )
        assert plan.counters["manual_held"] == 0
        assert plan.counters["pushed"] == 1


class TestPastedProjectRowResolves:
    def test_a_typed_name_matching_an_existing_project_is_that_project(self):
        """add_canonical_project is idempotent by name, so creating it returns
        the same row anyway. Resolving it HERE is what gives the action rows
        beneath a real parent."""
        plan = _plan(
            {TAB: _grid(["", "Product V1", "", "", "", "", ""])},
            tasks=[],
        )
        assert plan.creates == []
        assert plan.counters["matched_existing_project"] == 1

    def test_a_pasted_block_creates_neither_project_nor_duplicate_actions(self):
        """Check #19 end to end: project row and its actions all pasted without
        identity. Nothing should be created."""
        plan = _plan(
            {TAB: _grid(_prow(uid="p1"), _arow(uid="t1"),
                        ["", "Product V1", "", "", "", "", ""],
                        _human(action="Ship the API"))},
            tasks=[_db_task("t1", title="Ship the API", project_id="p1")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending"}},
        )
        assert plan.creates == []
        assert plan.counters["paste_duplicates"] == 1
        assert plan.counters["matched_existing_project"] == 1

    def test_a_genuinely_new_project_name_is_still_created(self):
        plan = _plan(
            {TAB: _grid(["", "Brand New Area", "", "", "", "", ""])}, tasks=[])
        assert [c["name"] for c in plan.creates] == ["Brand New Area"]
        assert plan.counters["matched_existing_project"] == 0


class TestNoMergeBaseIsNotAnEdit:
    """With snap == {} every populated cell differs from a None snapshot.

    A row whose snapshot failed to write (the helper logs and returns False
    rather than raising) or that was injected before its snapshot landed would
    have had its ENTIRE contents pulled into the database and marked sticky —
    freezing machine-written values as human decisions.
    """

    def test_a_row_with_no_snapshot_is_never_pulled(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Roye Tadmor", date="12/08/2026"))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={},                      # snapshot write failed
        )
        assert plan.task_updates == {}
        assert plan.counters["pulled"] == 0

    def test_it_is_refreshed_from_the_database_instead(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Roye Tadmor"))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={},
        )
        writes = [w for w in plan.cell_writes if w[2] == COLS["Resp."]]
        assert writes and writes[0][3] == "Eyal Zror"

    def test_a_real_snapshot_still_detects_the_edit(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow(resp="Roye Tadmor"))},
            tasks=[_db_task(assignee="Eyal Zror")],
            act_snaps={"t1": {"title": "Ship the API", "assignee": "Eyal Zror",
                              "status": "pending",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )
        assert plan.task_updates["t1"]["assignee"] == "Roye Tadmor"


class TestDeletedProjectRowDoesNotRefile:
    """Deleting a project row makes every action beneath it parse into the
    block ABOVE. Without evidence that the row's own project is gone, that is
    indistinguishable from a deliberate drag — so one delete silently re-filed
    a whole project's tasks and marked them sticky, blocking correction."""

    def test_actions_orphaned_by_a_deleted_project_row_are_left_alone(self):
        plan = _plan(
            {TAB: _grid(_prow(uid="p1"),
                        _arow(uid="t1", parent="p_gone"))},
            act_snaps={"t1": {"title": "Ship the API", "status": "pending",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )
        assert "project_id" not in plan.task_updates.get("t1", {})
        assert plan.counters["orphaned_by_deleted_project"] == 1
        assert plan.counters["reparented"] == 0

    def test_a_genuine_drag_still_repoints_and_rewrites_parent(self):
        """The _parent CELL must be rewritten too — leaving it stale re-applied
        the same re-parent, with a fresh manual timestamp, every cycle."""
        plan = _plan(
            {TAB: _grid(_prow(uid="p1"), _prow(uid="p2", name="Cloud", num=20),
                        _arow(uid="t1", parent="p1"))},
            projects=[_db_project("p1"), _db_project("p2", name="Cloud")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )
        assert plan.task_updates["t1"]["project_id"] == "p2"
        writes = [w for w in plan.cell_writes if w[2] == COLS["_parent"]]
        assert writes and writes[0][3] == "p2"


class TestProjectsGetABlock:
    """Nothing else could add one. write_project_status_blocks is only reached
    from the one-shot rollout script, the weekly slot writes nothing, and
    _plan_injections can only append INTO blocks that already exist — so a
    project created via MCP or the Projects tab never appeared here, and every
    task attached to it was invisible too."""

    def test_a_project_with_no_block_gets_one(self):
        with patch("processors.project_status._project_area_names",
                   return_value={"a1": TAB}):
            plan = _plan(
                {TAB: _grid(_prow(uid="p1"))},
                projects=[_db_project("p1", area_id="a1"),
                          _db_project("p2", name="Brand New", area_id="a1")],
                tasks=[],
            )
        names = [r[1] for _t, _a, rows in plan.new_blocks for r in rows]
        assert names == ["Brand New"]
        assert plan.counters["blocks_added"] == 1

    def test_the_new_block_carries_its_identity(self):
        with patch("processors.project_status._project_area_names",
                   return_value={"a1": TAB}):
            plan = _plan(
                {TAB: _grid(_prow(uid="p1"))},
                projects=[_db_project("p1", area_id="a1"),
                          _db_project("p2", name="Brand New", area_id="a1")],
                tasks=[],
            )
        row = plan.new_blocks[0][2][0]
        assert row[7] == "P" and row[8] == "p2" and row[9] == "p2"

    def test_an_existing_project_is_not_re_added(self):
        plan = _plan({TAB: _grid(_prow(uid="p1"))},
                     projects=[_db_project("p1")], tasks=[])
        assert [r for _t, _a, rows in plan.new_blocks for r in rows] == []

    def test_a_retired_project_never_gets_a_block(self):
        plan = _plan(
            {TAB: _grid(_prow(uid="p1"))},
            projects=[_db_project("p1"),
                      _db_project("p9", name="Gone", status="retired")],
            tasks=[])
        names = [r[1] for _t, _a, rows in plan.new_blocks for r in rows]
        assert "Gone" not in names


class TestClosedRowDropsItsSnapshot:
    def test_a_removed_closed_row_queues_its_snapshot_for_deletion(self):
        """Left behind, a task later re-opened has a snapshot but no row:
        suppression reads that as a delete, and injection refuses to re-add
        anything that already has a snapshot. Permanently invisible."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(checked=True))},
            tasks=[_db_task(status="done")],
            act_snaps={"t1": {"title": "Ship the API", "status": "done",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )
        assert plan.row_deletes and plan.drop_snapshots == ["t1"]

    def test_an_open_row_keeps_its_snapshot(self):
        """Only a REMOVED row drops its base. (A row carrying her comment used
        to be kept and struck; since 2026-08-08 every finished row leaves, so
        the open case is what distinguishes the two.)"""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(notes="waiting"))},
            tasks=[_db_task(status="in_progress", notes="waiting")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending",
                              "notes": "waiting",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )
        assert plan.drop_snapshots == []


class TestMatchedProjectRowIsAdopted:
    def test_it_is_stamped_with_the_existing_identity(self):
        """Setting parent_uid alone left the row human forever: what she typed
        on that line was never persisted and re-evaluated every cycle."""
        plan = _plan(
            {TAB: _grid(["", "Product V1", "", "Win Lombardy", "", "", ""])},
            tasks=[])
        assert plan.adopt_rows and plan.adopt_rows[0]["uid"] == "p1"

    def test_the_cells_she_typed_are_pulled(self):
        plan = _plan(
            {TAB: _grid(["", "Product V1", "", "Win Lombardy", "", "", ""])},
            tasks=[])
        assert plan.project_updates["p1"]["objective"] == "Win Lombardy"
        assert ("project", "p1", "objective") in plan.manual_marks


class TestStructuralApplyReachesTheSheet:
    """_apply_structural, not just build_plan.

    The whole test file exercised planning; nothing covered the apply path. So
    when `tabs` was left without plan.new_blocks, the function early-returned
    and a planned block never reached the sheet — every unit test still passed.
    Only a live dress rehearsal against a copy caught it.
    """

    def _svc(self, grid_values, gids):
        svc = MagicMock()

        # _execute_with_retry calls request_factory().execute() — the mock must
        # do the same or every read returns a MagicMock and each tab looks stale.
        svc._execute_with_retry.side_effect = lambda fn: fn().execute()
        sheets = svc.service.spreadsheets.return_value
        sheets.values.return_value.batchGet.return_value.execute.return_value = {
            "valueRanges": [{"values": grid_values}]}
        sheets.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": t, "sheetId": g}}
                       for t, g in gids.items()]}
        sheets.batchUpdate.return_value.execute.return_value = {}
        return svc, sheets

    def _plan_with_block(self, fingerprint_grid):
        plan = psr.Plan()
        row = ["", "Brand New", "", "", "", "", "", "P", "p9", "p9", "", ""]
        plan.new_blocks.append((TAB, 6, [row]))
        blocks, orphans, _ = psr.parse_tab(fingerprint_grid)
        plan.fingerprints[TAB] = psr.tab_fingerprint(blocks, orphans)
        return plan

    @pytest.mark.asyncio
    async def test_a_planned_block_actually_reaches_the_sheet(self):
        grid = _grid(_prow(uid="p1"))
        plan = self._plan_with_block(grid)
        svc, sheets = self._svc(grid, {TAB: 7})
        with patch("services.google_sheets.sheets_service", svc):
            out = await psr._apply_structural(plan, "sid")

        assert out.get("blocks_added") == 1
        reqs = sheets.batchUpdate.call_args.kwargs["body"]["requests"]
        assert any("insertDimension" in r for r in reqs)
        assert any("updateCells" in r for r in reqs)

    @pytest.mark.asyncio
    async def test_a_stale_tab_is_skipped(self):
        grid = _grid(_prow(uid="p1"))
        plan = self._plan_with_block(grid)
        plan.fingerprints[TAB] = "something-else"     # tab moved since the read
        svc, sheets = self._svc(grid, {TAB: 7})
        with patch("services.google_sheets.sheets_service", svc):
            out = await psr._apply_structural(plan, "sid")

        assert out["stale_tabs"] == [TAB]
        assert out.get("blocks_added", 0) == 0
        sheets.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_nothing_planned_means_no_call(self):
        svc, sheets = self._svc([], {TAB: 7})
        with patch("services.google_sheets.sheets_service", svc):
            out = await psr._apply_structural(psr.Plan(), "sid")
        assert out == {"injected": 0, "rows_deleted": 0, "struck": 0,
                       "stale_tabs": []}
        sheets.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_are_emitted_bottom_to_top(self):
        """Within one batch an earlier delete shifts the targets of later ones."""
        grid = _grid(_prow(uid="p1"), _arow(uid="t1"), _arow(uid="t2"))
        plan = psr.Plan()
        blocks, orphans, _ = psr.parse_tab(grid)
        plan.fingerprints[TAB] = psr.tab_fingerprint(blocks, orphans)
        plan.row_deletes.extend([(TAB, 5, "t1"), (TAB, 6, "t2")])
        svc, sheets = self._svc(grid, {TAB: 7})
        with patch("services.google_sheets.sheets_service", svc):
            await psr._apply_structural(plan, "sid")

        reqs = sheets.batchUpdate.call_args.kwargs["body"]["requests"]
        starts = [r["deleteDimension"]["range"]["startIndex"]
                  for r in reqs if "deleteDimension" in r]
        assert starts == sorted(starts, reverse=True)


class TestCrossTabMove:
    """Eyal moved a task from Product & Technology into the Italy block on
    Sales in his first real session. The per-tab parent check made that look
    identical to "the project row above me was deleted", so the move was
    refused. The question is only ever "does the project this row claims still
    exist ANYWHERE?" """

    OTHER = "SALES & BUSINESS DEVELOPMENT"

    def _grids(self, parent):
        return {
            TAB: _grid(_prow(uid="p1", name="MVP Delivery")),
            self.OTHER: _grid(_prow(uid="p2", name="Italy"),
                              _arow(uid="t1", parent=parent)),
        }

    def _run(self, parent):
        return _plan(
            self._grids(parent),
            projects=[_db_project("p1", name="MVP Delivery"),
                      _db_project("p2", name="Italy")],
            tasks=[_db_task("t1", project_id="p1")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )

    def test_a_task_moved_to_another_tab_is_repointed(self):
        plan = self._run("p1")            # claims a project on the OTHER tab
        assert plan.task_updates["t1"]["project_id"] == "p2"
        assert plan.counters["reparented"] == 1
        assert plan.counters["orphaned_by_deleted_project"] == 0

    def test_its_parent_cell_is_rewritten_so_it_settles(self):
        plan = self._run("p1")
        writes = [w for w in plan.cell_writes if w[2] == COLS["_parent"]]
        assert writes and writes[0][3] == "p2"

    def test_a_parent_that_exists_nowhere_is_still_left_alone(self):
        """The deleted-project-row case must keep working."""
        plan = self._run("p_gone")
        assert "project_id" not in plan.task_updates.get("t1", {})
        assert plan.counters["orphaned_by_deleted_project"] == 1


class TestEveryFinishedRowLeavesTheSheet:
    """A tick is an instruction, not a suggestion.

    Rows carrying anything of hers used to be kept and struck through instead,
    on the reasoning that removing one would throw away the only copy. That was
    wrong: every column on an action row persists to the database, so nothing
    on the row exists only on the row. In practice setting a date or an owner is
    the normal thing to do before finishing something, so almost every completed
    row qualified and the sheet accumulated finished work.
    """

    def _closed(self, **task_kw):
        return _plan(
            {TAB: _grid(_prow(), _arow(checked=True, **{
                k: v for k, v in task_kw.pop("row", {}).items()}))},
            tasks=[_db_task(status="done", **task_kw)],
            act_snaps={"t1": {"title": "Ship the API", "status": "done",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )

    def test_a_plain_finished_row_is_removed(self):
        plan = self._closed()
        assert [r[2] for r in plan.row_deletes] == ["t1"]
        assert plan.strikes == []

    def test_a_row_with_her_comment_is_ALSO_removed(self):
        """tasks.notes already holds it — the row is not the only copy."""
        plan = self._closed(notes="waiting on the bank",
                            row={"notes": "waiting on the bank"})
        assert [r[2] for r in plan.row_deletes] == ["t1"]
        assert plan.strikes == []

    def test_a_row_whose_owner_and_date_she_set_is_ALSO_removed(self):
        plan = self._closed(manual_assignee=True, manual_deadline=True,
                            manual_status=True)
        assert [r[2] for r in plan.row_deletes] == ["t1"]
        assert plan.strikes == []

    def test_its_snapshot_goes_with_it_so_re_opening_works(self):
        plan = self._closed(manual_assignee=True)
        assert plan.drop_snapshots == ["t1"]

    def test_a_row_ticked_this_very_cycle_is_left_for_next_time(self):
        """She would watch the line vanish under the cursor."""
        plan = _plan(
            {TAB: _grid(_prow(), _arow(checked=True))},
            tasks=[_db_task(status="pending")],
            act_snaps={"t1": {"title": "Ship the API", "status": "pending",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )
        assert plan.task_updates["t1"]["status"] == "done"
        assert plan.row_deletes == [] and plan.strikes == []

    def test_an_open_row_is_never_removed(self):
        plan = _plan(
            {TAB: _grid(_prow(), _arow())},
            act_snaps={"t1": {"title": "Ship the API", "status": "pending",
                              "snapshot_at": "2026-08-08T10:00:00Z"}},
        )
        assert plan.row_deletes == [] and plan.strikes == []
