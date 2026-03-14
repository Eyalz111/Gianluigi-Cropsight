"""
Gantt Guard — Write protection and validation for Gantt proposals.

Validates proposed changes before they reach the Google Sheets API.
Two layers:
1. Structural validation (protected rows, max cells, section existence)
2. Cell format validation (owner prefix, content pattern by row type)

Usage:
    from guardrails.gantt_guard import validate_proposal, expand_range_changes
"""

import logging
import re
from typing import Any

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Status values allowed in proposals
VALID_STATUSES = {"active", "planned", "blocked", "completed", ""}

# Subsection type classification keywords
_SUBSECTION_TYPE_MAP = {
    "execution": "execution",
    "planning": "planning",
    "meeting": "meeting",
    "milestone": "milestone",
    "availability": "availability",
    "okr": "okr",
    "strategy": "strategy",
}


def _classify_subsection_type(subsection_name: str) -> str:
    """Classify a subsection name into a type based on keywords."""
    name_lower = subsection_name.lower()
    for keyword, stype in _SUBSECTION_TYPE_MAP.items():
        if keyword in name_lower:
            return stype
    return "execution"  # default


def _load_schema() -> list[dict]:
    """Load the gantt_schema table from Supabase."""
    try:
        result = supabase_client.client.table("gantt_schema").select("*").execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Failed to load gantt_schema: {e}")
        return []


def _load_schema_metadata() -> dict:
    """Load schema metadata (owners, colors, max_week) from gantt_schema notes."""
    schema_rows = _load_schema()
    metadata = {}
    for row in schema_rows:
        notes = row.get("notes")
        if notes and notes.startswith("{"):
            try:
                import json
                meta = json.loads(notes)
                metadata.update(meta)
            except (json.JSONDecodeError, TypeError):
                pass
    return metadata


def resolve_row_number(
    sheet_name: str, section: str, subsection: str
) -> tuple[int | None, str | None, str | None]:
    """
    Resolve a section/subsection to a row number using the gantt_schema table.

    Uses case-insensitive partial matching for free-text resilience.
    e.g., "Product & Tech" matches "PRODUCT & TECHNOLOGY".

    Args:
        sheet_name: Tab name (e.g., "2026-2027").
        section: Section name (e.g., "Product & Technology").
        subsection: Subsection name (e.g., "Execution").

    Returns:
        Tuple of (row_number, matched_section, matched_subsection).
        row_number is None if no match found.
    """
    schema_rows = _load_schema()
    if not schema_rows:
        return None, None, None

    section_lower = section.lower()
    subsection_lower = subsection.lower()

    # First pass: exact match (case-insensitive)
    for row in schema_rows:
        if row.get("sheet_name", "").lower() != sheet_name.lower():
            continue
        row_section = (row.get("section") or "").lower()
        row_subsection = (row.get("subsection") or "").lower()
        if row_section == section_lower and row_subsection == subsection_lower:
            return row["row_number"], row["section"], row["subsection"]

    # Second pass: partial match
    for row in schema_rows:
        if row.get("sheet_name", "").lower() != sheet_name.lower():
            continue
        row_section = (row.get("section") or "").lower()
        row_subsection = (row.get("subsection") or "").lower()
        section_match = (
            section_lower in row_section or row_section in section_lower
        )
        subsection_match = (
            subsection_lower in row_subsection or row_subsection in subsection_lower
        )
        if section_match and subsection_match:
            return row["row_number"], row["section"], row["subsection"]

    return None, None, None


def is_protected(sheet_name: str, row_number: int) -> bool:
    """Check if a row is protected (formula rows, section headers)."""
    schema_rows = _load_schema()
    for row in schema_rows:
        if (
            row.get("sheet_name", "").lower() == sheet_name.lower()
            and row.get("row_number") == row_number
            and row.get("protected")
        ):
            return True
    return False


def validate_cell_format(
    value: str,
    subsection_type: str,
    valid_owners: list[str],
) -> tuple[bool, str | None]:
    """
    Validate a cell value matches the expected format for its row type.

    Args:
        value: The cell value to validate.
        subsection_type: Type of the subsection (execution, meeting, etc.).
        valid_owners: List of valid owner prefixes (e.g., ["[E]", "[R]"]).

    Returns:
        Tuple of (is_valid, error_message_or_None).
    """
    # Empty values are always valid (clearing a cell)
    if not value or not value.strip():
        return True, None

    value = value.strip()

    if subsection_type in ("execution", "planning", "okr", "strategy", "availability"):
        # Must start with an owner prefix
        owner_pattern = re.match(r'^\[([A-Za-z/]+)\]', value)
        if not owner_pattern:
            return False, (
                f"Missing owner prefix. Add a prefix like {', '.join(valid_owners[:4])}. "
                f"Example: '[R] {value}'"
            )
        prefix = f"[{owner_pattern.group(1).upper()}]"
        if valid_owners and prefix not in [o.upper() for o in valid_owners]:
            return False, (
                f"Invalid owner prefix '{prefix}'. "
                f"Valid prefixes: {', '.join(valid_owners)}"
            )
        return True, None

    elif subsection_type == "meeting":
        # Must match meeting pattern
        if not (
            value.lower().startswith("per cadence")
            or value.lower().startswith("no meeting")
            or value == ""
        ):
            return False, (
                "Meeting rows expect format: 'Per cadence (N)' or "
                "'Per cadence (N) — CANCEL: Meeting Name (reason)'. "
                f"Got: '{value}'"
            )
        return True, None

    elif subsection_type == "milestone":
        # Must start with a milestone marker
        if not any(value.startswith(m) for m in ("★", "●", "◆")):
            return False, (
                "Milestone rows expect format: '★ Title' (tech), "
                "'● Title' (commercial), or '◆ Title' (funding). "
                f"Got: '{value}'"
            )
        return True, None

    return True, None


def expand_range_changes(changes: list[dict]) -> list[dict]:
    """
    Expand range operations (week_start/week_end) into individual cell changes.

    Single-week changes pass through unchanged. Multi-week ranges expand
    into one change per week.

    Args:
        changes: List of change dicts, each with either 'week' or
                 'week_start'/'week_end'.

    Returns:
        List of single-week change dicts.
    """
    expanded = []
    for change in changes:
        if "week_start" in change and "week_end" in change:
            week_start = change["week_start"]
            week_end = change["week_end"]
            for w in range(week_start, week_end + 1):
                single = {k: v for k, v in change.items()
                          if k not in ("week_start", "week_end")}
                single["week"] = w
                expanded.append(single)
        else:
            expanded.append(change)
    return expanded


def validate_proposal(
    changes: list[dict],
    sheet_name: str | None = None,
) -> tuple[bool, list[str]]:
    """
    Validate a Gantt update proposal.

    Checks:
    - Required fields present
    - Target section/subsection exists in schema
    - Target row is not protected
    - Max cells limit not exceeded
    - Week in valid range
    - Cell format matches row type
    - Status field is valid

    Args:
        changes: List of proposed change dicts.
        sheet_name: Override sheet name (defaults to settings.GANTT_MAIN_TAB).

    Returns:
        Tuple of (is_valid, list_of_error_strings).
    """
    from config.settings import settings

    errors = []

    if not sheet_name:
        sheet_name = settings.GANTT_MAIN_TAB

    # Load schema
    schema_rows = _load_schema()
    if not schema_rows:
        errors.append(
            "No Gantt schema found. Run the schema parser first "
            "(python scripts/parse_gantt_schema.py)."
        )
        return False, errors

    # Expand ranges first
    expanded = expand_range_changes(changes)

    # Check max cells
    max_cells = settings.GANTT_MAX_CELLS_PER_PROPOSAL
    if len(expanded) > max_cells:
        errors.append(
            f"Too many cell changes ({len(expanded)}). "
            f"Maximum is {max_cells} per proposal."
        )
        return False, errors

    # Load metadata for owners and max_week
    metadata = _load_schema_metadata()
    valid_owners = metadata.get("valid_owners", [])
    max_week = metadata.get("max_week", 104)

    for i, change in enumerate(expanded):
        prefix = f"Change #{i + 1}"

        # Required fields
        if "section" not in change:
            errors.append(f"{prefix}: missing 'section' field")
            continue
        if "subsection" not in change:
            errors.append(f"{prefix}: missing 'subsection' field")
            continue
        if "week" not in change:
            errors.append(f"{prefix}: missing 'week' field")
            continue
        if "value" not in change:
            errors.append(f"{prefix}: missing 'value' field")
            continue
        if "reason" not in change:
            errors.append(f"{prefix}: missing 'reason' field")
            continue
        if "status" not in change:
            errors.append(f"{prefix}: missing 'status' field")
            continue

        # Status validation
        status = change.get("status", "")
        if status and status not in VALID_STATUSES:
            errors.append(
                f"{prefix}: invalid status '{status}'. "
                f"Valid: {', '.join(sorted(VALID_STATUSES - {''}))}"
            )

        # Week range validation
        week = change["week"]
        if not isinstance(week, int) or week < 1:
            errors.append(f"{prefix}: invalid week number '{week}'")
            continue
        if week > max_week:
            errors.append(
                f"{prefix}: week W{week} is out of range (max W{max_week})"
            )

        # Resolve section/subsection
        row_num, matched_section, matched_subsection = resolve_row_number(
            sheet_name, change["section"], change["subsection"]
        )
        if row_num is None:
            errors.append(
                f"{prefix}: section '{change['section']}' / "
                f"subsection '{change['subsection']}' not found in schema"
            )
            continue

        # Protected row check
        if is_protected(sheet_name, row_num):
            errors.append(
                f"{prefix}: row {row_num} ({matched_section} > {matched_subsection}) "
                f"is protected and cannot be modified"
            )
            continue

        # Cell format validation
        value = change.get("value", "")
        subsection_type = _classify_subsection_type(matched_subsection or "")

        # Check schema for stored subsection_type
        for schema_row in schema_rows:
            if (
                schema_row.get("row_number") == row_num
                and schema_row.get("sheet_name", "").lower() == sheet_name.lower()
            ):
                stored_type = schema_row.get("notes", "")
                if stored_type and not stored_type.startswith("{"):
                    # Strip pipe-separated flags (e.g., "planning|cond_format" → "planning")
                    subsection_type = stored_type.split("|")[0]
                break

        valid, format_error = validate_cell_format(value, subsection_type, valid_owners)
        if not valid:
            logger.warning(
                f"Cell format rejected: value='{value}', "
                f"subsection_type='{subsection_type}', error='{format_error}'"
            )
            errors.append(f"{prefix}: {format_error}")

    return len(errors) == 0, errors
