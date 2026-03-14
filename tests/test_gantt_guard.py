"""
Tests for guardrails/gantt_guard.py

Covers:
- Protected row rejection
- Max cells limit
- Schema validation
- Fuzzy section matching
- Cell format validation
- Range expansion
- Timeline shifts
- Missing fields
- Status validation
"""

import pytest
from unittest.mock import patch, MagicMock

from guardrails.gantt_guard import (
    is_protected,
    resolve_row_number,
    validate_cell_format,
    expand_range_changes,
    validate_proposal,
)

# ---------------------------------------------------------------------------
# Mock schema data
# ---------------------------------------------------------------------------

MOCK_SCHEMA_ROWS = [
    {
        "sheet_name": "2026-2027",
        "section": "PRODUCT & TECHNOLOGY",
        "subsection": "Execution",
        "row_number": 7,
        "protected": False,
        "notes": "execution",
        "owner_column": "C",
    },
    {
        "sheet_name": "2026-2027",
        "section": "PRODUCT & TECHNOLOGY",
        "subsection": "Planning",
        "row_number": 8,
        "protected": False,
        "notes": "planning",
    },
    {
        "sheet_name": "2026-2027",
        "section": "PRODUCT & TECHNOLOGY",
        "subsection": "Meetings",
        "row_number": 9,
        "protected": False,
        "notes": "meeting",
    },
    {
        "sheet_name": "2026-2027",
        "section": "PRODUCT & TECHNOLOGY",
        "subsection": "Milestones",
        "row_number": 10,
        "protected": False,
        "notes": "milestone",
    },
    {
        "sheet_name": "2026-2027",
        "section": "PRODUCT & TECHNOLOGY",
        "subsection": "Escalations",
        "row_number": 13,
        "protected": True,
        "notes": "section_header",
    },
    {
        "sheet_name": "_metadata",
        "section": "_config",
        "subsection": "_metadata",
        "row_number": 0,
        "protected": True,
        "notes": '{"valid_owners": ["[E]", "[R]", "[P]", "[Y]", "[E/R]", "[ALL]", "[TBD]"], "max_week": 96}',
    },
]

MOCK_METADATA = {
    "valid_owners": ["[E]", "[R]", "[P]", "[Y]", "[E/R]", "[ALL]", "[TBD]"],
    "max_week": 96,
}

# Patch targets — mock the internal helper functions, not supabase_client.client
PATCH_LOAD_SCHEMA = "guardrails.gantt_guard._load_schema"
PATCH_LOAD_META = "guardrails.gantt_guard._load_schema_metadata"


def _make_valid_change(**overrides):
    """Return a minimal valid single-cell change dict."""
    base = {
        "section": "PRODUCT & TECHNOLOGY",
        "subsection": "Execution",
        "week": 12,
        "value": "[E] In progress",
        "reason": "Team sync",
        "status": "active",
    }
    base.update(overrides)
    return base


# ===========================================================================
# is_protected
# ===========================================================================

class TestIsProtected:
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_protected_row_returns_true(self, _):
        assert is_protected("2026-2027", 13) is True

    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_non_protected_row_returns_false(self, _):
        assert is_protected("2026-2027", 7) is False

    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_unknown_row_returns_false(self, _):
        assert is_protected("2026-2027", 999) is False


# ===========================================================================
# resolve_row_number
# ===========================================================================

class TestResolveRowNumber:
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_exact_match_returns_row(self, _):
        row, section, subsection = resolve_row_number(
            "2026-2027", "PRODUCT & TECHNOLOGY", "Execution"
        )
        assert row == 7
        assert section == "PRODUCT & TECHNOLOGY"
        assert subsection == "Execution"

    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_non_existent_section_returns_none(self, _):
        row, section, subsection = resolve_row_number(
            "2026-2027", "DOES NOT EXIST", "Execution"
        )
        assert row is None

    @patch(PATCH_LOAD_SCHEMA)
    def test_exact_match_preferred_over_partial(self, mock_load):
        extra_rows = MOCK_SCHEMA_ROWS + [
            {
                "sheet_name": "2026-2027",
                "section": "PRODUCT & TECHNOLOGY EXTENDED",
                "subsection": "Execution",
                "row_number": 99,
                "protected": False,
                "notes": "execution",
            }
        ]
        mock_load.return_value = extra_rows
        row, section, subsection = resolve_row_number(
            "2026-2027", "PRODUCT & TECHNOLOGY", "Execution"
        )
        assert row == 7

    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_case_insensitive_fuzzy_match(self, _):
        """'Product & Tech' should fuzzy-match to 'PRODUCT & TECHNOLOGY'."""
        row, section, subsection = resolve_row_number(
            "2026-2027", "Product & Tech", "Execution"
        )
        assert row == 7
        assert section == "PRODUCT & TECHNOLOGY"


# ===========================================================================
# validate_cell_format
# ===========================================================================

class TestValidateCellFormat:
    def test_empty_value_always_valid(self):
        ok, err = validate_cell_format("", "execution", ["[E]", "[R]", "[E/R]"])
        assert ok is True
        assert err is None

    def test_execution_without_owner_prefix_rejected(self):
        ok, err = validate_cell_format(
            "In progress", "execution", ["[E]", "[R]", "[E/R]"]
        )
        assert ok is False
        assert "prefix" in err.lower() or "owner" in err.lower()

    def test_execution_with_valid_owner_prefix_accepted(self):
        ok, err = validate_cell_format(
            "[E/R] In progress", "execution", ["[E]", "[R]", "[E/R]"]
        )
        assert ok is True
        assert err is None

    def test_invalid_owner_prefix_rejected(self):
        ok, err = validate_cell_format(
            "[X] In progress", "execution", ["[E]", "[R]", "[E/R]"]
        )
        assert ok is False
        assert "invalid" in err.lower() or "prefix" in err.lower()

    def test_meeting_format_to_execution_row_rejected(self):
        ok, err = validate_cell_format(
            "Meeting: Sprint Review", "execution", ["[E]", "[R]", "[E/R]"]
        )
        assert ok is False

    def test_execution_format_to_meeting_row_rejected(self):
        ok, err = validate_cell_format(
            "[E] Coding sprint", "meeting", ["[E]", "[R]", "[E/R]"]
        )
        assert ok is False

    def test_milestone_valid_symbols(self):
        for symbol in ["★ Launch", "● Demo Day", "◆ Series A"]:
            ok, err = validate_cell_format(symbol, "milestone", ["[E]", "[R]"])
            assert ok is True, f"Expected {symbol!r} to be valid for milestone"
            assert err is None

    def test_milestone_invalid_value(self):
        ok, err = validate_cell_format("Launch!", "milestone", ["[E]", "[R]"])
        assert ok is False


# ===========================================================================
# expand_range_changes
# ===========================================================================

class TestExpandRangeChanges:
    def test_single_change_returned_as_is(self):
        change = _make_valid_change()
        result = expand_range_changes([change])
        assert len(result) == 1
        assert result[0]["week"] == 12

    def test_week_range_expands_to_individual_cells(self):
        change = {
            "section": "PRODUCT & TECHNOLOGY",
            "subsection": "Execution",
            "week_start": 10,
            "week_end": 12,
            "value": "[E] Sprint",
            "reason": "Sprint block",
            "status": "active",
        }
        result = expand_range_changes([change])
        weeks = [r["week"] for r in result]
        assert weeks == [10, 11, 12]
        assert len(result) == 3

    def test_range_fills_all_cells_with_same_value(self):
        change = {
            "section": "PRODUCT & TECHNOLOGY",
            "subsection": "Planning",
            "week_start": 5,
            "week_end": 8,
            "value": "[R] Planning phase",
            "reason": "Q1 planning",
            "status": "planned",
        }
        result = expand_range_changes([change])
        assert len(result) == 4
        for cell in result:
            assert cell["value"] == "[R] Planning phase"

    def test_timeline_shift_as_two_ranges(self):
        """Timeline shift is composed as clear old + fill new in one proposal."""
        clear_old = {
            "section": "PRODUCT & TECHNOLOGY",
            "subsection": "Execution",
            "week_start": 9,
            "week_end": 10,
            "value": "",
            "reason": "Shift timeline",
            "status": "",
        }
        fill_new = {
            "section": "PRODUCT & TECHNOLOGY",
            "subsection": "Execution",
            "week_start": 11,
            "week_end": 12,
            "value": "[E] Shifted work",
            "reason": "Shift timeline",
            "status": "active",
        }
        result = expand_range_changes([clear_old, fill_new])
        assert len(result) == 4
        clear_cells = [r for r in result if r["value"] == ""]
        fill_cells = [r for r in result if r["value"] != ""]
        assert len(clear_cells) == 2
        assert len(fill_cells) == 2


# ===========================================================================
# validate_proposal
# ===========================================================================

class TestValidateProposal:
    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_valid_proposal_passes(self, _, __):
        changes = [_make_valid_change()]
        ok, errors = validate_proposal(changes, "2026-2027")
        assert ok is True
        assert errors == []

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_protected_row_rejected(self, _, __):
        changes = [_make_valid_change(subsection="Escalations")]
        ok, errors = validate_proposal(changes, "2026-2027")
        assert ok is False
        assert any("protected" in e.lower() for e in errors)

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_max_cells_limit_exceeded(self, _, __):
        with patch("config.settings.settings") as mock_settings:
            mock_settings.GANTT_MAIN_TAB = "2026-2027"
            mock_settings.GANTT_MAX_CELLS_PER_PROPOSAL = 20
            changes = [_make_valid_change(week=i) for i in range(10, 35)]
            ok, errors = validate_proposal(changes, "2026-2027")
        assert ok is False
        assert any("cell" in e.lower() or "max" in e.lower() for e in errors)

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_missing_section_in_schema(self, _, __):
        changes = [_make_valid_change(section="NONEXISTENT SECTION")]
        ok, errors = validate_proposal(changes, "2026-2027")
        assert ok is False
        assert len(errors) > 0

    @patch(PATCH_LOAD_META, return_value={})
    @patch(PATCH_LOAD_SCHEMA, return_value=[])
    def test_empty_schema_gives_clear_error(self, _, __):
        changes = [_make_valid_change()]
        ok, errors = validate_proposal(changes, "2026-2027")
        assert ok is False
        assert any("schema" in e.lower() or "parser" in e.lower() for e in errors)

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_out_of_range_week_rejected(self, _, __):
        changes = [_make_valid_change(week=200)]
        ok, errors = validate_proposal(changes, "2026-2027")
        assert ok is False
        assert any("W200" in e or "range" in e.lower() for e in errors)

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_missing_required_field_section(self, _, __):
        change = _make_valid_change()
        del change["section"]
        ok, errors = validate_proposal([change], "2026-2027")
        assert ok is False

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_missing_required_field_subsection(self, _, __):
        change = _make_valid_change()
        del change["subsection"]
        ok, errors = validate_proposal([change], "2026-2027")
        assert ok is False

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_missing_required_field_week(self, _, __):
        change = _make_valid_change()
        del change["week"]
        ok, errors = validate_proposal([change], "2026-2027")
        assert ok is False

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_missing_required_field_value(self, _, __):
        change = _make_valid_change()
        del change["value"]
        ok, errors = validate_proposal([change], "2026-2027")
        assert ok is False

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_missing_required_field_reason(self, _, __):
        change = _make_valid_change()
        del change["reason"]
        ok, errors = validate_proposal([change], "2026-2027")
        assert ok is False

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_missing_status_field_rejected(self, _, __):
        change = _make_valid_change()
        del change["status"]
        ok, errors = validate_proposal([change], "2026-2027")
        assert ok is False

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_valid_status_values_accepted(self, _, __):
        for status in ("active", "planned", "blocked", "completed", ""):
            changes = [_make_valid_change(status=status)]
            ok, errors = validate_proposal(changes, "2026-2027")
            assert ok is True, f"Status {status!r} should be valid, got errors: {errors}"

    @patch(PATCH_LOAD_META, return_value=MOCK_METADATA)
    @patch(PATCH_LOAD_SCHEMA, return_value=MOCK_SCHEMA_ROWS)
    def test_invalid_status_rejected(self, _, __):
        changes = [_make_valid_change(status="pending_review")]
        ok, errors = validate_proposal(changes, "2026-2027")
        assert ok is False
        assert any("status" in e.lower() for e in errors)
