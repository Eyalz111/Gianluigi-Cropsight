"""
Gantt Schema Parser — Reads the live Gantt sheet and populates gantt_schema.

Reads column A-D structure, Config tab settings, Meeting Cadence tab,
and status colors. Stores everything in the gantt_schema Supabase table.

Run once initially, then re-run whenever the Gantt structure changes.

Usage:
    python scripts/parse_gantt_schema.py

Also callable as a function:
    from scripts.parse_gantt_schema import parse_gantt_schema
    await parse_gantt_schema()
"""

import json
import logging
import re
import sys
import os

# Add project root to path when run as script
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from services.google_sheets import sheets_service
from services.supabase_client import supabase_client
from services.gantt_weeks import column_to_index, index_to_column

logger = logging.getLogger(__name__)

# Subsection type classification
_TYPE_KEYWORDS = {
    "execution": "execution",
    "planning": "planning",
    "meeting": "meeting",
    "milestone": "milestone",
    "availability": "availability",
    "okr": "okr",
    "objective": "okr",
    "key result": "okr",
    "strategy": "strategy",
    "strategic": "strategy",
}


def _classify_subsection(name: str) -> str:
    """Classify a subsection name into a type."""
    name_lower = name.lower()
    for keyword, stype in _TYPE_KEYWORDS.items():
        if keyword in name_lower:
            return stype
    return "execution"


def _is_section_header(row_values: list) -> bool:
    """Check if a row is a section header (ALL CAPS text in column A)."""
    if not row_values or not row_values[0]:
        return False
    text = str(row_values[0]).strip()
    # Section headers are ALL CAPS and at least 3 chars
    return len(text) >= 3 and text == text.upper() and text.replace("&", "").replace(" ", "").isalpha()


def _parse_config_tab(spreadsheet_id: str) -> dict:
    """
    Read the Config tab for start dates, week offsets, owner prefixes, etc.

    Returns:
        Dict with config values: week_offset, start_date, valid_owners,
        protected_rows, etc.
    """
    config = {
        "week_offset": 9,
        "first_week_col": "E",
        "valid_owners": ["[E]", "[R]", "[P]", "[Y]", "[E/R]", "[ALL]", "[TBD]"],
        "protected_row_names": [],
    }

    try:
        config_tab = settings.GANTT_CONFIG_TAB
        result = sheets_service.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{config_tab}'!A:B",
        ).execute()
        rows = result.get("values", [])

        for row in rows:
            if len(row) < 2:
                continue
            key = str(row[0]).strip().lower()
            value = str(row[1]).strip()

            if "week offset" in key or "start week" in key:
                try:
                    config["week_offset"] = int(value)
                except ValueError:
                    pass
            elif "first week col" in key:
                config["first_week_col"] = value.upper()
            elif "owner" in key:
                # Parse owner prefixes — may be comma-separated or a regex pattern
                raw_owners = [o.strip() for o in value.split(",") if o.strip()]
                # Check if the value is a regex pattern (e.g., \[([A-Z/+]+)\])
                # If so, keep the defaults; otherwise use parsed values
                parsed_owners = []
                for o in raw_owners:
                    if o.startswith("\\") or "(" in o or ")" in o:
                        # Regex pattern — skip, keep defaults
                        logger.info(f"Config tab has regex owner pattern: {o}. Using defaults.")
                        break
                    parsed_owners.append(o)
                else:
                    # All entries were valid non-regex owner prefixes
                    if parsed_owners:
                        config["valid_owners"] = parsed_owners
            elif "protected" in key:
                config["protected_row_names"] = [
                    n.strip() for n in value.split(",") if n.strip()
                ]

    except Exception as e:
        logger.warning(f"Could not read Config tab: {e}. Using defaults.")

    return config


def _parse_header_row(spreadsheet_id: str, sheet_name: str) -> int:
    """
    Read row 5 (header) to determine the max week column.

    Returns:
        Max week number found in the header.
    """
    try:
        result = sheets_service.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!5:5",
        ).execute()
        row = result.get("values", [[]])[0]

        max_week = 0
        for i, cell in enumerate(row):
            cell_str = str(cell).strip()
            match = re.match(r'^W(\d+)$', cell_str)
            if match:
                week_num = int(match.group(1))
                if week_num > max_week:
                    max_week = week_num

        return max_week if max_week > 0 else 104  # default

    except Exception as e:
        logger.warning(f"Could not read header row: {e}")
        return 104


def _read_status_colors(spreadsheet_id: str, sheet_name: str) -> dict:
    """
    Read actual background colors from the Gantt sheet.

    Reads two ranges:
    1. Column A (rows 1-60) for section header colors
    2. Data cells (E6:Z60) for status colors (active/planned/blocked/completed)

    Returns:
        Dict mapping status names to hex color strings, plus section_colors dict.
    """
    colors = {}
    try:
        # Read both section headers (col A) and data cells (wide range)
        result = sheets_service.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            ranges=[
                f"'{sheet_name}'!A1:D60",   # Section headers & structure
                f"'{sheet_name}'!E6:AZ60",  # Data cells (wide to catch all statuses)
            ],
            includeGridData=True,
        ).execute()

        sheets_data = result.get("sheets", [])
        if not sheets_data:
            return colors

        all_grid = sheets_data[0].get("data", [])

        # --- Pass 1: Section header colors from column A ---
        section_colors = {}
        if len(all_grid) >= 1:
            for row_idx, row_data in enumerate(all_grid[0].get("rowData", [])):
                cells = row_data.get("values", [])
                if not cells:
                    continue
                cell = cells[0]  # Column A
                bg = cell.get("effectiveFormat", {}).get("backgroundColor", {})
                value = cell.get("formattedValue", "")
                if value and bg and _is_section_header([value]):
                    r = int(bg.get("red", 0) * 255)
                    g = int(bg.get("green", 0) * 255)
                    b = int(bg.get("blue", 0) * 255)
                    hex_color = f"#{r:02X}{g:02X}{b:02X}"
                    if hex_color != "#FFFFFF":
                        section_colors[value.strip()] = hex_color

        if section_colors:
            colors["_section_colors"] = section_colors
            logger.info(f"Section colors: {section_colors}")

        # --- Pass 2: Status colors from data cells ---
        seen_colors = {}  # hex -> count
        if len(all_grid) >= 2:
            for row_data in all_grid[1].get("rowData", []):
                for cell in row_data.get("values", []):
                    bg = cell.get("effectiveFormat", {}).get("backgroundColor", {})
                    value = cell.get("formattedValue", "")
                    if bg:
                        r = int(bg.get("red", 0) * 255)
                        g = int(bg.get("green", 0) * 255)
                        b = int(bg.get("blue", 0) * 255)
                        hex_color = f"#{r:02X}{g:02X}{b:02X}"
                        # Skip white, black, and known section header colors
                        if hex_color in ("#FFFFFF", "#000000"):
                            continue
                        if hex_color in section_colors.values():
                            continue
                        seen_colors[hex_color] = seen_colors.get(hex_color, 0) + 1

        if seen_colors:
            colors["_raw_colors"] = seen_colors

        # Auto-map data cell colors to statuses using HSL analysis
        sorted_colors = sorted(seen_colors.items(), key=lambda x: -x[1])
        for hex_color, count in sorted_colors:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            brightness = (r + g + b) / 3
            # Skip near-white (background) and very dark (headers)
            if brightness > 235 or brightness < 60:
                continue
            max_c = max(r, g, b)
            min_c = min(r, g, b)
            saturation = (max_c - min_c) / max(max_c, 1)
            if saturation < 0.08:
                if "completed" not in colors:
                    colors["completed"] = hex_color
            elif g >= r and g >= b and saturation > 0.1:
                if "active" not in colors:
                    colors["active"] = hex_color
            elif b >= r and b >= g and saturation > 0.1:
                if "planned" not in colors:
                    colors["planned"] = hex_color
            elif r >= g and r >= b and saturation > 0.15:
                if "blocked" not in colors:
                    colors["blocked"] = hex_color

    except Exception as e:
        logger.warning(f"Could not read status colors: {e}")

    return colors


def _parse_meeting_cadence(spreadsheet_id: str) -> list[dict]:
    """
    Parse the Meeting Cadence tab.

    Returns:
        List of meeting definitions with name, frequency, attendees.
    """
    meetings = []
    try:
        tab_name = settings.GANTT_MEETING_CADENCE_TAB
        result = sheets_service.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab_name}'!A:E",
        ).execute()
        rows = result.get("values", [])

        if len(rows) < 2:
            return meetings

        # First row is header
        headers = [str(h).strip().lower() for h in rows[0]]

        for row in rows[1:]:
            if not row or not row[0]:
                continue
            meeting = {"name": str(row[0]).strip()}
            for j, val in enumerate(row[1:], 1):
                if j < len(headers):
                    meeting[headers[j]] = str(val).strip()
            meetings.append(meeting)

    except Exception as e:
        logger.warning(f"Could not read Meeting Cadence tab: {e}")

    return meetings


async def parse_gantt_schema() -> dict:
    """
    Parse the live Gantt spreadsheet and populate the gantt_schema table.

    Algorithm:
    1. Read Config tab for settings and owner prefixes
    2. Read main sheet columns A-D to detect sections and subsections
    3. Read header row to verify week alignment and get max week
    4. Read status colors from reference cells
    5. Parse Meeting Cadence tab
    6. Clear old schema → upsert new rows to Supabase

    Returns:
        Dict with parsing results: rows_created, sections_found, etc.
    """
    spreadsheet_id = settings.GANTT_SHEET_ID
    if not spreadsheet_id:
        raise ValueError("GANTT_SHEET_ID not configured in settings")

    sheet_name = settings.GANTT_MAIN_TAB
    header_rows = settings.GANTT_HEADER_ROWS

    # Step 1: Read Config tab
    config = _parse_config_tab(spreadsheet_id)
    logger.info(f"Config: week_offset={config['week_offset']}, owners={config['valid_owners']}")

    # Step 2: Read main sheet structure (columns A-D)
    result = sheets_service.service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A:D",
    ).execute()
    all_rows = result.get("values", [])

    schema_rows = []
    current_section = None
    sections_found = []

    # First pass: scan OPERATIONAL RULES to find formula/conditional-formatting rows
    formula_rows = set()         # Rows with formulas — must be protected
    cond_format_rows = set()     # Rows with conditional formatting — skip color writes
    cadence_driven_rows = set()  # Meeting rows driven by cadence tab
    in_rules_section = False
    for row_idx, row_values in enumerate(all_rows):
        if not row_values:
            continue
        text = str(row_values[0]).strip()
        if text == "OPERATIONAL RULES":
            in_rules_section = True
            continue
        if in_rules_section and _is_section_header(row_values) and text != "OPERATIONAL RULES":
            break  # past the rules section
        if in_rules_section:
            # Join all columns to get the full text
            full_text = " ".join(str(v).strip() for v in row_values if v)
            full_lower = full_text.lower()
            # Extract row numbers from tech notes
            row_nums_in_text = [int(n) for n in re.findall(r'\b(\d{2,3})\b', full_text)]
            if "formula" in full_lower and row_nums_in_text:
                formula_rows.update(row_nums_in_text)
                cond_format_rows.update(row_nums_in_text)
            elif "conditional formatting" in full_lower and row_nums_in_text:
                cond_format_rows.update(row_nums_in_text)
            elif "cadence" in full_lower:
                cadence_driven_rows.update(row_nums_in_text)

    if formula_rows:
        logger.info(f"Formula rows (protected): {sorted(formula_rows)}")
    if cond_format_rows:
        logger.info(f"Conditional formatting rows (skip color writes): {sorted(cond_format_rows)}")

    # Second pass: build schema rows
    for row_idx, row_values in enumerate(all_rows):
        row_number = row_idx + 1  # 1-based

        # Skip header rows
        if row_number <= header_rows:
            continue

        if not row_values or all(not v for v in row_values):
            continue

        # Check for section header
        if _is_section_header(row_values):
            current_section = str(row_values[0]).strip()
            sections_found.append(current_section)
            # Section header row is protected
            schema_rows.append({
                "sheet_name": sheet_name,
                "section": current_section,
                "subsection": None,
                "row_number": row_number,
                "owner_column": config.get("owner_column", "C"),
                "due_column": config.get("due_column", "D"),
                "first_week_column": config["first_week_col"],
                "week_offset": config["week_offset"],
                "protected": True,
                "notes": "section_header",
            })
            continue

        if not current_section:
            continue

        # Subsection row — text in column A or B
        subsection_name = None
        for col_val in row_values:
            val = str(col_val).strip() if col_val else ""
            if val:
                subsection_name = val
                break

        if not subsection_name:
            continue

        # Check if this row should be protected
        is_protected_row = (
            row_number in formula_rows  # has formulas — never write
            or row_number in (13, 15, 16)  # known formula rows (fallback)
            or subsection_name in config.get("protected_row_names", [])
        )

        # Classify subsection type
        sub_type = _classify_subsection(subsection_name)

        # Build notes with metadata about row behavior
        notes_data = sub_type
        if row_number in cond_format_rows and row_number not in formula_rows:
            notes_data = f"{sub_type}|cond_format"
        if row_number in cadence_driven_rows:
            notes_data = f"{sub_type}|cadence_driven"

        schema_rows.append({
            "sheet_name": sheet_name,
            "section": current_section,
            "subsection": subsection_name,
            "row_number": row_number,
            "owner_column": "C",
            "due_column": "D",
            "first_week_column": config["first_week_col"],
            "week_offset": config["week_offset"],
            "protected": is_protected_row,
            "notes": notes_data,
        })

    # Step 3: Read header row for max week
    max_week = _parse_header_row(spreadsheet_id, sheet_name)
    logger.info(f"Max week: W{max_week}")

    # Step 4: Read status colors
    status_colors = _read_status_colors(spreadsheet_id, sheet_name)
    logger.info(f"Status colors found: {len(status_colors)} entries")

    # Step 5: Parse Meeting Cadence tab
    meeting_cadence = _parse_meeting_cadence(spreadsheet_id)
    logger.info(f"Meeting cadence: {len(meeting_cadence)} meetings")

    # Add meeting cadence rows to schema
    for meeting in meeting_cadence:
        schema_rows.append({
            "sheet_name": settings.GANTT_MEETING_CADENCE_TAB,
            "section": "Meeting Cadence",
            "subsection": meeting.get("name", "Unknown"),
            "row_number": 0,
            "protected": True,
            "notes": json.dumps(meeting),
        })

    # Add metadata row with owners, max_week, colors
    metadata = {
        "valid_owners": config["valid_owners"],
        "max_week": max_week,
        "gantt_colors": status_colors,
        "week_offset": config["week_offset"],
        "first_week_col": config["first_week_col"],
    }
    schema_rows.append({
        "sheet_name": "_metadata",
        "section": "_config",
        "subsection": "_metadata",
        "row_number": 0,
        "protected": True,
        "notes": json.dumps(metadata),
    })

    # Step 6: Clear old schema and insert new rows
    try:
        supabase_client.client.table("gantt_schema").delete().eq(
            "workspace_id", "cropsight"
        ).execute()
    except Exception as e:
        logger.warning(f"Could not clear old schema: {e}")

    inserted = 0
    for row in schema_rows:
        try:
            supabase_client.client.table("gantt_schema").insert(row).execute()
            inserted += 1
        except Exception as e:
            logger.error(f"Failed to insert schema row: {e}")

    result = {
        "rows_created": inserted,
        "sections_found": sections_found,
        "max_week": max_week,
        "meeting_cadence_count": len(meeting_cadence),
        "status_colors": status_colors,
    }
    logger.info(f"Schema parse complete: {result}")
    return result


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    print("Parsing Gantt schema...")
    result = asyncio.run(parse_gantt_schema())
    print(f"Done: {result}")
