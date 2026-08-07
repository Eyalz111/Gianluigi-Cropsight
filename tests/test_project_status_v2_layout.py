"""v2 block layout: the builder and the sheet requests it produces.

These pin the properties the cutover depends on. The engine tests (P3) cover
the merge rules; this file covers what actually lands in the file.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.project_status_rows import (
    ALL_HEADERS, FIRST_BODY_ROW, KIND_ACTION, KIND_PROJECT,
    parse_tab, resolve_columns, strip_provenance,
)
from services.project_status_sheet import (
    _checkbox_requests, _conditional_format_rules, _ddmmyyyy,
    _header_note_requests, _v2_structure_requests,
)

PROD = "PRODUCT & TECHNOLOGY"
AREA_ID = "area-prod"


def _project(pid, name, order, **kw):
    row = {"id": pid, "name": name, "display_order": order, "area_id": AREA_ID,
           "status": "active", "objective": "", "target_date": None,
           "owner": "", "notes": ""}
    row.update(kw)
    return row


def _task(tid, title, **kw):
    row = {"id": tid, "title": title, "status": "pending", "deadline": None,
           "assignee": "", "notes": "", "meeting_id": "", "meetings": {}}
    row.update(kw)
    return row


def _build(projects, by_project, areas=None):
    from processors import project_status as ps
    with patch.object(ps, "supabase_client") as sb, \
         patch.object(ps, "_project_area_names",
                      return_value=areas or {AREA_ID: PROD}):
        sb.get_canonical_projects.return_value = projects
        sb.get_open_tasks_by_project.return_value = by_project
        return ps.build_status_blocks()


class TestBlockBuilder:
    def test_project_row_then_its_actions(self):
        pack = _build(
            [_project("p1", "Product V1", 10)],
            {"p1": [_task("t1", "Ship the API"), _task("t2", "Write docs")]},
        )
        rows = pack[PROD]
        assert [r[7] for r in rows] == [KIND_PROJECT, KIND_ACTION, KIND_ACTION]
        assert rows[0][1] == "Product V1"
        assert rows[1][8] == "t1" and rows[1][9] == "p1"

    def test_every_row_carries_its_own_and_its_parents_identity(self):
        """Identity must never be positional — this is what survives a drag."""
        pack = _build([_project("p1", "A", 10)], {"p1": [_task("t1", "x")]})
        proj, action = pack[PROD]
        assert proj[8] == proj[9] == "p1"
        assert action[8] == "t1" and action[9] == "p1"

    def test_retired_projects_are_omitted(self):
        pack = _build(
            [_project("p1", "Live", 10), _project("p2", "Gone", 20, status="retired")],
            {},
        )
        assert [r[1] for r in pack[PROD]] == ["Live"]

    def test_empty_project_keeps_its_block(self):
        """A project with no open actions is a real review state — and she needs
        the block to exist before she can add the first action under it."""
        pack = _build([_project("p1", "Quiet", 10)], {})
        assert len(pack[PROD]) == 1 and pack[PROD][0][7] == KIND_PROJECT

    def test_ordered_by_display_order(self):
        pack = _build(
            [_project("p2", "Second", 20), _project("p1", "First", 10)], {})
        assert [r[1] for r in pack[PROD]] == ["First", "Second"]

    def test_action_row_column_a_is_a_real_boolean(self):
        """Not the string 'FALSE' — the cell carries BOOLEAN validation."""
        pack = _build([_project("p1", "A", 10)], {"p1": [_task("t1", "x")]})
        assert pack[PROD][1][0] is False

    def test_project_row_column_a_is_the_number(self):
        pack = _build([_project("p1", "A", 10)], {})
        assert pack[PROD][0][0] == 10

    def test_objective_is_the_to_do_cell_on_the_project_row(self):
        """v1 had these backwards: To do is the eventual aim, not the step."""
        pack = _build([_project("p1", "A", 10, objective="Win Lombardy")], {})
        cols = resolve_columns(ALL_HEADERS)
        assert pack[PROD][0][cols["To do"]] == "Win Lombardy"
        assert pack[PROD][0][cols["Action"]] == ""

    def test_action_text_carries_a_provenance_marker(self):
        pack = _build(
            [_project("p1", "A", 10)],
            {"p1": [_task("t1", "Chase NCPB",
                          meetings={"title": "Weekly Sync", "date": "2026-08-04"})]},
        )
        action = pack[PROD][1][2]
        assert "[auto" in action and "Weekly Sync" in action
        assert strip_provenance(action) == "Chase NCPB"

    def test_rows_are_the_full_width_of_the_header(self):
        pack = _build([_project("p1", "A", 10)], {"p1": [_task("t1", "x")]})
        assert all(len(r) == len(ALL_HEADERS) for r in pack[PROD])

    def test_a_project_in_an_unlisted_area_still_gets_a_tab(self):
        """Silently dropping a project because its area has no tab would make
        the file disagree with the curated list."""
        pack = _build([_project("p1", "Odd", 10, area_id="area-x")], {},
                      areas={"area-x": "SOMEWHERE NEW"})
        assert [r[1] for r in pack["SOMEWHERE NEW"]] == ["Odd"]


class TestRoundTrip:
    def test_built_rows_parse_back_into_the_same_blocks(self):
        """The builder writes it, the reconcile parser reads it. If these two
        ever disagree the snapshot seed is wrong on cycle one."""
        pack = _build(
            [_project("p1", "Product V1", 10, objective="Ship"),
             _project("p2", "Cloud", 20)],
            {"p1": [_task("t1", "Ship the API"), _task("t2", "Docs")],
             "p2": [_task("t3", "Move region")]},
        )
        grid = [["title"], ["dist"], list(ALL_HEADERS), *pack[PROD]]
        blocks, orphans, _ = parse_tab(grid)

        assert orphans == []
        assert [b.project.uid for b in blocks] == ["p1", "p2"]
        assert [a.uid for a in blocks[0].actions] == ["t1", "t2"]
        assert blocks[0].project.values["To do"] == "Ship"
        assert blocks[0].project.checked is False       # '10' is not a tick
        assert all(a.checked is False for a in blocks[0].actions)
        assert blocks[0].actions[0].parent == "p1"


class TestCheckboxRequests:
    def _rows(self, kinds):
        return [[""] * 7 + [k, f"u{i}", "p", "", ""] for i, k in enumerate(kinds)]

    def test_one_run_per_contiguous_block_of_actions(self):
        reqs = _checkbox_requests(1, self._rows(["P", "A", "A", "P", "A"]))
        ranges = [r["setDataValidation"]["range"] for r in reqs]
        assert len(ranges) == 2
        assert ranges[0]["startRowIndex"] == FIRST_BODY_ROW  # row 4 is the project
        assert ranges[0]["endRowIndex"] == FIRST_BODY_ROW + 2
        assert ranges[1]["startRowIndex"] == FIRST_BODY_ROW + 3

    def test_project_rows_never_get_a_checkbox(self):
        """Column A holds the '#' there — validation would flag every one."""
        reqs = _checkbox_requests(1, self._rows(["P", "P", "P"]))
        assert reqs == []

    def test_trailing_actions_are_flushed(self):
        reqs = _checkbox_requests(1, self._rows(["P", "A"]))
        assert len(reqs) == 1
        assert reqs[0]["setDataValidation"]["range"]["endRowIndex"] == FIRST_BODY_ROW + 1

    def test_validation_is_boolean_and_not_strict(self):
        rule = _checkbox_requests(1, self._rows(["A"]))[0]["setDataValidation"]["rule"]
        assert rule["condition"]["type"] == "BOOLEAN"
        assert rule["strict"] is False

    def test_no_rows_no_requests(self):
        assert _checkbox_requests(1, []) == []


class TestStructureRequests:
    def test_hidden_columns_are_hidden_tinted_and_protected(self):
        reqs = _v2_structure_requests(7, 7)
        hide = next(r for r in reqs if "updateDimensionProperties" in r)
        assert hide["updateDimensionProperties"]["properties"]["hiddenByUser"] is True
        assert hide["updateDimensionProperties"]["range"]["startIndex"] == 7
        assert hide["updateDimensionProperties"]["range"]["endIndex"] == 12

        prot = next(r for r in reqs if "addProtectedRange" in r)
        assert prot["addProtectedRange"]["protectedRange"]["warningOnly"] is True

    def test_protection_never_covers_a_visible_column(self):
        """Locking anything in A..G would fight the person the file is for."""
        prot = next(r for r in _v2_structure_requests(7, 7) if "addProtectedRange" in r)
        assert prot["addProtectedRange"]["protectedRange"]["range"]["startColumnIndex"] == 7

    def test_colour_rules_are_scoped_to_action_rows(self):
        rules = _conditional_format_rules(7, 7)
        for r in rules:
            formula = (r["addConditionalFormatRule"]["rule"]["booleanRule"]
                       ["condition"]["values"][0]["userEnteredValue"])
            assert '$H4="A"' in formula, formula

    def test_overdue_rule_outranks_due_soon(self):
        rules = _conditional_format_rules(7, 7)
        assert rules[0]["addConditionalFormatRule"]["index"] == 0
        assert rules[1]["addConditionalFormatRule"]["index"] == 1

    def test_due_soon_window_follows_the_setting(self):
        rules = _conditional_format_rules(7, 14)
        formula = (rules[1]["addConditionalFormatRule"]["rule"]["booleanRule"]
                   ["condition"]["values"][0]["userEnteredValue"])
        assert "TODAY()+14" in formula

    def test_date_expression_is_locale_proof(self):
        """DATEVALUE would read 05/08 as 8 May under a US locale."""
        expr = _ddmmyyyy("E")
        assert "DATEVALUE" not in expr
        assert "RIGHT($E4,4)" in expr and "LEFT($E4,2)" in expr

    def test_a_note_on_every_visible_header(self):
        req = _header_note_requests(3)[0]["updateCells"]
        assert req["fields"] == "note"
        assert len(req["rows"][0]["values"]) == 7
        assert all(v["note"] for v in req["rows"][0]["values"])


class TestSchedulerGuard:
    """The Monday 07:00 slot must not rebuild once the file is Nechama's.

    v1's refresh CLEARS every tab. With PROJECT_STATUS_ENABLED already true in
    production, leaving that path reachable would have destroyed the cutover on
    the next scheduled run — a week of edits gone at 07:00 with no error.
    """

    @pytest.mark.asyncio
    async def test_v2_flag_never_writes_to_the_sheet(self):
        from schedulers import project_status_scheduler as mod

        with patch.object(mod.settings, "PROJECT_STATUS_V2_LAYOUT", True), \
             patch.object(mod.settings, "PROJECT_STATUS_SHEET_ID", "sid"), \
             patch("services.project_status_sheet.write_project_status") as writer, \
             patch("services.supabase_client.supabase_client") as sb:
            sb.get_open_tasks_by_project.return_value = {"p1": [{"id": "t1"}]}
            sb.get_canonical_projects.return_value = [
                {"id": "p1", "name": "Live", "status": "active"},
                {"id": "p2", "name": "Quiet", "status": "active"},
            ]
            result = await mod.project_status_scheduler.refresh(notify=False)

        writer.assert_not_called()
        assert result["rebuilt"] is False
        assert result["actions"] == 1 and result["projects"] == 2

    @pytest.mark.asyncio
    async def test_v1_path_is_unchanged_when_the_flag_is_off(self):
        from schedulers import project_status_scheduler as mod

        with patch.object(mod.settings, "PROJECT_STATUS_V2_LAYOUT", False), \
             patch("processors.project_status.build_status_pack", return_value={}), \
             patch("processors.project_status.title_block", return_value={}), \
             patch("services.project_status_sheet.write_project_status") as writer:
            writer.return_value = {"rows": 0, "tabs": [], "url": "", "error": None}
            await mod.project_status_scheduler.refresh(notify=False)

        writer.assert_called_once()
