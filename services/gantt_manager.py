"""
Gantt Manager — Core service for reading/writing the operational Gantt chart.

Provides structured read operations (parsed cell data with owner/status/type),
safe write operations (propose → approve → execute with snapshots), and
rollback capability.

Usage:
    from services.gantt_manager import gantt_manager

    # Read
    status = await gantt_manager.get_gantt_status(week=11)

    # Write (propose → approve → execute)
    proposal = await gantt_manager.propose_gantt_update(changes, source="telegram")
    await gantt_manager.execute_approved_proposal(proposal["id"])
"""

import json
import logging
import re
from datetime import date, datetime
from typing import Any

from config.settings import settings
from services.supabase_client import supabase_client
from services.google_sheets import sheets_service
from services.gantt_weeks import (
    week_to_column,
    column_to_week,
    current_week_number,
    column_to_index,
    index_to_column,
)
from guardrails.gantt_guard import (
    validate_proposal,
    expand_range_changes,
    resolve_row_number,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Cell Parser
# =============================================================================

def _hex_to_sheets_color(hex_str: str) -> dict:
    """Convert '#RRGGBB' to Sheets API color dict (0.0-1.0 scale)."""
    return {
        "red": int(hex_str[1:3], 16) / 255,
        "green": int(hex_str[3:5], 16) / 255,
        "blue": int(hex_str[5:7], 16) / 255,
    }


def _sheets_color_to_hex(color: dict) -> str:
    """Convert Sheets API color dict to '#RRGGBB' hex string."""
    r = int(color.get("red", 0) * 255)
    g = int(color.get("green", 0) * 255)
    b = int(color.get("blue", 0) * 255)
    return f"#{r:02X}{g:02X}{b:02X}"


def _color_to_status(hex_color: str, color_map: dict) -> str:
    """
    Map a background color to a status string using the schema color map.

    Falls back to heuristic matching if no exact match in color_map.
    """
    # Try exact match from schema
    for status, mapped_color in color_map.items():
        if status.startswith("_"):
            continue
        if mapped_color.upper() == hex_color.upper():
            return status

    # Heuristic fallback using HSL analysis (handles muted palettes)
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    brightness = (r + g + b) / 3
    max_c = max(r, g, b)
    min_c = min(r, g, b)
    saturation = (max_c - min_c) / max(max_c, 1)

    # Near-white or very light = completed/empty
    if brightness > 235:
        return "completed"
    # Very dark = header/section (not a status)
    if brightness < 60:
        return "unknown"
    # Low saturation gray = completed
    if saturation < 0.08:
        return "completed"
    # Green-dominant = active
    if g >= r and g >= b and saturation > 0.1:
        return "active"
    # Blue-dominant = planned
    if b >= r and b >= g and saturation > 0.1:
        return "planned"
    # Red-dominant = blocked
    if r >= g and r >= b and saturation > 0.15:
        return "blocked"

    return "unknown"


def _parse_cell(
    raw_value: str,
    background_color: dict | None,
    section: str,
    subsection: str,
    week: int,
    color_map: dict | None = None,
) -> dict | None:
    """
    Parse a raw cell value into a structured dict.

    Args:
        raw_value: The cell text.
        background_color: Sheets API color dict or None.
        section: Section name.
        subsection: Subsection name.
        week: Week number.
        color_map: Status→hex color mapping from schema.

    Returns:
        Parsed dict or None for empty cells.
    """
    if not raw_value or not raw_value.strip():
        return None

    raw_value = raw_value.strip()
    result = {
        "section": section,
        "subsection": subsection,
        "owner": None,
        "text": raw_value,
        "status": "unknown",
        "raw_value": raw_value,
        "week": week,
        "type": "work_item",
    }

    # Determine status from background color
    if background_color and color_map:
        hex_color = _sheets_color_to_hex(background_color)
        result["status"] = _color_to_status(hex_color, color_map)

    # Parse owner prefix: [R], [E/R], [ALL], etc.
    owner_match = re.match(r'^\[([A-Za-z/]+)\]\s*(.*)', raw_value, re.DOTALL)
    if owner_match:
        result["owner"] = owner_match.group(1).upper()
        result["text"] = owner_match.group(2).strip()
        result["type"] = "work_item"
        return result

    # Parse meeting pattern: "Per cadence (N)" or with cancellations
    cadence_match = re.match(
        r'^Per cadence\s*\((\d+)\)(.*)', raw_value, re.IGNORECASE | re.DOTALL
    )
    if cadence_match:
        result["type"] = "meeting"
        result["count"] = int(cadence_match.group(1))
        remainder = cadence_match.group(2).strip()

        # Parse cancellations: "— CANCEL: Name (reason)"
        cancellations = []
        cancel_pattern = re.finditer(
            r'CANCEL:\s*([^(]+)\(([^)]+)\)', remainder
        )
        for m in cancel_pattern:
            cancellations.append({
                "name": m.group(1).strip(),
                "reason": m.group(2).strip(),
            })
        if cancellations:
            result["cancellations"] = cancellations
        return result

    # Parse milestone markers
    if raw_value.startswith("★"):
        result["type"] = "milestone"
        result["marker"] = "star"
        result["text"] = raw_value[1:].strip()
        return result
    if raw_value.startswith("●"):
        result["type"] = "milestone"
        result["marker"] = "bullet"
        result["text"] = raw_value[1:].strip()
        return result
    if raw_value.startswith("◆"):
        result["type"] = "milestone"
        result["marker"] = "diamond"
        result["text"] = raw_value[1:].strip()
        return result

    return result


# =============================================================================
# Schema Helpers
# =============================================================================

def _get_schema_metadata() -> dict:
    """Load cached schema metadata from Supabase."""
    try:
        result = supabase_client.client.table("gantt_schema").select("*").eq(
            "sheet_name", "_metadata"
        ).execute()
        rows = result.data or []
        if rows and rows[0].get("notes"):
            return json.loads(rows[0]["notes"])
    except Exception as e:
        logger.error(f"Failed to load schema metadata: {e}")
    return {}


def _get_color_map() -> dict:
    """Get status→hex color mapping from schema metadata."""
    metadata = _get_schema_metadata()
    colors = metadata.get("gantt_colors", {})
    # Filter out internal keys
    return {k: v for k, v in colors.items() if not k.startswith("_")}


def _get_schema_rows(sheet_name: str | None = None) -> list[dict]:
    """Load schema rows, optionally filtered by sheet name."""
    try:
        query = supabase_client.client.table("gantt_schema").select("*")
        if sheet_name:
            query = query.eq("sheet_name", sheet_name)
        result = query.execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Failed to load schema rows: {e}")
        return []


# =============================================================================
# GanttManager
# =============================================================================

class GanttManager:
    """
    Core Gantt chart service. Singleton pattern.

    Read operations return parsed structured dicts.
    Write operations go through propose → approve → execute flow.
    """

    def __init__(self):
        self._metadata_cache: dict | None = None
        self._cache_time: datetime | None = None

    def _get_metadata(self) -> dict:
        """Get schema metadata with simple caching (5 min TTL)."""
        now = datetime.now()
        if (
            self._metadata_cache is not None
            and self._cache_time is not None
            and (now - self._cache_time).total_seconds() < 300
        ):
            return self._metadata_cache
        self._metadata_cache = _get_schema_metadata()
        self._cache_time = now
        return self._metadata_cache

    # =========================================================================
    # Read Operations
    # =========================================================================

    async def get_gantt_status(self, week: int | None = None) -> dict:
        """
        Get all sections' parsed cells for a given week.

        Args:
            week: Week number (defaults to current week).

        Returns:
            Dict with sections and their parsed cell data.
        """
        metadata = self._get_metadata()
        week_offset = metadata.get("week_offset", 9)
        first_week_col = metadata.get("first_week_col", "E")

        if week is None:
            week = current_week_number(week_offset=week_offset)

        try:
            col_letter = week_to_column(week, week_offset, first_week_col)
        except ValueError as e:
            return {"error": str(e), "week": week}

        spreadsheet_id = settings.GANTT_SHEET_ID
        sheet_name = settings.GANTT_MAIN_TAB
        color_map = _get_color_map()

        # Read the column with grid data for colors
        col_index = column_to_index(col_letter)
        range_str = f"'{sheet_name}'!{col_letter}1:{col_letter}100"

        try:
            result = sheets_service.service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                ranges=[range_str],
                includeGridData=True,
            ).execute()
        except Exception as e:
            return {"error": f"Failed to read Gantt: {e}", "week": week}

        # Also read columns A-B for section/subsection labels
        label_result = sheets_service.service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A1:B100",
        ).execute()
        label_rows = label_result.get("values", [])

        # Parse grid data
        grid_data = (
            result.get("sheets", [{}])[0]
            .get("data", [{}])[0]
            .get("rowData", [])
        )

        schema_rows = _get_schema_rows(sheet_name)
        row_map = {}
        for sr in schema_rows:
            row_map[sr["row_number"]] = sr

        items = []
        for row_idx, row_data in enumerate(grid_data):
            row_number = row_idx + 1
            cells = row_data.get("values", [])
            if not cells:
                continue

            cell = cells[0]
            raw_value = cell.get("formattedValue", "")
            bg_color = cell.get("effectiveFormat", {}).get("backgroundColor")

            if not raw_value:
                continue

            # Look up section/subsection from schema
            schema_entry = row_map.get(row_number, {})
            section = schema_entry.get("section", "Unknown")
            subsection = schema_entry.get("subsection", "")

            if not subsection or schema_entry.get("protected"):
                continue

            parsed = _parse_cell(
                raw_value, bg_color, section, subsection, week, color_map
            )
            if parsed:
                items.append(parsed)

        return {
            "week": week,
            "week_label": f"W{week}",
            "column": col_letter,
            "items": items,
            "count": len(items),
        }

    async def get_gantt_section(
        self, section: str, weeks: list[int] | None = None
    ) -> dict:
        """
        Deep dive into one section across multiple weeks.

        Args:
            section: Section name (fuzzy matched).
            weeks: List of week numbers (defaults to current ± 2).

        Returns:
            Dict with section data across weeks.
        """
        metadata = self._get_metadata()
        week_offset = metadata.get("week_offset", 9)
        first_week_col = metadata.get("first_week_col", "E")

        if weeks is None:
            current = current_week_number(week_offset=week_offset)
            weeks = list(range(current - 2, current + 3))

        schema_rows = _get_schema_rows(settings.GANTT_MAIN_TAB)
        color_map = _get_color_map()

        # Find matching section rows
        section_lower = section.lower()
        matching_rows = [
            sr for sr in schema_rows
            if (
                not sr.get("protected")
                and sr.get("subsection")
                and (
                    section_lower in (sr.get("section") or "").lower()
                    or (sr.get("section") or "").lower() in section_lower
                )
            )
        ]

        if not matching_rows:
            return {"error": f"Section '{section}' not found in schema", "section": section}

        matched_section = matching_rows[0]["section"]
        spreadsheet_id = settings.GANTT_SHEET_ID
        sheet_name = settings.GANTT_MAIN_TAB

        items_by_week = {}
        for week in weeks:
            try:
                col_letter = week_to_column(week, week_offset, first_week_col)
            except ValueError:
                continue

            range_str = f"'{sheet_name}'!{col_letter}1:{col_letter}100"
            try:
                result = sheets_service.service.spreadsheets().get(
                    spreadsheetId=spreadsheet_id,
                    ranges=[range_str],
                    includeGridData=True,
                ).execute()
            except Exception as e:
                logger.error(f"Error reading week {week}: {e}")
                continue

            grid_data = (
                result.get("sheets", [{}])[0]
                .get("data", [{}])[0]
                .get("rowData", [])
            )

            week_items = []
            for sr in matching_rows:
                row_idx = sr["row_number"] - 1
                if row_idx >= len(grid_data):
                    continue
                row_data = grid_data[row_idx]
                cells = row_data.get("values", [])
                if not cells:
                    continue
                cell = cells[0]
                raw_value = cell.get("formattedValue", "")
                bg_color = cell.get("effectiveFormat", {}).get("backgroundColor")

                parsed = _parse_cell(
                    raw_value, bg_color,
                    sr["section"], sr["subsection"],
                    week, color_map,
                )
                if parsed:
                    week_items.append(parsed)

            items_by_week[f"W{week}"] = week_items

        return {
            "section": matched_section,
            "weeks": items_by_week,
            "week_range": [f"W{w}" for w in weeks],
        }

    async def get_meeting_cadence(self, week: int | None = None) -> dict:
        """
        Get expected meetings from cached Meeting Cadence tab data.

        Args:
            week: Week number (for context, defaults to current).

        Returns:
            Dict with meeting cadence definitions.
        """
        metadata = self._get_metadata()
        if week is None:
            week = current_week_number(week_offset=metadata.get("week_offset", 9))

        # Read cached meeting cadence from schema
        cadence_rows = _get_schema_rows(settings.GANTT_MEETING_CADENCE_TAB)
        meetings = []
        for row in cadence_rows:
            notes = row.get("notes", "")
            if notes and notes.startswith("{"):
                try:
                    meeting = json.loads(notes)
                    meetings.append(meeting)
                except json.JSONDecodeError:
                    pass

        return {
            "week": week,
            "week_label": f"W{week}",
            "meetings": meetings,
            "count": len(meetings),
        }

    async def get_gantt_horizon(self, weeks_ahead: int = 8) -> dict:
        """
        Get upcoming milestones and transitions.

        Args:
            weeks_ahead: How many weeks to look ahead.

        Returns:
            Dict with upcoming milestones and status transitions.
        """
        metadata = self._get_metadata()
        week_offset = metadata.get("week_offset", 9)
        current = current_week_number(week_offset=week_offset)
        weeks = list(range(current, current + weeks_ahead + 1))

        schema_rows = _get_schema_rows(settings.GANTT_MAIN_TAB)
        color_map = _get_color_map()
        spreadsheet_id = settings.GANTT_SHEET_ID
        sheet_name = settings.GANTT_MAIN_TAB
        first_week_col = metadata.get("first_week_col", "E")

        milestones = []
        transitions = []

        for week in weeks:
            try:
                col_letter = week_to_column(week, week_offset, first_week_col)
            except ValueError:
                continue

            range_str = f"'{sheet_name}'!{col_letter}1:{col_letter}100"
            try:
                result = sheets_service.service.spreadsheets().get(
                    spreadsheetId=spreadsheet_id,
                    ranges=[range_str],
                    includeGridData=True,
                ).execute()
            except Exception as e:
                continue

            grid_data = (
                result.get("sheets", [{}])[0]
                .get("data", [{}])[0]
                .get("rowData", [])
            )

            row_map = {sr["row_number"]: sr for sr in schema_rows}
            for row_idx, row_data in enumerate(grid_data):
                row_number = row_idx + 1
                sr = row_map.get(row_number)
                if not sr or sr.get("protected") or not sr.get("subsection"):
                    continue

                cells = row_data.get("values", [])
                if not cells:
                    continue
                cell = cells[0]
                raw_value = cell.get("formattedValue", "")
                bg_color = cell.get("effectiveFormat", {}).get("backgroundColor")

                parsed = _parse_cell(
                    raw_value, bg_color,
                    sr["section"], sr["subsection"],
                    week, color_map,
                )
                if parsed and parsed["type"] == "milestone":
                    milestones.append(parsed)

        return {
            "current_week": current,
            "horizon_weeks": weeks_ahead,
            "milestones": milestones,
            "count": len(milestones),
        }

    async def get_gantt_history(self, limit: int = 10) -> dict:
        """
        Get recent approved Gantt changes.

        Reads from gantt_proposals table (approved, ordered by reviewed_at desc).
        Falls back to reading the Log tab if proposals table is empty.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            Dict with recent changes.
        """
        try:
            result = supabase_client.client.table("gantt_proposals").select("*").eq(
                "status", "approved"
            ).order("reviewed_at", desc=True).limit(limit).execute()

            proposals = result.data or []
        except Exception as e:
            logger.error(f"Failed to read proposals: {e}")
            proposals = []

        if proposals:
            history = []
            for p in proposals:
                changes = p.get("changes", [])
                diffs = []
                for c in changes:
                    old_val = c.get("old_value", "")
                    new_val = c.get("new_value", c.get("value", ""))
                    section = c.get("section", "")
                    subsection = c.get("subsection", "")
                    week = c.get("week", "")
                    if old_val:
                        desc = f"{subsection} W{week}: '{old_val}' → '{new_val}'"
                    else:
                        desc = f"{subsection} W{week}: added '{new_val}'"
                    diffs.append(desc)

                history.append({
                    "id": p["id"],
                    "source_type": p.get("source_type"),
                    "proposed_at": p.get("proposed_at"),
                    "reviewed_at": p.get("reviewed_at"),
                    "changes": diffs,
                })
            return {"history": history, "count": len(history), "source": "proposals"}

        # Fallback: read Log tab
        try:
            log_tab = settings.GANTT_LOG_TAB
            log_result = sheets_service.service.spreadsheets().values().get(
                spreadsheetId=settings.GANTT_SHEET_ID,
                range=f"'{log_tab}'!A:F",
            ).execute()
            log_rows = log_result.get("values", [])

            # Skip header, take last N rows
            if len(log_rows) > 1:
                entries = []
                for row in log_rows[-limit:]:
                    entry = {
                        "date": row[0] if len(row) > 0 else "",
                        "week": row[1] if len(row) > 1 else "",
                        "section": row[2] if len(row) > 2 else "",
                        "description": row[3] if len(row) > 3 else "",
                        "by": row[4] if len(row) > 4 else "",
                        "related": row[5] if len(row) > 5 else "",
                    }
                    entries.append(entry)
                return {"history": entries, "count": len(entries), "source": "log_tab"}
        except Exception as e:
            logger.warning(f"Could not read Log tab: {e}")

        return {"history": [], "count": 0, "source": "none"}

    # =========================================================================
    # Write Operations
    # =========================================================================

    async def propose_gantt_update(
        self,
        changes: list[dict],
        source: str = "manual",
        source_id: str | None = None,
    ) -> dict:
        """
        Create a Gantt update proposal.

        Validates changes, reads current values, stores proposal in Supabase.

        Args:
            changes: List of change dicts with section, subsection, week, value,
                     status, reason (and optionally week_start/week_end for ranges).
            source: Source type (meeting, email, telegram, manual).
            source_id: Optional reference to source (meeting_id, etc.).

        Returns:
            Dict with proposal_id, status, validated_changes.
        """
        sheet_name = settings.GANTT_MAIN_TAB

        # Validate
        valid, errors = validate_proposal(changes, sheet_name)
        if not valid:
            return {
                "status": "rejected",
                "errors": errors,
            }

        # Expand ranges
        expanded = expand_range_changes(changes)

        metadata = self._get_metadata()
        week_offset = metadata.get("week_offset", 9)
        first_week_col = metadata.get("first_week_col", "E")
        spreadsheet_id = settings.GANTT_SHEET_ID

        # Read current values for each cell
        enriched_changes = []
        conflicts = []
        for change in expanded:
            row_num, matched_section, matched_subsection = resolve_row_number(
                sheet_name, change["section"], change["subsection"]
            )
            if row_num is None:
                continue

            week = change["week"]
            try:
                col_letter = week_to_column(week, week_offset, first_week_col)
            except ValueError:
                continue

            # Read current cell value
            cell_ref = f"{col_letter}{row_num}"
            old_value = ""
            try:
                current = sheets_service.service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet_name}'!{cell_ref}",
                ).execute()
                values = current.get("values", [[]])
                old_value = values[0][0] if values and values[0] else ""
            except Exception:
                pass

            # Check for conflict: cell has existing content and no force_mode set
            force_mode = change.get("force_mode")
            raw_new_value = change["value"]

            if old_value and old_value.strip() and raw_new_value.strip() and not force_mode:
                # Conflict detected — cell already has content, user must decide
                conflicts.append({
                    "section": matched_section,
                    "subsection": matched_subsection,
                    "week": week,
                    "existing_content": old_value,
                    "proposed_content": raw_new_value,
                })
                continue

            # Determine final new_value based on force_mode
            if force_mode == "append" and old_value and old_value.strip():
                final_value = f"{old_value}\n{raw_new_value}"
            else:
                # replace, or cell is empty
                final_value = raw_new_value

            enriched_changes.append({
                "section": matched_section,
                "subsection": matched_subsection,
                "week": week,
                "column": col_letter,
                "row": row_num,
                "old_value": old_value,
                "new_value": final_value,
                "status": change.get("status", ""),
                "reason": change.get("reason", ""),
            })

        # If there are conflicts, return them for user confirmation
        if conflicts:
            return {
                "status": "needs_confirmation",
                "conflicts": conflicts,
                "message": (
                    "Some cells already have content. "
                    "Please confirm whether to ADD alongside existing content "
                    "or REPLACE it."
                ),
            }

        # Store proposal in Supabase
        proposal_data = {
            "status": "pending",
            "source_type": source,
            "changes": enriched_changes,
        }
        if source_id:
            proposal_data["source_id"] = source_id

        try:
            result = supabase_client.client.table("gantt_proposals").insert(
                proposal_data
            ).execute()
            proposal = result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Failed to store proposal: {e}")
            return {"status": "error", "error": str(e)}

        return {
            "status": "pending",
            "proposal_id": proposal.get("id"),
            "changes": enriched_changes,
            "changes_count": len(enriched_changes),
        }

    async def execute_approved_proposal(self, proposal_id: str) -> dict:
        """
        Execute an approved Gantt proposal: snapshot → write → log.

        Args:
            proposal_id: UUID of the approved proposal.

        Returns:
            Dict with execution result.
        """
        # Load proposal
        try:
            result = supabase_client.client.table("gantt_proposals").select("*").eq(
                "id", proposal_id
            ).execute()
            proposals = result.data or []
        except Exception as e:
            return {"status": "error", "error": f"Failed to load proposal: {e}"}

        if not proposals:
            return {"status": "error", "error": f"Proposal {proposal_id} not found"}

        proposal = proposals[0]
        if proposal["status"] != "pending" and proposal["status"] != "approved":
            return {
                "status": "error",
                "error": f"Proposal is {proposal['status']}, cannot execute",
            }

        changes = proposal.get("changes", [])
        if not changes:
            return {"status": "error", "error": "No changes to execute"}

        spreadsheet_id = settings.GANTT_SHEET_ID
        sheet_name = settings.GANTT_MAIN_TAB
        color_map = _get_color_map()
        metadata = self._get_metadata()

        # Step 1: Save snapshot (including old colors for rollback)
        cell_refs = [f"{c['column']}{c['row']}" for c in changes]
        old_values = {f"{c['column']}{c['row']}": c.get("old_value", "") for c in changes}
        new_values = {f"{c['column']}{c['row']}": c.get("new_value", "") for c in changes}

        # Read old background colors from sheet before overwriting
        old_colors = {}
        try:
            for cell_ref in cell_refs:
                range_str = f"'{sheet_name}'!{cell_ref}"
                resp = sheets_service.service.spreadsheets().get(
                    spreadsheetId=spreadsheet_id,
                    ranges=[range_str],
                    includeGridData=True,
                ).execute()
                for s in resp.get("sheets", []):
                    for grid_data in s.get("data", []):
                        for row_data in grid_data.get("rowData", []):
                            for cell in row_data.get("values", []):
                                fmt = cell.get("effectiveFormat", {})
                                bg = fmt.get("backgroundColor", {})
                                if bg:
                                    old_colors[cell_ref] = {
                                        "red": bg.get("red", 1),
                                        "green": bg.get("green", 1),
                                        "blue": bg.get("blue", 1),
                                    }
        except Exception as e:
            logger.warning(f"Failed to read old colors for snapshot: {e}")

        try:
            supabase_client.client.table("gantt_snapshots").insert({
                "proposal_id": proposal_id,
                "sheet_name": sheet_name,
                "cell_references": cell_refs,
                "old_values": {**old_values, "_old_colors": old_colors},
                "new_values": new_values,
            }).execute()
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")
            return {"status": "error", "error": f"Snapshot failed: {e}"}

        # Step 2: Build atomic batchUpdate (text + color in one call)
        sheet_id = sheets_service._get_sheet_id_by_name(spreadsheet_id, sheet_name)
        if sheet_id is None:
            sheet_id = sheets_service._get_first_sheet_id(spreadsheet_id)

        requests = []
        for change in changes:
            col_idx = column_to_index(change["column"])
            row_idx = change["row"] - 1  # 0-based

            # Text update
            requests.append({
                "updateCells": {
                    "rows": [{
                        "values": [{
                            "userEnteredValue": {
                                "stringValue": change.get("new_value", "")
                            }
                        }]
                    }],
                    "fields": "userEnteredValue",
                    "start": {
                        "sheetId": sheet_id,
                        "rowIndex": row_idx,
                        "columnIndex": col_idx,
                    }
                }
            })

            # Color update — skip if row has conditional formatting (sheet handles colors)
            has_cond_format = False
            row_number = change["row"]
            schema_rows = _get_schema_rows(sheet_name)
            for sr in schema_rows:
                if sr.get("row_number") == row_number:
                    notes = sr.get("notes", "")
                    if "cond_format" in notes:
                        has_cond_format = True
                    break

            status = change.get("status", "")
            if status and status in color_map and not has_cond_format:
                hex_color = color_map[status]
                color_dict = _hex_to_sheets_color(hex_color)
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx,
                            "endRowIndex": row_idx + 1,
                            "startColumnIndex": col_idx,
                            "endColumnIndex": col_idx + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": color_dict,
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                })
            elif has_cond_format and status:
                logger.info(
                    f"Skipping color write for row {row_number} — "
                    f"conditional formatting handles colors"
                )

        # Execute atomic write
        try:
            sheets_service.service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            ).execute()
        except Exception as e:
            logger.error(f"Gantt write failed: {e}")
            return {"status": "error", "error": f"Write failed: {e}"}

        # Step 3: Update proposal status
        try:
            supabase_client.client.table("gantt_proposals").update({
                "status": "approved",
                "reviewed_at": datetime.now().isoformat(),
                "reviewed_by": "eyal",
            }).eq("id", proposal_id).execute()
        except Exception as e:
            logger.warning(f"Failed to update proposal status: {e}")

        # Step 4: Append to Log tab (non-fatal)
        try:
            self._append_to_log(changes, proposal.get("source_type", "manual"))
        except Exception as e:
            logger.warning(f"Failed to append to Log tab: {e}")

        return {
            "status": "executed",
            "proposal_id": proposal_id,
            "cells_written": len(changes),
        }

    def _append_to_log(self, changes: list[dict], source: str) -> None:
        """Append change entries to the Gantt Log tab."""
        log_tab = settings.GANTT_LOG_TAB
        today = date.today().strftime("%Y-%m-%d")
        rows = []

        for change in changes:
            week = change.get("week", "")
            section = change.get("section", "")
            subsection = change.get("subsection", "")
            old_val = change.get("old_value", "")
            new_val = change.get("new_value", "")

            if old_val:
                description = (
                    f"{subsection} W{week}: changed '{old_val}' → '{new_val}'"
                )
            else:
                description = f"{subsection} W{week}: added '{new_val}' (was empty)"

            rows.append([
                today,
                f"W{week}",
                section,
                description,
                "Gianluigi",
                source,
            ])

        if rows:
            sheets_service.service.spreadsheets().values().append(
                spreadsheetId=settings.GANTT_SHEET_ID,
                range=f"'{log_tab}'!A:F",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()

    async def rollback_proposal(self, proposal_id: str | None = None) -> dict:
        """
        Rollback a Gantt proposal by restoring cells from snapshot.

        Args:
            proposal_id: UUID of the proposal to rollback. If None,
                        rolls back the most recently approved proposal.

        Returns:
            Dict with rollback result.
        """
        # Find the proposal
        if proposal_id is None:
            try:
                result = supabase_client.client.table("gantt_proposals").select(
                    "*"
                ).eq("status", "approved").order(
                    "reviewed_at", desc=True
                ).limit(1).execute()
                proposals = result.data or []
            except Exception as e:
                return {"status": "error", "error": f"Failed to find proposals: {e}"}

            if not proposals:
                return {"status": "error", "error": "No approved proposals to rollback"}
            proposal_id = proposals[0]["id"]
        else:
            try:
                result = supabase_client.client.table("gantt_proposals").select(
                    "*"
                ).eq("id", proposal_id).execute()
                proposals = result.data or []
            except Exception as e:
                return {"status": "error", "error": str(e)}

            if not proposals:
                return {"status": "error", "error": f"Proposal {proposal_id} not found"}

        proposal = proposals[0]

        if proposal["status"] == "rolled_back":
            return {
                "status": "error",
                "error": "This proposal has already been rolled back",
            }
        if proposal["status"] not in ("approved",):
            return {
                "status": "error",
                "error": f"Cannot rollback: proposal is {proposal['status']}",
            }

        # Load snapshot
        try:
            snap_result = supabase_client.client.table("gantt_snapshots").select(
                "*"
            ).eq("proposal_id", proposal_id).execute()
            snapshots = snap_result.data or []
        except Exception as e:
            return {"status": "error", "error": f"Failed to load snapshot: {e}"}

        if not snapshots:
            return {"status": "error", "error": "No snapshot found for this proposal"}

        snapshot = snapshots[0]
        sheet_name = snapshot["sheet_name"]
        old_values_raw = snapshot.get("old_values", {})
        # Extract old colors (stored under _old_colors key)
        old_colors = old_values_raw.pop("_old_colors", {}) if isinstance(old_values_raw, dict) else {}
        old_values = old_values_raw
        cell_refs = snapshot.get("cell_references", [])

        spreadsheet_id = settings.GANTT_SHEET_ID

        # Check current values vs snapshot (warn if manually edited)
        warnings = []
        for cell_ref in cell_refs:
            try:
                current = sheets_service.service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet_name}'!{cell_ref}",
                ).execute()
                current_val = ""
                vals = current.get("values", [[]])
                if vals and vals[0]:
                    current_val = vals[0][0]

                expected_new = snapshot.get("new_values", {}).get(cell_ref, "")
                if current_val != expected_new and current_val != "":
                    warnings.append(
                        f"{cell_ref}: current value '{current_val}' differs from "
                        f"expected '{expected_new}' — may have been manually edited"
                    )
            except Exception:
                pass

        # Restore old values
        sheet_id = sheets_service._get_sheet_id_by_name(spreadsheet_id, sheet_name)
        if sheet_id is None:
            sheet_id = sheets_service._get_first_sheet_id(spreadsheet_id)

        requests = []
        for cell_ref in cell_refs:
            old_val = old_values.get(cell_ref, "")
            # Parse cell reference (e.g., "H12" → col_idx, row_idx)
            col_match = re.match(r'^([A-Z]+)(\d+)$', cell_ref)
            if not col_match:
                continue
            col_idx = column_to_index(col_match.group(1))
            row_idx = int(col_match.group(2)) - 1

            requests.append({
                "updateCells": {
                    "rows": [{
                        "values": [{
                            "userEnteredValue": {"stringValue": old_val}
                        }]
                    }],
                    "fields": "userEnteredValue",
                    "start": {
                        "sheetId": sheet_id,
                        "rowIndex": row_idx,
                        "columnIndex": col_idx,
                    }
                }
            })

            # Restore old background color if we have it
            if cell_ref in old_colors:
                color_dict = old_colors[cell_ref]
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx,
                            "endRowIndex": row_idx + 1,
                            "startColumnIndex": col_idx,
                            "endColumnIndex": col_idx + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": color_dict,
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                })

        if requests:
            try:
                sheets_service.service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": requests},
                ).execute()
            except Exception as e:
                return {"status": "error", "error": f"Rollback write failed: {e}"}

        # Update proposal status
        try:
            supabase_client.client.table("gantt_proposals").update({
                "status": "rolled_back",
                "reviewed_at": datetime.now().isoformat(),
            }).eq("id", proposal_id).execute()
        except Exception as e:
            logger.warning(f"Failed to update proposal status: {e}")

        # Log the rollback
        try:
            self._append_to_log(
                [{"week": "", "section": "ROLLBACK", "subsection": "",
                  "old_value": "", "new_value": f"Rolled back proposal {proposal_id[:8]}"}],
                "rollback",
            )
        except Exception:
            pass

        result = {
            "status": "rolled_back",
            "proposal_id": proposal_id,
            "cells_restored": len(cell_refs),
        }
        if warnings:
            result["warnings"] = warnings
        return result

    async def backup_full_gantt(self) -> dict:
        """
        Create a full copy of the Gantt spreadsheet in the Backups folder.

        Uses Google Drive API files().copy() for a complete sheet-level backup.

        Returns:
            Dict with backup file ID and name.
        """
        from services.google_drive import drive_service

        spreadsheet_id = settings.GANTT_SHEET_ID
        backup_folder_id = settings.GANTT_BACKUP_FOLDER_ID

        if not spreadsheet_id:
            return {"status": "error", "error": "GANTT_SHEET_ID not configured"}
        if not backup_folder_id:
            return {"status": "error", "error": "GANTT_BACKUP_FOLDER_ID not configured"}

        backup_name = f"Gantt Backup {date.today().strftime('%Y-%m-%d')}"

        try:
            copied_file = drive_service.service.files().copy(
                fileId=spreadsheet_id,
                body={
                    "name": backup_name,
                    "parents": [backup_folder_id],
                },
            ).execute()

            result = {
                "status": "success",
                "file_id": copied_file.get("id"),
                "name": backup_name,
            }
            logger.info(f"Gantt backup created: {backup_name}")

            # Log the backup
            supabase_client.log_action(
                action="gantt_backup_created",
                details=result,
                triggered_by="auto",
            )

            return result

        except Exception as e:
            logger.error(f"Gantt backup failed: {e}")
            return {"status": "error", "error": str(e)}


# Singleton instance
gantt_manager = GanttManager()
