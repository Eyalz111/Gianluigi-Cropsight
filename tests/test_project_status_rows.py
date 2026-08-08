"""Pure row/column model for the Project Status sheet (v2, 2026-08-07).

The sheet is Nechama's working surface, so the load-bearing property is that
the system can always tell HER rows from its own without depending on where a
row sits. These tests pin that.
"""

import pytest

from services.project_status_rows import (
    ALL_HEADERS, AMBIGUOUS, FIRST_BODY_ROW, HUMAN_ACTION, HUMAN_PROJECT,
    INCOMPLETE, KIND_ACTION, KIND_PROJECT, ORIGIN_AUTO, ORIGIN_MANUAL,
    PRIORITY_TO_DB, SPACER, SYSTEM_ACTION, SYSTEM_PROJECT, VISIBLE_HEADERS,
    COL_ACTION, COL_COMMENTS, COL_DATE, COL_KIND, COL_NUM, COL_ORIGIN,
    COL_PARENT, COL_PRIORITY, COL_PROJECT, COL_RESP, COL_SRC, COL_TODO,
    COL_TOPIC, COL_UID,
    action_row_values, classify_row, find_duplicate_uids, format_provenance,
    is_checked, parse_provenance, parse_tab, project_row_values,
    resolve_columns, strip_provenance, tab_fingerprint,
)

HDR = list(ALL_HEADERS)
COLS = {name: i for i, name in enumerate(ALL_HEADERS)}


def _grid(*body_rows):
    """A tab: two title rows, the header row, then the body."""
    return [["Kenya", "", "", "Confidential"], ["Distribution:", "", "", ""], HDR,
            *body_rows]


# Fixtures go through the SAME builders the engine uses, so a column added to
# ALL_HEADERS cannot leave these asserting against shifted cells.
def _project(name, uid, todo="", date="", resp="", notes="", num="1"):
    return project_row_values(
        {"id": uid, "name": name, "objective": todo, "target_date": date,
         "owner": resp, "notes": notes}, num=num)


def _action(text, uid, parent, date="", resp="", comments="",
            origin=ORIGIN_AUTO, src="", checked="", topic="", priority=""):
    return action_row_values(
        {"id": uid, "title": text, "label": topic, "deadline": date,
         "assignee": resp, "notes": comments, "priority": priority,
         "meeting_id": src, "_origin": origin},
        parent_uid=parent, checked=checked)


def _human(action="", project="", topic="", todo="", date="", resp="",
           notes="", priority=""):
    """A typed row: the visible cells only, no identity columns."""
    row = [""] * len(VISIBLE_HEADERS)
    for name, value in ((COL_PROJECT, project), (COL_TOPIC, topic),
                        (COL_ACTION, action), (COL_TODO, todo),
                        (COL_DATE, date), (COL_RESP, resp),
                        (COL_PRIORITY, priority), (COL_COMMENTS, notes)):
        row[COLS[name]] = value
    return row


def _blank():
    return [""] * len(VISIBLE_HEADERS)


class TestColumnResolution:
    def test_resolves_by_header_text(self):
        cols = resolve_columns(HDR)
        assert cols[COL_PROJECT] == COLS[COL_PROJECT]
        assert cols[COL_UID] == COLS[COL_UID]

    def test_survives_an_inserted_column(self):
        """A human inserting a column must not silently shift the whole map."""
        shifted = ["#", "NEW", *ALL_HEADERS[1:]]
        cols = resolve_columns(shifted)
        assert cols[COL_PROJECT] == COLS[COL_PROJECT] + 1
        assert cols[COL_ACTION] == COLS[COL_ACTION] + 1
        assert cols[COL_UID] == COLS[COL_UID] + 1

    def test_tolerates_punctuation_and_case(self):
        """'Resp.' and 'resp' must match — both spellings occur in the wild."""
        cols = resolve_columns([h.lower().replace(".", "") for h in ALL_HEADERS])
        assert cols[COL_RESP] == COLS[COL_RESP]
        assert cols[COL_TODO] == COLS[COL_TODO]
        assert cols[COL_COMMENTS] == COLS[COL_COMMENTS]

    def test_missing_header_falls_back_to_default_position(self):
        cols = resolve_columns([])
        assert cols[COL_PROJECT] == COLS[COL_PROJECT]
        assert cols[COL_COMMENTS] == COLS[COL_COMMENTS]


class TestClassification:
    def _cols(self):
        return resolve_columns(HDR)

    def test_system_rows_declare_themselves(self):
        c = self._cols()
        assert classify_row(_project("Kenya", "p1"), c) == SYSTEM_PROJECT
        assert classify_row(_action("Chase", "t1", "p1"), c) == SYSTEM_ACTION

    def test_human_action_is_a_row_with_action_text(self):
        c = self._cols()
        row = _human(action="Call the bank", date="12/08", resp="Paolo")
        assert classify_row(row, c) == HUMAN_ACTION

    def test_human_project_is_the_project_cell_filled(self):
        c = self._cols()
        assert classify_row(_human(project="New Vertical"), c) == HUMAN_PROJECT

    def test_a_topic_alone_is_not_a_project(self):
        """Until 2026-08-09 the project name and the topic shared one column, so
        tagging an action's topic with nothing beside it CREATED A PROJECT.
        Eyal hit it: 'AWS expected expenses' became a project when he meant an
        action. Topic alone is now an unfinished row, not a new project."""
        c = self._cols()
        assert classify_row(_human(topic="AWS expected expenses"), c) != HUMAN_PROJECT

    def test_both_filled_is_ambiguous_and_never_guessed(self):
        c = self._cols()
        row = _human(project="New Vertical", action="Call the bank")
        assert classify_row(row, c) == AMBIGUOUS

    def test_incomplete_row_is_never_written(self):
        """Date/Resp. but no Action — a task with no title is worse than none."""
        c = self._cols()
        assert classify_row(_human(date="12/08", resp="Paolo"), c) == INCOMPLETE

    def test_a_priority_alone_is_incomplete_not_a_task(self):
        c = self._cols()
        assert classify_row(_human(priority="Urgent"), c) == INCOMPLETE

    def test_blank_row_is_a_spacer(self):
        c = self._cols()
        assert classify_row(_blank(), c) == SPACER
        assert classify_row([], c) == SPACER

    def test_classification_never_depends_on_position(self):
        """The same cells classify the same wherever the row sits."""
        c = self._cols()
        row = _human(action="Call the bank")
        assert classify_row(row, c) == classify_row(row, c) == HUMAN_ACTION


class TestPriorityVocabulary:
    """The Priority column replaces the Tasks tab's, so the four levels have to
    survive the round trip in both spellings."""

    def test_sheet_spelling_maps_to_the_stored_letter(self):
        assert PRIORITY_TO_DB["urgent"] == "U"
        assert PRIORITY_TO_DB["h"] == "H" and PRIORITY_TO_DB["high"] == "H"
        assert PRIORITY_TO_DB["l"] == "L" and PRIORITY_TO_DB["low"] == "L"

    def test_an_action_row_renders_the_stored_letter_back(self):
        row = _action("Chase", "t1", "p1", priority="U")
        assert row[COLS[COL_PRIORITY]] == "Urgent"

    def test_no_priority_renders_blank_not_a_default(self):
        """Showing 'M' for a task the database has no priority for would make
        the merge push it back every cycle, and would tell the reader the work
        was triaged when nobody triaged it."""
        assert _action("Chase", "t1", "p1")[COLS[COL_PRIORITY]] == ""


class TestRowShapeIsSharedWithTheEngine:
    """Three call sites used to hand-build these literals and they drifted —
    the pack builder left Topic blank while the database held a label, so the
    merge queued 37 phantom writes on a sheet nobody had touched."""

    def test_both_builders_emit_the_full_width(self):
        assert len(_project("Kenya", "p1")) == len(ALL_HEADERS)
        assert len(_action("Chase", "t1", "p1")) == len(ALL_HEADERS)

    def test_a_project_row_never_fills_an_action_only_column(self):
        row = _project("Kenya", "p1", todo="Win the pilot")
        assert row[COLS[COL_ACTION]] == "" and row[COLS[COL_PRIORITY]] == ""
        assert row[COLS[COL_TOPIC]] == ""

    def test_an_action_row_never_fills_a_project_only_column(self):
        row = _action("Chase", "t1", "p1")
        assert row[COLS[COL_PROJECT]] == "" and row[COLS[COL_TODO]] == ""

    def test_identity_lands_in_the_hidden_columns(self):
        proj = _project("Kenya", "p1")
        act = _action("Chase", "t1", "p1", src="m9")
        assert proj[COLS[COL_KIND]] == KIND_PROJECT
        assert proj[COLS[COL_UID]] == "p1" and proj[COLS[COL_PARENT]] == "p1"
        assert act[COLS[COL_KIND]] == KIND_ACTION
        assert act[COLS[COL_UID]] == "t1" and act[COLS[COL_PARENT]] == "p1"
        assert act[COLS[COL_ORIGIN]] == ORIGIN_AUTO and act[COLS[COL_SRC]] == "m9"


class TestCheckbox:
    def test_true_forms(self):
        for v in (True, "TRUE", "true", "Yes", "x", "✓", "done"):
            assert is_checked(v), v

    def test_false_forms(self):
        for v in (False, "", "FALSE", None, "no", "1"):
            assert not is_checked(v), v

    def test_project_row_number_is_not_a_tick(self):
        """Column A is the '#' on a project row — a numeral must not read as done."""
        cols = resolve_columns(HDR)
        blocks, _, _ = parse_tab(_grid(_project("Kenya", "p1", num="1")))
        assert blocks[0].project.checked is False

    def test_action_row_tick_is_read(self):
        blocks, _, _ = parse_tab(_grid(
            _project("Kenya", "p1"),
            _action("Chase NCPB", "t1", "p1", checked="TRUE"),
        ))
        assert blocks[0].actions[0].checked is True


class TestProvenance:
    def test_auto_marker_round_trips(self):
        text = format_provenance(ORIGIN_AUTO, "Weekly Sync", "04/08/2026")
        assert parse_provenance(text) == "auto"
        assert "Weekly Sync" in text

    def test_manual_marker(self):
        assert parse_provenance(format_provenance(ORIGIN_MANUAL)) == "manual"

    def test_unmarked_text_is_hers(self):
        """Absence of a marker IS the signal — it is how we avoid writing into her cell."""
        assert parse_provenance("Chase Ido on the trustee intro") is None

    def test_strip_leaves_the_action_text(self):
        marked = "Chase NCPB follow-up " + format_provenance(ORIGIN_AUTO, "Weekly", "04/08/2026")
        assert strip_provenance(marked) == "Chase NCPB follow-up"

    def test_closed_marker_carries_the_done_date(self):
        text = format_provenance(ORIGIN_AUTO, "Weekly", "04/08/2026", closed="12/08")
        assert "done 12/08" in text and parse_provenance(text) == "auto"


class TestBlockParsing:
    def test_blocks_and_their_actions(self):
        blocks, orphans, _ = parse_tab(_grid(
            _project("Kenya", "p1", todo="Win the pilot", resp="Paolo"),
            _action("Chase NCPB", "t1", "p1"),
            _action("Send terms", "t2", "p1"),
            _project("Italy", "p2", num="2"),
            _action("Book Politecnico", "t3", "p2"),
        ))
        assert [b.project.values[COL_PROJECT] for b in blocks] == ["Kenya", "Italy"]
        assert len(blocks[0].actions) == 2 and len(blocks[1].actions) == 1
        assert orphans == []
        assert blocks[0].project.values[COL_TODO] == "Win the pilot"

    def test_spacers_between_blocks_are_ignored(self):
        blocks, _, _ = parse_tab(_grid(
            _project("Kenya", "p1"),
            _action("Chase", "t1", "p1"),
            _blank(),
            _project("Italy", "p2"),
        ))
        assert len(blocks) == 2 and len(blocks[0].actions) == 1

    def test_action_above_the_first_project_is_an_orphan_not_dropped(self):
        """Losing a row she typed because it sat in the wrong place would be
        the worst possible behaviour — it is surfaced, then re-homed."""
        blocks, orphans, _ = parse_tab(_grid(
            _human(action="Stray action"),
            _project("Kenya", "p1"),
        ))
        assert len(orphans) == 1 and orphans[0].kind == HUMAN_ACTION
        assert len(blocks) == 1

    def test_row_numbers_are_absolute_sheet_rows(self):
        blocks, _, _ = parse_tab(_grid(_project("Kenya", "p1"), _action("Chase", "t1", "p1")))
        assert blocks[0].project.row_number == FIRST_BODY_ROW
        assert blocks[0].actions[0].row_number == FIRST_BODY_ROW + 1

    def test_end_row_is_the_insert_anchor(self):
        blocks, _, _ = parse_tab(_grid(
            _project("Kenya", "p1"), _action("a", "t1", "p1"), _action("b", "t2", "p1"),
        ))
        assert blocks[0].end_row == FIRST_BODY_ROW + 3

    def test_empty_tab_parses_to_nothing(self):
        blocks, orphans, _ = parse_tab([])
        assert blocks == [] and orphans == []

    def test_human_rows_mixed_into_a_system_block(self):
        blocks, _, _ = parse_tab(_grid(
            _project("Kenya", "p1"),
            _action("Auto one", "t1", "p1"),
            _human(action="Her own action", date="12/08", resp="Paolo",
                   notes="note"),
        ))
        kinds = [a.kind for a in blocks[0].actions]
        assert kinds == [SYSTEM_ACTION, HUMAN_ACTION]
        assert blocks[0].actions[1].uid == ""      # no identity yet


class TestDuplicateUids:
    def test_pasted_block_is_detected(self):
        blocks, orphans, _ = parse_tab(_grid(
            _project("Kenya", "p1"),
            _action("Chase", "t1", "p1"),
            _project("Kenya copy", "p1", num="2"),
            _action("Chase", "t1", "p1"),
        ))
        dups = find_duplicate_uids(blocks, orphans)
        assert set(dups) == {"p1", "t1"}
        # topmost keeps the identity
        assert dups["t1"][0].row_number < dups["t1"][1].row_number

    def test_no_duplicates_when_clean(self):
        blocks, orphans, _ = parse_tab(_grid(
            _project("Kenya", "p1"), _action("Chase", "t1", "p1"),
        ))
        assert find_duplicate_uids(blocks, orphans) == {}

    def test_blank_uids_are_not_duplicates(self):
        """Several hand-added rows all have no uid — that is normal, not a clash."""
        blocks, _, _ = parse_tab(_grid(
            _project("Kenya", "p1"),
            _human(action="one"),
            _human(action="two"),
        ))
        assert find_duplicate_uids(blocks) == {}


class TestFingerprint:
    def test_stable_for_the_same_layout(self):
        grid = _grid(_project("Kenya", "p1"), _action("Chase", "t1", "p1"))
        assert tab_fingerprint(*parse_tab(grid)[:2]) == tab_fingerprint(*parse_tab(grid)[:2])

    def test_changes_when_a_row_is_added(self):
        a = parse_tab(_grid(_project("Kenya", "p1"), _action("Chase", "t1", "p1")))
        b = parse_tab(_grid(_project("Kenya", "p1"), _action("Chase", "t1", "p1"),
                            _action("New", "t2", "p1")))
        assert tab_fingerprint(*a[:2]) != tab_fingerprint(*b[:2])

    def test_changes_when_rows_are_reordered(self):
        """Row numbers computed before the value pass go stale if she reorders —
        the fingerprint is what makes the structural pass notice."""
        a = parse_tab(_grid(_project("K", "p1"), _action("x", "t1", "p1"), _action("y", "t2", "p1")))
        b = parse_tab(_grid(_project("K", "p1"), _action("y", "t2", "p1"), _action("x", "t1", "p1")))
        assert tab_fingerprint(*a[:2]) != tab_fingerprint(*b[:2])

    def test_ignores_pure_text_edits(self):
        """She retyping an action must NOT trip the structural guard."""
        a = parse_tab(_grid(_project("K", "p1"), _action("Chase NCPB", "t1", "p1")))
        b = parse_tab(_grid(_project("K", "p1"), _action("Chase NCPB again", "t1", "p1")))
        assert tab_fingerprint(*a[:2]) == tab_fingerprint(*b[:2])


class TestFingerprintCatchesRowShifts:
    """The guard exists to prove row numbers are still valid. Hashing only the
    uid sequence made it blind to the single most likely mid-review edit —
    inserting a blank row — because parse_tab discards blank rows entirely. The
    sequence was unchanged while everything below had shifted, so a queued
    deleteDimension landed on the wrong line."""

    def test_a_blank_row_inserted_above_changes_the_fingerprint(self):
        before = parse_tab(_grid(_project("Kenya", "p1"),
                                 _action("Chase", "t1", "p1")))
        after = parse_tab(_grid(_blank(),
                                _project("Kenya", "p1"),
                                _action("Chase", "t1", "p1")))
        assert tab_fingerprint(*before[:2]) != tab_fingerprint(*after[:2])

    def test_a_blank_row_inserted_between_rows_changes_it(self):
        before = parse_tab(_grid(_project("Kenya", "p1"),
                                 _action("Chase", "t1", "p1")))
        after = parse_tab(_grid(_project("Kenya", "p1"),
                                _blank(),
                                _action("Chase", "t1", "p1")))
        assert tab_fingerprint(*before[:2]) != tab_fingerprint(*after[:2])

    def test_a_pure_text_edit_still_does_not_trip_it(self):
        """Retyping an action must not cancel that tab's structural work."""
        a = parse_tab(_grid(_project("K", "p1"), _action("Chase NCPB", "t1", "p1")))
        b = parse_tab(_grid(_project("K", "p1"), _action("Chase NCPB again", "t1", "p1")))
        assert tab_fingerprint(*a[:2]) == tab_fingerprint(*b[:2])


class TestHashHeaderResolves:
    def test_the_number_column_resolves_from_the_sheet(self):
        """"#" is all punctuation, so normalising stripped it to "" and the
        truthiness guard skipped it — column A could never be resolved and
        always fell back to its hard-coded index."""
        shifted = ["NEW", *ALL_HEADERS]
        cols = resolve_columns(shifted)
        assert cols[COL_NUM] == 1

    def test_a_shifted_sheet_reads_the_checkbox_from_the_right_cell(self):
        """With '#' unresolvable, every ticked row read as unticked and the
        engine wrote status='pending' back over completed work."""
        hdr = ["NEW", *ALL_HEADERS]
        # Every row shifted one column right, exactly as an inserted column
        # would leave them.
        proj = ["", *_project("Kenya", "p1", num=1)]
        act = ["", *_action("Chase", "t1", "p1", checked=True)]
        blocks, _, _ = parse_tab([["t"], ["d"], hdr, proj, act])
        assert blocks[0].actions[0].checked is True
