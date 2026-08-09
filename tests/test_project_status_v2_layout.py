"""v2 block layout: the builder and the sheet requests it produces.

These pin the properties the cutover depends on. The engine tests (P3) cover
the merge rules; this file covers what actually lands in the file.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.project_status_rows import (
    ALL_HEADERS, FIRST_BODY_ROW, KIND_ACTION, KIND_PROJECT, VISIBLE_HEADERS,
    COL_ACTION, COL_DATE, COL_KIND, COL_NUM, COL_PARENT, COL_PRIORITY,
    COL_PROJECT, COL_TODO, COL_UID,
    parse_tab, resolve_columns, strip_provenance,
)
from services.project_status_sheet import (
    IDX, N_ALL, N_VISIBLE, _a1,
    _checkbox_requests, _conditional_format_rules, _ddmmyyyy,
    _header_note_requests, _priority_requests, _v2_structure_requests,
)

PROD = "PRODUCT & TECHNOLOGY"
AREA_ID = "area-prod"

# Every position in this file is derived. The literals it used to carry (row[7]
# for _kind, column 7 for the first hidden column, '$H4' in a formula) all moved
# when Project and Priority were added, and a test that hard-codes a position
# stops testing the thing and starts testing the number.
KIND, UID, PARENT = IDX[COL_KIND], IDX[COL_UID], IDX[COL_PARENT]
KIND_A1 = _a1(KIND)
DATE_A1 = _a1(IDX[COL_DATE])


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
        assert [r[KIND] for r in rows] == [KIND_PROJECT, KIND_ACTION, KIND_ACTION]
        assert rows[0][IDX[COL_PROJECT]] == "Product V1"
        assert rows[1][UID] == "t1" and rows[1][PARENT] == "p1"

    def test_every_row_carries_its_own_and_its_parents_identity(self):
        """Identity must never be positional — this is what survives a drag."""
        pack = _build([_project("p1", "A", 10)], {"p1": [_task("t1", "x")]})
        proj, action = pack[PROD]
        assert proj[UID] == proj[PARENT] == "p1"
        assert action[UID] == "t1" and action[PARENT] == "p1"

    def test_retired_projects_are_omitted(self):
        pack = _build(
            [_project("p1", "Live", 10), _project("p2", "Gone", 20, status="retired")],
            {},
        )
        assert [r[IDX[COL_PROJECT]] for r in pack[PROD]] == ["Live"]

    def test_empty_project_keeps_its_block(self):
        """A project with no open actions is a real review state — and she needs
        the block to exist before she can add the first action under it."""
        pack = _build([_project("p1", "Quiet", 10)], {})
        assert len(pack[PROD]) == 1 and pack[PROD][0][KIND] == KIND_PROJECT

    def test_ordered_by_display_order(self):
        pack = _build(
            [_project("p2", "Second", 20), _project("p1", "First", 10)], {})
        assert [r[IDX[COL_PROJECT]] for r in pack[PROD]] == ["First", "Second"]

    def test_action_row_column_a_is_a_real_boolean(self):
        """Not the string 'FALSE' — the cell carries BOOLEAN validation."""
        pack = _build([_project("p1", "A", 10)], {"p1": [_task("t1", "x")]})
        assert pack[PROD][1][0] is False

    def test_project_row_column_a_is_left_for_the_renumbering_pass(self):
        """`#` used to show display_order — a sort key with deliberate gaps
        (10/20/30/90) that read as an arbitrary number to anyone using the file.
        It is now contiguous 1,2,3… computed from where the blocks SIT, which
        the builder cannot know."""
        pack = _build([_project("p1", "A", 10)], {})
        assert pack[PROD][0][IDX[COL_NUM]] == ""

    def test_objective_is_the_to_do_cell_on_the_project_row(self):
        """v1 had these backwards: To do is the eventual aim, not the step."""
        pack = _build([_project("p1", "A", 10, objective="Win Lombardy")], {})
        cols = resolve_columns(ALL_HEADERS)
        assert pack[PROD][0][cols[COL_TODO]] == "Win Lombardy"
        assert pack[PROD][0][cols[COL_ACTION]] == ""

    def test_action_text_carries_a_provenance_marker(self):
        pack = _build(
            [_project("p1", "A", 10)],
            {"p1": [_task("t1", "Chase NCPB",
                          meetings={"title": "Weekly Sync", "date": "2026-08-04"})]},
        )
        action = pack[PROD][1][IDX[COL_ACTION]]
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
        assert [r[IDX[COL_PROJECT]] for r in pack["SOMEWHERE NEW"]] == ["Odd"]


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
        assert blocks[0].project.values[COL_TODO] == "Ship"
        assert blocks[0].project.checked is False       # a numeral is not a tick
        assert all(a.checked is False for a in blocks[0].actions)
        assert blocks[0].actions[0].parent == "p1"


class TestCheckboxRequests:
    def _rows(self, kinds):
        return [[""] * N_VISIBLE + [k, f"u{i}", "p", "", ""]
                for i, k in enumerate(kinds)]

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


class TestPriorityColumn:
    """Brought over from the Tasks tab — one of the two reasons that tab still
    had a reason to exist, and the reason Eyal is retiring it."""

    def _rows(self, kinds):
        return [[""] * N_VISIBLE + [k, f"u{i}", "p", "", ""]
                for i, k in enumerate(kinds)]

    def test_the_dropdown_offers_all_four_levels(self):
        rule = _priority_requests(1, self._rows(["A"]))[0]["setDataValidation"]["rule"]
        assert rule["condition"]["type"] == "ONE_OF_LIST"
        values = [v["userEnteredValue"] for v in rule["condition"]["values"]]
        assert values == ["Urgent", "H", "M", "L"]

    def test_it_warns_rather_than_refuses(self):
        """House convention: a system-written value must never hard-error a
        cell, and typing over a dropdown must not be blocked mid-keystroke."""
        rule = _priority_requests(1, self._rows(["A"]))[0]["setDataValidation"]["rule"]
        assert rule["strict"] is False

    def test_project_rows_get_no_dropdown(self):
        """A project has no priority; an empty cell under a dropdown reads as a
        field somebody forgot to fill."""
        assert _priority_requests(1, self._rows(["P", "P"])) == []

    def test_one_run_per_contiguous_block(self):
        reqs = _priority_requests(1, self._rows(["P", "A", "A", "P", "A"]))
        assert len(reqs) == 2

    def test_it_targets_the_priority_column(self):
        rng = _priority_requests(1, self._rows(["A"]))[0]["setDataValidation"]["range"]
        assert rng["startColumnIndex"] == IDX[COL_PRIORITY]
        assert rng["endColumnIndex"] == IDX[COL_PRIORITY] + 1


class TestStructureRequests:
    def test_hidden_columns_are_hidden_tinted_and_protected(self):
        reqs = _v2_structure_requests(7, 7)
        # The HIDING one specifically — there is now also an explicit un-hide of
        # the visible columns, and taking whichever comes first is how the test
        # would stop noticing which block is which.
        hide = next(r for r in reqs
                    if "updateDimensionProperties" in r
                    and r["updateDimensionProperties"]["properties"]
                    .get("hiddenByUser") is True)
        assert hide["updateDimensionProperties"]["properties"]["hiddenByUser"] is True
        assert hide["updateDimensionProperties"]["range"]["startIndex"] == N_VISIBLE
        assert hide["updateDimensionProperties"]["range"]["endIndex"] == N_ALL

        # Protection detail lives in TestHiddenColumnsAreLocked — it depends on
        # whether a bot address is configured.
        assert any("addProtectedRange" in r for r in reqs)

    def test_protection_never_covers_a_visible_column(self):
        """Locking anything in A..G would fight the person the file is for."""
        prot = next(r for r in _v2_structure_requests(7, 7) if "addProtectedRange" in r)
        assert (prot["addProtectedRange"]["protectedRange"]["range"]
                ["startColumnIndex"] == N_VISIBLE)

    def test_every_rule_is_scoped_by_the_kind_column(self):
        """Rule 0 styles PROJECT rows, the rest style ACTION rows. Nothing may
        colour a row without first asking what kind it is."""
        rules = _conditional_format_rules(7, 7)
        formulas = [r["addConditionalFormatRule"]["rule"]["booleanRule"]
                    ["condition"]["values"][0]["userEnteredValue"] for r in rules]
        assert formulas[0] == f'=${KIND_A1}4="P"'
        for f in formulas[1:]:
            assert f'${KIND_A1}4="A"' in f, f

    def test_rule_precedence_is_bad_date_then_overdue_then_due_soon(self):
        """First match wins. An unreadable date must not be evaluated as
        overdue, and overdue must beat due-soon."""
        rules = _conditional_format_rules(7, 7)
        formulas = [r["addConditionalFormatRule"]["rule"]["booleanRule"]
                    ["condition"]["values"][0]["userEnteredValue"] for r in rules]
        assert "ISERROR" in formulas[1]
        assert "<TODAY()" in formulas[2]
        assert ">=TODAY()" in formulas[3]
        assert ([r["addConditionalFormatRule"]["index"] for r in rules]
                == list(range(len(rules))))

    def test_due_soon_window_follows_the_setting(self):
        rules = _conditional_format_rules(7, 14)
        formula = (rules[3]["addConditionalFormatRule"]["rule"]["booleanRule"]
                   ["condition"]["values"][0]["userEnteredValue"])
        assert "TODAY()+14" in formula

    def test_date_expression_is_locale_proof(self):
        """DATEVALUE would read 05/08 as 8 May under a US locale."""
        expr = _ddmmyyyy("E")
        assert "DATEVALUE" not in expr
        assert "RIGHT($E4,4)" in expr and "LEFT($E4,2)" in expr

    def test_the_date_rules_read_the_date_column_wherever_it_sits(self):
        """These formulas hard-coded $E4 and $H4. Adding two columns moved both,
        so every rule would have coloured the wrong cell — silently, because a
        conditional format cannot fail loudly."""
        rules = _conditional_format_rules(7, 7)
        formulas = [r["addConditionalFormatRule"]["rule"]["booleanRule"]
                    ["condition"]["values"][0]["userEnteredValue"] for r in rules]
        assert all(f"${DATE_A1}4" in f for f in formulas[1:5]), formulas

    def test_a_note_on_every_visible_header(self):
        req = _header_note_requests(3)[0]["updateCells"]
        assert req["fields"] == "note"
        assert len(req["rows"][0]["values"]) == len(VISIBLE_HEADERS)
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


class TestVisualFeedbackRound2:
    """Eyal's review of the live file, 2026-08-07."""

    def _rows(self, kinds, action_text=""):
        out = []
        for i, k in enumerate(kinds):
            row = [""] * N_VISIBLE + [k, f"u{i}", "p", "", ""]
            row[IDX[COL_ACTION]] = action_text
            out.append(row)
        return out

    def test_project_rows_get_a_fence_but_NO_static_paint(self):
        """The band and bold come from the conditional rule. A STATIC background
        is inherited by any row inserted beneath it — by the system with
        inheritFromBefore, or by Nechama pressing "insert row" — so the first
        action under every project arrived tinted and bold. Patching the format
        reset would only have covered the system's inserts. [2026-08-08]"""
        from services.project_status_sheet import _project_row_requests
        reqs = _project_row_requests(9, self._rows(["P", "A", "A", "P"]))
        assert [next(iter(r)) for r in reqs] == ["updateBorders"] * 2
        assert not [r for r in reqs if "repeatCell" in r]

    def test_action_rows_get_neither(self):
        from services.project_status_sheet import _project_row_requests
        assert _project_row_requests(9, self._rows(["A", "A"])) == []

    def test_the_provenance_marker_is_bolded_in_place(self):
        """Cell formatting styles the WHOLE cell — only textFormatRuns can bold
        a range of characters inside one."""
        from services.project_status_sheet import _marker_bold_requests
        rows = self._rows(["A"], action_text="Chase NCPB [auto · Weekly · 04/08/2026]")
        req = _marker_bold_requests(9, rows)[0]["updateCells"]
        runs = req["rows"][0]["values"][0]["textFormatRuns"]
        assert req["fields"] == "textFormatRuns"       # never touches the value
        assert runs[0]["format"]["bold"] is False
        assert runs[1]["startIndex"] == len("Chase NCPB ")
        assert runs[1]["format"]["bold"] is True

    def test_an_action_with_no_marker_is_skipped(self):
        from services.project_status_sheet import _marker_bold_requests
        assert _marker_bold_requests(9, self._rows(["A"], "Her own line")) == []

    def test_a_marker_at_position_zero_is_skipped(self):
        """Nothing to distinguish it from — bolding the whole cell is not the ask."""
        from services.project_status_sheet import _marker_bold_requests
        assert _marker_bold_requests(9, self._rows(["A"], "[auto]")) == []

    def test_an_unreadable_date_has_its_own_colour_rule_and_wins(self):
        """"if i insert in a wrong format it doesn't correct me" — the cell was
        silently ignored, which was indistinguishable from success."""
        from services.project_status_sheet import _conditional_format_rules
        rules = _conditional_format_rules(9, 7)
        bad = rules[1]["addConditionalFormatRule"]
        formula = bad["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        assert bad["index"] == 1                       # beats past-due (2)
        assert "ISERROR" in formula and f'${DATE_A1}4<>""' in formula

    def test_the_date_column_warns_on_an_unreadable_value(self):
        from services.project_status_sheet import _date_validation_requests
        reqs = _date_validation_requests(9, self._rows(["P", "A", "A"]))
        rule = reqs[0]["setDataValidation"]["rule"]
        assert rule["condition"]["type"] == "DATE_IS_VALID"
        assert rule["strict"] is False                  # warns, never blocks
        assert "12/8" in rule["inputMessage"]

    def test_project_rows_get_no_date_validation(self):
        from services.project_status_sheet import _date_validation_requests
        assert _date_validation_requests(9, self._rows(["P", "P"])) == []


class TestHiddenColumnsWarnButNeverBlock:
    """A HARD lock on the identity columns broke the file.

    Dragging a row and deleting a row both touch that row's hidden cells, and
    Sheets cannot tell "typing into a protected column" apart from "moving a row
    that contains one". Eyal hit it on checks 15 and 16: reorder and remove, the
    two most important gestures in a review, were refused outright. A warning is
    a seatbelt; a lock stopped the car."""

    def test_protection_never_hard_blocks(self):
        from services import project_status_sheet as pss
        reqs = pss._v2_structure_requests(9, 7)
        pr = next(r for r in reqs if "addProtectedRange" in r)["addProtectedRange"]["protectedRange"]
        assert pr["warningOnly"] is True
        assert "editors" not in pr

    def test_it_still_covers_only_the_hidden_columns(self):
        from services import project_status_sheet as pss
        reqs = pss._v2_structure_requests(9, 7)
        rng = next(r for r in reqs if "addProtectedRange" in r)["addProtectedRange"]["protectedRange"]["range"]
        assert rng["startColumnIndex"] == N_VISIBLE and rng["endColumnIndex"] == N_ALL


class TestHowToTab:
    def test_every_line_has_a_known_style(self):
        from services.project_status_sheet import HOWTO_BLOCK
        assert {k for k, _a, _b in HOWTO_BLOCK} <= {
            "title", "lede", "h2", "row", "note", "blank"}

    def test_it_is_two_columns_not_a_wall_of_prose(self):
        from services.project_status_sheet import HOWTO_TEXT
        assert all(len(r) == 2 for r in HOWTO_TEXT)
        assert any(a and b for a, b in HOWTO_TEXT)

    def test_section_headers_are_banded(self):
        from services.project_status_sheet import _howto_format_requests
        reqs = _howto_format_requests(4)
        assert sum(1 for r in reqs if "repeatCell" in r) > 10

    def test_it_explains_the_purple_date(self):
        from services.project_status_sheet import HOWTO_TEXT
        flat = " ".join(a + " " + b for a, b in HOWTO_TEXT)
        assert "Purple" in flat and "could not be read" in flat

    def test_it_states_the_day_first_rule(self):
        from services.project_status_sheet import HOWTO_TEXT
        flat = " ".join(a + " " + b for a, b in HOWTO_TEXT)
        assert "05/08 is 5 August" in flat


class TestNewContentIsStyledAutomatically:
    """Eyal: "I want the new project distinctions to also work once we add
    manually or through meeting transcription summaries — every time content is
    added there."

    Per-row painting can only ever catch up on the next cycle. A conditional
    rule keyed on the hidden _kind column styles a row the instant it becomes a
    project, with no API call at all.
    """

    def test_the_project_band_is_a_rule_not_per_row_paint(self):
        from services.project_status_sheet import _conditional_format_rules
        rule = _conditional_format_rules(9, 7)[0]["addConditionalFormatRule"]["rule"]
        assert (rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
                == f'=${KIND_A1}4="P"')
        assert rule["booleanRule"]["format"]["textFormat"]["bold"] is True
        assert rule["booleanRule"]["format"]["backgroundColor"]

    def test_the_band_covers_every_visible_column(self):
        from services.project_status_sheet import _conditional_format_rules
        rng = _conditional_format_rules(9, 7)[0]["addConditionalFormatRule"]["rule"]["ranges"][0]
        assert rng["startColumnIndex"] == 0 and rng["endColumnIndex"] == N_VISIBLE

    def test_the_band_range_is_open_ended_so_new_rows_inherit_it(self):
        """A fixed endRowIndex would stop styling rows added past the bottom."""
        from services.project_status_sheet import _conditional_format_rules
        rng = _conditional_format_rules(9, 7)[0]["addConditionalFormatRule"]["rule"]["ranges"][0]
        assert rng["startRowIndex"] == 3 and "endRowIndex" not in rng


class TestNewRowFormatting:
    """A row that appears BETWEEN rebuilds must get the same treatment as one
    written at cutover — otherwise a line typed in the morning has no checkbox
    until 02:00, and an auto-injected row looks different from a manual one."""

    def test_a_new_action_row_gets_checkbox_and_date_validation(self):
        from services.project_status_sheet import new_row_format_requests
        reqs = new_row_format_requests(9, 12, KIND_ACTION, "Chase it")
        kinds = [next(iter(r)) for r in reqs]
        assert kinds.count("setDataValidation") == 3
        conds = [r["setDataValidation"]["rule"]["condition"]["type"]
                 for r in reqs if "setDataValidation" in r]
        assert set(conds) == {"BOOLEAN", "DATE_IS_VALID", "ONE_OF_LIST"}

    def test_a_new_action_row_is_not_left_bold(self):
        """insertDimension inherits from above; under an empty project block
        that means the bold project row."""
        from services.project_status_sheet import new_row_format_requests
        reqs = new_row_format_requests(9, 12, KIND_ACTION, "x")
        reset = next(r for r in reqs if "repeatCell" in r)
        assert reset["repeatCell"]["cell"]["userEnteredFormat"]["textFormat"]["bold"] is False

    def test_an_injected_row_gets_its_marker_bolded_too(self):
        from services.project_status_sheet import new_row_format_requests
        reqs = new_row_format_requests(9, 12, KIND_ACTION, "Chase it [auto · W · 04/08]")
        runs = next(r for r in reqs if "updateCells" in r)["updateCells"]
        assert runs["fields"] == "textFormatRuns"
        assert runs["rows"][0]["values"][0]["textFormatRuns"][1]["format"]["bold"] is True

    def test_a_new_project_row_gets_its_fence(self):
        """The band comes free from the conditional rule; the border cannot."""
        from services.project_status_sheet import new_row_format_requests
        reqs = new_row_format_requests(9, 12, KIND_PROJECT)
        assert len(reqs) == 1 and "updateBorders" in reqs[0]
        assert reqs[0]["updateBorders"]["top"]["style"] == "SOLID_THICK"

    def test_a_new_project_row_gets_no_checkbox(self):
        from services.project_status_sheet import new_row_format_requests
        assert not [r for r in new_row_format_requests(9, 12, KIND_PROJECT)
                    if "setDataValidation" in r]

    def test_the_row_number_is_honoured(self):
        from services.project_status_sheet import new_row_format_requests
        reqs = new_row_format_requests(9, 12, KIND_ACTION, "x")
        assert all(r[next(iter(r))]["range"]["startRowIndex"] == 11 for r in reqs)


class TestUnfiledWorkIsSurfaced:
    """The one gap this sheet cannot show by itself.

    It is project-centric, so a task with no project_id does not appear on it at
    all — no error, no empty row, just absence. Every other gap announces
    itself; this one is silent, and silence in a review reads as "nothing to
    see". Tier 4 catches most of it via the area's Others bucket, but a task
    whose category is not an area at all (e.g. "General") has no bucket to fall
    into.
    """

    @pytest.mark.asyncio
    async def test_unfiled_tasks_are_counted(self):
        from schedulers import project_status_scheduler as mod

        with patch.object(mod.settings, "PROJECT_STATUS_V2_LAYOUT", True), \
             patch.object(mod.settings, "PROJECT_STATUS_SHEET_ID", "sid"), \
             patch("services.supabase_client.supabase_client") as sb:
            sb.get_open_tasks_by_project.return_value = {
                "p1": [{"id": "t1"}], None: [{"id": "t9"}, {"id": "t8"}]}
            sb.get_canonical_projects.return_value = [
                {"id": "p1", "name": "Live", "status": "active"}]
            result = await mod.project_status_scheduler.refresh(notify=False)

        assert result["unfiled"] == 2
        assert result["actions"] == 1        # unfiled are NOT counted as on-sheet

    @pytest.mark.asyncio
    async def test_nothing_unfiled_reports_zero(self):
        from schedulers import project_status_scheduler as mod

        with patch.object(mod.settings, "PROJECT_STATUS_V2_LAYOUT", True), \
             patch.object(mod.settings, "PROJECT_STATUS_SHEET_ID", "sid"), \
             patch("services.supabase_client.supabase_client") as sb:
            sb.get_open_tasks_by_project.return_value = {"p1": [{"id": "t1"}]}
            sb.get_canonical_projects.return_value = [
                {"id": "p1", "name": "Live", "status": "active"}]
            result = await mod.project_status_scheduler.refresh(notify=False)

        assert result["unfiled"] == 0


class TestNothingCanInheritTheProjectTint:
    """The whole point of moving the band to a conditional rule."""

    def test_the_band_is_only_ever_conditional(self):
        from services.project_status_sheet import (
            _conditional_format_rules, _project_row_requests)
        rule = _conditional_format_rules(9, 7)[0]["addConditionalFormatRule"]["rule"]
        assert rule["booleanRule"]["format"]["backgroundColor"]
        static = _project_row_requests(
            9, [[""] * N_VISIBLE + ["P", "u", "u", "", ""]])
        assert not any("repeatCell" in r for r in static)

    def test_a_new_action_row_needs_no_background_reset(self):
        """Because there is no static background above it to inherit."""
        from services.project_status_sheet import new_row_format_requests
        reqs = new_row_format_requests(9, 12, KIND_ACTION, "x")
        fields = [r["repeatCell"]["fields"] for r in reqs if "repeatCell" in r]
        assert all("backgroundColor" not in f for f in fields)


class TestTheVisibleColumnsAreAsserted:
    """Priority and Comments were INVISIBLE on the live sheet for a whole
    session. Every value was correct in the sheet and in the database — the
    formatting step said only "hide/tint/protect the system block" and nothing
    ever said what should be VISIBLE, so when the block moved from 8-12 to 10-14
    the two columns it vacated kept the old hidden + white-8pt styling.

    An operation that sets a state must describe the WHOLE state, or it silently
    inherits whatever the last version left behind."""

    def _reqs(self):
        return _v2_structure_requests(7, 7)

    def test_the_visible_columns_are_explicitly_unhidden(self):
        unhide = [r["updateDimensionProperties"] for r in self._reqs()
                  if "updateDimensionProperties" in r
                  and r["updateDimensionProperties"]["properties"].get(
                      "hiddenByUser") is False]
        assert len(unhide) == 1
        rng = unhide[0]["range"]
        assert rng["startIndex"] == 0 and rng["endIndex"] == N_VISIBLE

    def _body_reset(self):
        """Selected by WHAT IT SETS, not by how wide it is. Keying on
        `endColumnIndex == N_VISIBLE` also matched the per-column alignment
        request for the last column the moment one was added."""
        resets = [r["repeatCell"] for r in self._reqs()
                  if "repeatCell" in r
                  and "backgroundColor" in r["repeatCell"]["fields"]
                  and "bold" in r["repeatCell"]["fields"]]
        assert len(resets) == 1
        return resets[0]["cell"]["userEnteredFormat"]

    def test_the_visible_body_text_is_explicitly_readable(self):
        """White-on-white 8pt was left behind on the vacated columns too, so
        un-hiding alone would still have shown nothing."""
        fmt = self._body_reset()["textFormat"]
        assert fmt["fontSize"] == 11
        assert fmt["foregroundColor"] != {"red": 1, "green": 1, "blue": 1}

    def test_the_body_starts_plain_so_old_paint_cannot_survive(self):
        """values().clear() removes values and nothing else. When the rebuild
        rewrote the tabs, the static band that used to mark project rows stayed
        at those row NUMBERS — and the task now sitting there rendered bold and
        tinted, which is the block-structure signal inverted. Four of them on
        one tab."""
        fmt = self._body_reset()
        assert fmt["textFormat"]["bold"] is False
        assert fmt["backgroundColor"] == {"red": 1, "green": 1, "blue": 1}

    def test_the_project_band_still_comes_from_the_rule(self):
        """Resetting the body must not mean painting project rows statically
        again — an inserted row inherits static paint, which is why the band
        became a conditional rule in the first place."""
        from services.project_status_sheet import _project_row_requests
        rows = [[""] * N_VISIBLE + ["P", "u", "u", "", ""]]
        assert not any("repeatCell" in r for r in _project_row_requests(9, rows))
        rule = _conditional_format_rules(9, 7)[0]["addConditionalFormatRule"]
        assert rule["rule"]["booleanRule"]["format"]["textFormat"]["bold"] is True

    def test_it_is_idempotent_across_layouts(self):
        """Run against a sheet formatted for ANY older layout and the result is
        the same — that is what makes it self-correcting rather than a one-off
        repair."""
        a = self._reqs()
        b = self._reqs()
        assert a == b

    def test_the_system_block_is_still_hidden_and_protected(self):
        reqs = self._reqs()
        hide = [r["updateDimensionProperties"] for r in reqs
                if "updateDimensionProperties" in r
                and r["updateDimensionProperties"]["properties"].get(
                    "hiddenByUser") is True]
        assert hide and hide[0]["range"]["startIndex"] == N_VISIBLE
        assert hide[0]["range"]["endIndex"] == N_ALL
        assert any("addProtectedRange" in r for r in reqs)

    def test_no_visible_column_is_ever_in_the_hidden_range(self):
        """The mechanical form of the bug: a visible header must never fall
        inside the block that gets hidden."""
        for name in VISIBLE_HEADERS:
            assert IDX[name] < N_VISIBLE, name


class TestStaleFormattingCannotSurviveALayoutChange:
    """Formatting is per-cell and `values().clear()` does not touch it, so a
    rebuild leaves the PREVIOUS layout's validation and borders sitting at
    whatever row and column they were applied to. Both were reported from the
    live file: `To do` raising "invalid date" because it now sits where `Date`
    used to, and action rows carrying the project block's blue fence across
    seven columns instead of nine."""

    def _reqs(self):
        return _v2_structure_requests(7, 7)

    def test_the_body_validation_is_cleared_first(self):
        clears = [r["setDataValidation"] for r in self._reqs()
                  if "setDataValidation" in r and "rule" not in r["setDataValidation"]]
        assert len(clears) == 1
        rng = clears[0]["range"]
        assert rng["startRowIndex"] == FIRST_BODY_ROW - 1
        assert rng["startColumnIndex"] == 0 and rng["endColumnIndex"] == N_VISIBLE

    def test_the_body_borders_are_cleared_first(self):
        clears = [r["updateBorders"] for r in self._reqs()
                  if "updateBorders" in r
                  and r["updateBorders"].get("top", {}).get("style") == "NONE"]
        assert len(clears) == 1
        assert clears[0]["range"]["startRowIndex"] == FIRST_BODY_ROW - 1

    def test_the_fence_is_re_applied_only_to_project_rows(self):
        from services.project_status_sheet import _project_row_requests
        rows = [[""] * N_VISIBLE + [k, "u", "u", "", ""] for k in ("P", "A", "A", "P")]
        reqs = _project_row_requests(9, rows)
        assert len(reqs) == 2
        for r in reqs:
            assert r["updateBorders"]["range"]["endColumnIndex"] == N_VISIBLE


class TestAlignmentIsDeliberate:
    """Eyal: "some that are aligned to the left and some to the right". Dates
    default RIGHT and text defaults LEFT, so a tab nobody aligned looks
    hand-aligned at random."""

    def _map(self):
        from services.project_status_sheet import _alignment_requests
        out = {}
        for r in _alignment_requests(7):
            i = r["repeatCell"]["range"]["startColumnIndex"]
            out[ALL_HEADERS[i]] = r["repeatCell"]["cell"]["userEnteredFormat"]
        return out

    def test_every_visible_column_gets_one(self):
        assert set(self._map()) == set(VISIBLE_HEADERS)

    def test_every_column_centres(self):
        """Including the sentence-shaped ones. They were left-aligned on the
        first pass and Eyal asked for them centred anyway — his file, and a
        consistent grid beats the typographic argument."""
        assert all(f["horizontalAlignment"] == "CENTER"
                   for f in self._map().values())

    def test_everything_is_vertically_middle(self):
        """Top-aligned short cells float away from the wrapped text they
        belong to."""
        assert all(f["verticalAlignment"] == "MIDDLE"
                   for f in self._map().values())


class TestEveryPriorityHasAColour:
    """Only Urgent was tinted, which left the other three indistinguishable and
    made the column decorative."""

    def _rules(self):
        return [r["addConditionalFormatRule"]["rule"]
                for r in _conditional_format_rules(7, 7)
                if r["addConditionalFormatRule"]["rule"]["ranges"][0]
                ["startColumnIndex"] == IDX[COL_PRIORITY]]

    def test_one_rule_per_level(self):
        from services.project_status_rows import PRIORITIES
        assert len(self._rules()) == len(PRIORITIES)

    def test_the_colours_all_differ(self):
        seen = [tuple(sorted(r["booleanRule"]["format"]["backgroundColor"].items()))
                for r in self._rules()]
        assert len(set(seen)) == len(seen)

    def test_the_scale_is_shared_with_the_meetings_tab(self):
        """One scale meaning one thing everywhere is the point of sharing it."""
        import services.google_sheets as gs
        from services.project_status_rows import PRIORITIES, PRIORITY_COLORS
        assert tuple(gs.MEETING_PRIORITIES) == tuple(PRIORITIES)
        assert set(PRIORITY_COLORS) == set(PRIORITIES)

    def test_a_finished_row_stops_advertising_its_priority(self):
        assert all("NOT($A4=TRUE)" in
                   r["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
                   for r in self._rules())


class TestBatchTwoReviewFindings:
    """2026-08-09 max-effort review, second batch."""

    def test_the_border_wipe_stays_inside_the_grid(self):
        """FINDING 4. A bare `addSheet` allocates 1000 rows, and a BOUNDED range
        past the end of the grid is rejected outright — `Range ... exceeds grid
        limits`. batchUpdate is atomic, so one such request discards the ENTIRE
        formatting batch and the tab keeps exactly the stale state the wipe
        exists to clear."""
        wipe = next(r["updateBorders"] for r in _v2_structure_requests(7, 7)
                    if "updateBorders" in r
                    and r["updateBorders"].get("top", {}).get("style") == "NONE")
        assert wipe["range"]["endRowIndex"] == 1000

    def test_a_taller_grid_is_wiped_in_full(self):
        wipe = next(r["updateBorders"] for r in
                    _v2_structure_requests(7, 7, row_count=4200)
                    if "updateBorders" in r
                    and r["updateBorders"].get("top", {}).get("style") == "NONE")
        assert wipe["range"]["endRowIndex"] == 4200

    def test_the_meetings_wipe_stays_inside_its_grid_too(self):
        import services.google_sheets as gs
        wipe = next(r["updateBorders"] for r in
                    gs.sheets_service.meetings_format_requests(
                        1, gs.MEETING_TRACKER_HEADERS)
                    if "updateBorders" in r
                    and r["updateBorders"].get("top", {}).get("style") == "NONE")
        assert wipe["range"]["endRowIndex"] == 1000

    def test_urgent_outranks_high_in_the_tasks_tab_sort(self):
        """FINDING 7. `_PRI_SCORE` had no 'U', so Urgent scored exactly as
        Medium and sorted BELOW every High task."""
        from services.google_sheets import _PRI_SCORE
        assert _PRI_SCORE["U"] > _PRI_SCORE["H"] > _PRI_SCORE["M"] > _PRI_SCORE["L"]

    def test_urgent_is_the_most_emphasised_in_telegram(self):
        """It fell through to the default, so the one Urgent task was the only
        one in the list with no emphasis at all."""
        import inspect
        import services.telegram_bot as tb
        src = inspect.getsource(tb)
        assert '{"U": "!!! ", "H": "!! ", "M": "", "L": "~ "}' in src

    def test_the_repair_script_no_longer_strips_the_tick_boxes(self):
        """FINDING 5. It called `_v2_structure_requests` alone, which wipes
        validation — with nothing to put the tick boxes, dropdowns, date rules
        and project fences back."""
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path(
            "scripts/fix_project_status_columns.py").read_text(encoding="utf-8"))
        # By IMPORT, not by substring — the name is discussed in the docstring,
        # and a test that reads prose is testing the prose.
        imported = {a.name for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    for a in node.names}
        assert "_v2_structure_requests" not in imported
        assert "run" in imported          # delegates to restyle_workbook.run

    def test_the_restyle_script_has_the_layout_guard(self):
        """FINDING 11. Without it a tab on an older layout has every checkbox
        wiped and none restored, because no row classifies as an action."""
        import pathlib
        src = pathlib.Path("scripts/restyle_workbook.py").read_text(
            encoding="utf-8")
        assert "unresolved_columns" in src
        assert "SKIPPED, " in src


class TestATypedPriorityReachesTheDatabase:
    """FINDING 3. `_handle_action` collected the priority into the create spec
    and a test asserted the plan carried it, but `_create_entity` never passed
    it on — so create_task applied its own 'M' default and the next cycle pushed
    "M" back into the cell she had typed "Urgent" into."""

    def test_create_entity_forwards_the_priority(self):
        import inspect
        from processors import project_status_reconcile as psr
        src = inspect.getsource(psr._create_entity)
        assert 'priority=spec.get("priority")' in src

    def test_the_plan_and_the_create_agree_on_the_key(self):
        """The spec key the planner writes must be the one the creator reads —
        a mismatch here is silent."""
        import inspect
        from processors import project_status_reconcile as psr
        assert '"priority": _to_db_priority(' in inspect.getsource(psr._handle_action)
        assert 'spec.get("priority")' in inspect.getsource(psr._create_entity)


class TestTheSheetSaysWhatItWillNotKeep:
    """A cell nothing reads must not look like a cell that is saved. The same
    instinct as the purple unreadable-date: answer at the moment of typing,
    rather than thirty minutes later — or, as happened here, never."""

    def _rule(self):
        rules = [r["addConditionalFormatRule"]["rule"]
                 for r in _conditional_format_rules(7, 7)
                 if r["addConditionalFormatRule"]["rule"]["ranges"][0]
                 ["startColumnIndex"] == IDX[COL_TODO]]
        assert len(rules) == 1
        return rules[0]

    def test_to_do_on_an_action_row_is_tinted(self):
        f = self._rule()["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        assert f'{_a1(IDX[COL_KIND])}4="A"' in f
        assert f'${_a1(IDX[COL_TODO])}4<>""' in f

    def test_it_does_not_tint_the_project_row(self):
        """That is where the objective belongs."""
        f = self._rule()["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        assert '="P"' not in f

    def test_its_colour_is_its_own(self):
        """Distinct from past-due red and unreadable-date purple — it means
        something different: not wrong, just not kept."""
        from services.project_status_sheet import _BAD_DATE, _RED, _IGNORED
        bg = self._rule()["booleanRule"]["format"]["backgroundColor"]
        assert bg == _IGNORED and bg != _BAD_DATE and bg != _RED

    def test_the_header_note_warns_about_it(self):
        req = _header_note_requests(3)[0]["updateCells"]
        note = req["rows"][0]["values"][IDX[COL_TODO]]["note"]
        assert "PROJECT ROW" in note.upper()
