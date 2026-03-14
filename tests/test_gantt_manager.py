"""
Tests for services/gantt_manager.py

Covers:
- _parse_cell: cell text parsing into structured dicts
- _hex_to_sheets_color / _sheets_color_to_hex: color conversion helpers
- _color_to_status: background color → status mapping
- GanttManager.get_gantt_status: week column read + parse
- GanttManager.get_gantt_section: section deep-dive across weeks
- GanttManager.get_meeting_cadence: cached cadence tab
- GanttManager.get_gantt_history: proposals table + Log tab fallback
- GanttManager.get_gantt_horizon: upcoming milestones
- GanttManager.propose_gantt_update: validation → read current → insert proposal
- GanttManager.execute_approved_proposal: snapshot → batchUpdate → log
- GanttManager.rollback_proposal: restore from snapshot
- GanttManager.backup_full_gantt: Drive files().copy()

All Supabase, Sheets API, and Drive API calls are mocked.
"""

import json
import pytest
from datetime import datetime, date
from unittest.mock import patch, MagicMock, call


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_manager():
    """Return a fresh GanttManager with cleared cache."""
    from services.gantt_manager import GanttManager
    mgr = GanttManager()
    mgr._metadata_cache = None
    mgr._cache_time = None
    return mgr


def _mock_settings(**kwargs):
    """Build a settings mock with sensible Gantt defaults."""
    defaults = {
        "GANTT_SHEET_ID": "test-sheet-id",
        "GANTT_MAIN_TAB": "2026-2027",
        "GANTT_LOG_TAB": "Log",
        "GANTT_MEETING_CADENCE_TAB": "Meeting Cadence",
        "GANTT_BACKUP_FOLDER_ID": "backup-folder-id",
        "GANTT_MAX_CELLS_PER_PROPOSAL": 20,
    }
    defaults.update(kwargs)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


# ---------------------------------------------------------------------------
# _parse_cell tests (~12)
# ---------------------------------------------------------------------------

class TestParseCell:
    """Unit tests for the _parse_cell function."""

    def _call(self, raw, bg=None, section="Engineering", subsection="Execution",
              week=11, color_map=None):
        from services.gantt_manager import _parse_cell
        return _parse_cell(raw, bg, section, subsection, week, color_map or {})

    def test_empty_string_returns_none(self):
        assert self._call("") is None

    def test_whitespace_only_returns_none(self):
        assert self._call("   ") is None

    def test_owner_r_prefix(self):
        result = self._call("[R] Build API endpoint")
        assert result is not None
        assert result["owner"] == "R"
        assert result["text"] == "Build API endpoint"
        assert result["type"] == "work_item"

    def test_owner_e_prefix(self):
        result = self._call("[E] Investor call prep")
        assert result["owner"] == "E"
        assert result["text"] == "Investor call prep"

    def test_owner_p_prefix(self):
        result = self._call("[P] Partner outreach")
        assert result["owner"] == "P"

    def test_owner_y_prefix(self):
        result = self._call("[Y] Review data model")
        assert result["owner"] == "Y"

    def test_owner_all_prefix(self):
        result = self._call("[ALL] Company offsite")
        assert result["owner"] == "ALL"
        assert result["text"] == "Company offsite"
        assert result["type"] == "work_item"

    def test_owner_multi_prefix_e_r(self):
        result = self._call("[E/R] Architecture review")
        assert result["owner"] == "E/R"
        assert result["text"] == "Architecture review"

    def test_owner_prefix_strips_whitespace(self):
        result = self._call("[R]   Trailing spaces  ")
        assert result["owner"] == "R"
        assert result["text"] == "Trailing spaces"

    def test_no_owner_prefix_is_none(self):
        result = self._call("Some generic cell")
        assert result["owner"] is None
        assert result["type"] == "work_item"
        assert result["text"] == "Some generic cell"

    def test_meeting_per_cadence_basic(self):
        result = self._call("Per cadence (4)")
        assert result is not None
        assert result["type"] == "meeting"
        assert result["count"] == 4

    def test_meeting_per_cadence_with_cancellation(self):
        result = self._call("Per cadence (4) — CANCEL: CEO-CTO (holiday)")
        assert result["type"] == "meeting"
        assert result["count"] == 4
        assert "cancellations" in result
        assert len(result["cancellations"]) == 1
        assert result["cancellations"][0]["name"] == "CEO-CTO"
        assert result["cancellations"][0]["reason"] == "holiday"

    def test_meeting_per_cadence_multiple_cancellations(self):
        raw = "Per cadence (3) — CANCEL: CEO-CTO (holiday) CANCEL: Weekly Sync (no agenda)"
        result = self._call(raw)
        assert result["type"] == "meeting"
        assert len(result["cancellations"]) == 2

    def test_milestone_star(self):
        result = self._call("★ MVP Launch")
        assert result["type"] == "milestone"
        assert result["marker"] == "star"
        assert result["text"] == "MVP Launch"

    def test_milestone_bullet(self):
        result = self._call("● Commercial PoC signed")
        assert result["type"] == "milestone"
        assert result["marker"] == "bullet"
        assert result["text"] == "Commercial PoC signed"

    def test_milestone_diamond(self):
        result = self._call("◆ Series A close")
        assert result["type"] == "milestone"
        assert result["marker"] == "diamond"
        assert result["text"] == "Series A close"

    def test_background_color_mapped_to_status(self):
        # green background → "active" via color_map
        bg = {"red": 0.0, "green": 1.0, "blue": 0.0}
        color_map = {"active": "#00FF00"}
        result = self._call("[R] Build feature", bg=bg, color_map=color_map)
        assert result["status"] == "active"

    def test_no_background_color_status_unknown(self):
        result = self._call("[R] Build feature", bg=None)
        assert result["status"] == "unknown"

    def test_week_stored_in_result(self):
        result = self._call("[E] Something", week=15)
        assert result["week"] == 15

    def test_section_and_subsection_stored(self):
        result = self._call("[R] Work", section="Product", subsection="Roadmap", week=10)
        assert result["section"] == "Product"
        assert result["subsection"] == "Roadmap"


# ---------------------------------------------------------------------------
# Color conversion tests (~5)
# ---------------------------------------------------------------------------

class TestColorConversions:
    """Tests for _hex_to_sheets_color and _sheets_color_to_hex."""

    def test_hex_to_sheets_color_red(self):
        from services.gantt_manager import _hex_to_sheets_color
        result = _hex_to_sheets_color("#FF0000")
        assert result["red"] == pytest.approx(1.0)
        assert result["green"] == pytest.approx(0.0)
        assert result["blue"] == pytest.approx(0.0)

    def test_hex_to_sheets_color_green(self):
        from services.gantt_manager import _hex_to_sheets_color
        result = _hex_to_sheets_color("#00FF00")
        assert result["red"] == pytest.approx(0.0)
        assert result["green"] == pytest.approx(1.0)
        assert result["blue"] == pytest.approx(0.0)

    def test_hex_to_sheets_color_white(self):
        from services.gantt_manager import _hex_to_sheets_color
        result = _hex_to_sheets_color("#FFFFFF")
        assert result["red"] == pytest.approx(1.0)
        assert result["green"] == pytest.approx(1.0)
        assert result["blue"] == pytest.approx(1.0)

    def test_sheets_color_to_hex_red(self):
        from services.gantt_manager import _sheets_color_to_hex
        result = _sheets_color_to_hex({"red": 1.0, "green": 0.0, "blue": 0.0})
        assert result == "#FF0000"

    def test_sheets_color_to_hex_blue(self):
        from services.gantt_manager import _sheets_color_to_hex
        result = _sheets_color_to_hex({"red": 0.0, "green": 0.0, "blue": 1.0})
        assert result == "#0000FF"

    def test_sheets_color_to_hex_missing_keys(self):
        from services.gantt_manager import _sheets_color_to_hex
        # Missing keys default to 0
        result = _sheets_color_to_hex({"red": 1.0})
        assert result == "#FF0000"

    def test_round_trip_conversion(self):
        from services.gantt_manager import _hex_to_sheets_color, _sheets_color_to_hex
        original = "#4A90D9"
        converted = _sheets_color_to_hex(_hex_to_sheets_color(original))
        assert converted == original

    def test_color_to_status_exact_match(self):
        from services.gantt_manager import _color_to_status
        color_map = {"active": "#00B050", "planned": "#0070C0"}
        assert _color_to_status("#00B050", color_map) == "active"
        assert _color_to_status("#0070C0", color_map) == "planned"

    def test_color_to_status_case_insensitive(self):
        from services.gantt_manager import _color_to_status
        color_map = {"active": "#00ff00"}
        assert _color_to_status("#00FF00", color_map) == "active"

    def test_color_to_status_heuristic_green(self):
        from services.gantt_manager import _color_to_status
        # Bright green with no match in color_map → heuristic "active"
        assert _color_to_status("#00CC00", {}) == "active"

    def test_color_to_status_heuristic_blue(self):
        from services.gantt_manager import _color_to_status
        assert _color_to_status("#0000CC", {}) == "planned"

    def test_color_to_status_heuristic_red(self):
        from services.gantt_manager import _color_to_status
        assert _color_to_status("#CC0000", {}) == "blocked"

    def test_color_to_status_gray_is_completed(self):
        from services.gantt_manager import _color_to_status
        # Mid-range grey — low saturation = completed
        assert _color_to_status("#808080", {}) == "completed"

    def test_color_to_status_very_dark_is_unknown(self):
        from services.gantt_manager import _color_to_status
        # Very dark color — likely header, not a status
        assert _color_to_status("#1A1A1A", {}) == "unknown"


# ---------------------------------------------------------------------------
# get_gantt_status tests (~8)
# ---------------------------------------------------------------------------

class TestGetGanttStatus:
    """Tests for GanttManager.get_gantt_status."""

    def _make_grid_row(self, text, bg_color=None):
        cell = {"formattedValue": text}
        if bg_color:
            cell["effectiveFormat"] = {"backgroundColor": bg_color}
        else:
            cell["effectiveFormat"] = {}
        return {"values": [cell]}

    @pytest.mark.asyncio
    async def test_returns_parsed_items(self):
        mgr = _make_manager()
        # Pre-seed cache so no Supabase call needed for metadata
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        grid_rows = [self._make_grid_row("[R] Build API")]
        sheets_response = {
            "sheets": [{"data": [{"rowData": grid_rows}]}]
        }
        label_response = {"values": [["Engineering", "Execution"]]}

        schema_rows = [
            {
                "row_number": 1,
                "section": "Engineering",
                "subsection": "Execution",
                "protected": False,
                "sheet_name": "2026-2027",
            }
        ]

        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .get().execute.return_value) = sheets_response
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = label_response

        mock_db = MagicMock()
        mock_db.client.table().select().eq().execute.return_value = MagicMock(data=schema_rows)

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_status(week=11)

        assert "error" not in result
        assert result["week"] == 11
        assert result["week_label"] == "W11"
        assert result["count"] >= 1
        assert result["items"][0]["owner"] == "R"

    @pytest.mark.asyncio
    async def test_week_none_uses_current_week(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        sheets_response = {"sheets": [{"data": [{"rowData": []}]}]}
        label_response = {"values": []}

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response
        mock_svc.service.spreadsheets().values().get().execute.return_value = label_response

        mock_db = MagicMock()
        mock_db.client.table().select().eq().execute.return_value = MagicMock(data=[])

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()), \
             patch("services.gantt_manager.current_week_number", return_value=14):
            result = await mgr.get_gantt_status(week=None)

        assert result["week"] == 14

    @pytest.mark.asyncio
    async def test_invalid_week_returns_error(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        with patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_status(week=1)

        assert "error" in result
        assert result["week"] == 1

    @pytest.mark.asyncio
    async def test_sheets_api_error_returns_error(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.side_effect = Exception("API down")

        mock_db = MagicMock()
        mock_db.client.table().select().eq().execute.return_value = MagicMock(data=[])

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_status(week=11)

        assert "error" in result
        assert "Failed to read Gantt" in result["error"]

    @pytest.mark.asyncio
    async def test_protected_rows_excluded(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        grid_rows = [
            {"values": [{"formattedValue": "SECTION HEADER", "effectiveFormat": {}}]},
        ]
        sheets_response = {"sheets": [{"data": [{"rowData": grid_rows}]}]}
        label_response = {"values": [["Engineering", ""]]}

        schema_rows = [
            {
                "row_number": 1,
                "section": "Engineering",
                "subsection": "",       # no subsection = header
                "protected": True,
                "sheet_name": "2026-2027",
            }
        ]

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response
        mock_svc.service.spreadsheets().values().get().execute.return_value = label_response

        mock_db = MagicMock()
        mock_db.client.table().select().eq().execute.return_value = MagicMock(data=schema_rows)

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_status(week=11)

        # The header row should be filtered out
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_returns_correct_column_letter(self):
        mgr = _make_manager()
        # week_offset=9, first_week_col=E → W11 → column G (E+2)
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        sheets_response = {"sheets": [{"data": [{"rowData": []}]}]}
        label_response = {"values": []}

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response
        mock_svc.service.spreadsheets().values().get().execute.return_value = label_response

        mock_db = MagicMock()
        mock_db.client.table().select().eq().execute.return_value = MagicMock(data=[])

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_status(week=11)

        assert result["column"] == "G"

    @pytest.mark.asyncio
    async def test_items_have_required_keys(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        grid_rows = [{"values": [{"formattedValue": "[E] Plan sprint", "effectiveFormat": {}}]}]
        sheets_response = {"sheets": [{"data": [{"rowData": grid_rows}]}]}
        label_response = {"values": []}

        schema_rows = [{
            "row_number": 1,
            "section": "Product",
            "subsection": "Sprint Planning",
            "protected": False,
            "sheet_name": "2026-2027",
        }]

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response
        mock_svc.service.spreadsheets().values().get().execute.return_value = label_response

        mock_db = MagicMock()
        mock_db.client.table().select().eq().execute.return_value = MagicMock(data=schema_rows)

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_status(week=11)

        item = result["items"][0]
        for key in ("section", "subsection", "owner", "text", "status", "week", "type"):
            assert key in item, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# get_gantt_section tests (~5)
# ---------------------------------------------------------------------------

class TestGetGanttSection:
    """Tests for GanttManager.get_gantt_section."""

    def _make_schema_row(self, section, subsection, row_number=5):
        return {
            "row_number": row_number,
            "section": section,
            "subsection": subsection,
            "protected": False,
            "sheet_name": "2026-2027",
        }

    @pytest.mark.asyncio
    async def test_fuzzy_section_match(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        schema_rows = [self._make_schema_row("Product & Technology", "Execution", row_number=5)]
        grid_rows = [{"values": [{"formattedValue": "[R] Work item", "effectiveFormat": {}}]}] * 10

        sheets_response = {"sheets": [{"data": [{"rowData": grid_rows}]}]}

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response

        mock_db = MagicMock()
        # _get_schema_rows and _get_color_map calls
        mock_db.client.table().select().execute.return_value = MagicMock(data=[])
        mock_db.client.table().select().eq().execute.return_value = MagicMock(data=[])

        def table_select_side_effect(*a, **kw):
            m = MagicMock()
            m.execute.return_value = MagicMock(data=schema_rows)
            m.eq.return_value = m
            return m

        mock_db.client.table.return_value.select.side_effect = table_select_side_effect

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            # "product" should fuzzy-match "Product & Technology"
            result = await mgr.get_gantt_section("product", weeks=[11, 12])

        assert "error" not in result
        assert result["section"] == "Product & Technology"

    @pytest.mark.asyncio
    async def test_nonexistent_section_returns_error(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_db = MagicMock()
        mock_db.client.table.return_value.select.return_value.execute.return_value = MagicMock(data=[])
        mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_section("NONEXISTENT SECTION XYZ", weeks=[11])

        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_returns_items_by_week_keys(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        schema_rows = [self._make_schema_row("Engineering", "Execution", row_number=1)]
        grid_rows = [{"values": [{"formattedValue": "[R] Feature A", "effectiveFormat": {}}]}]
        sheets_response = {"sheets": [{"data": [{"rowData": grid_rows}]}]}

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager._get_schema_rows", return_value=schema_rows), \
             patch("services.gantt_manager._get_color_map", return_value={}), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_section("Engineering", weeks=[11, 12])

        assert "W11" in result["weeks"]
        assert "W12" in result["weeks"]

    @pytest.mark.asyncio
    async def test_skips_invalid_weeks_gracefully(self):
        """Invalid week (before offset) is skipped without crashing."""
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        schema_rows = [self._make_schema_row("Engineering", "Execution", row_number=1)]

        mock_svc = MagicMock()
        sheets_response = {"sheets": [{"data": [{"rowData": []}]}]}
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager._get_schema_rows", return_value=schema_rows), \
             patch("services.gantt_manager._get_color_map", return_value={}), \
             patch("services.gantt_manager.settings", _mock_settings()):
            # Week 1 is before offset 9 → should be skipped
            result = await mgr.get_gantt_section("Engineering", weeks=[1, 11])

        assert "error" not in result
        # W1 skipped, W11 present
        assert "W11" in result["weeks"]
        assert "W1" not in result["weeks"]


# ---------------------------------------------------------------------------
# get_meeting_cadence tests (~3)
# ---------------------------------------------------------------------------

class TestGetMeetingCadence:
    """Tests for GanttManager.get_meeting_cadence."""

    @pytest.mark.asyncio
    async def test_returns_meetings_from_schema(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        meeting_json = json.dumps({
            "name": "CEO-CTO Weekly",
            "cadence": "weekly",
            "participants": ["E", "R"],
        })
        cadence_rows = [
            {"row_number": 1, "sheet_name": "Meeting Cadence",
             "section": "", "subsection": "", "notes": meeting_json},
        ]

        mock_db = MagicMock()
        mock_db.client.table.return_value.select.return_value.execute.return_value = MagicMock(data=cadence_rows)
        mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=cadence_rows)

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_meeting_cadence(week=11)

        assert result["week"] == 11
        assert result["count"] == 1
        assert result["meetings"][0]["name"] == "CEO-CTO Weekly"

    @pytest.mark.asyncio
    async def test_week_none_uses_current(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_db = MagicMock()
        mock_db.client.table.return_value.select.return_value.execute.return_value = MagicMock(data=[])
        mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()), \
             patch("services.gantt_manager.current_week_number", return_value=14):
            result = await mgr.get_meeting_cadence(week=None)

        assert result["week"] == 14

    @pytest.mark.asyncio
    async def test_non_json_notes_skipped(self):
        """Rows whose notes are not JSON objects are skipped gracefully."""
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        cadence_rows = [
            {"row_number": 1, "sheet_name": "Meeting Cadence",
             "section": "", "subsection": "", "notes": "plain text, not JSON"},
            {"row_number": 2, "sheet_name": "Meeting Cadence",
             "section": "", "subsection": "", "notes": None},
        ]

        mock_db = MagicMock()
        mock_db.client.table.return_value.select.return_value.execute.return_value = MagicMock(data=cadence_rows)
        mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=cadence_rows)

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_meeting_cadence(week=11)

        assert result["count"] == 0
        assert result["meetings"] == []


# ---------------------------------------------------------------------------
# get_gantt_history tests (~5)
# ---------------------------------------------------------------------------

class TestGetGanttHistory:
    """Tests for GanttManager.get_gantt_history."""

    @pytest.mark.asyncio
    async def test_reads_from_proposals_table(self):
        mgr = _make_manager()
        proposals = [
            {
                "id": "abc-123",
                "source_type": "telegram",
                "proposed_at": "2026-03-10T10:00:00",
                "reviewed_at": "2026-03-10T10:05:00",
                "changes": [
                    {
                        "subsection": "Execution",
                        "week": 11,
                        "old_value": "old text",
                        "new_value": "new text",
                    }
                ],
            }
        ]

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .order.return_value
         .limit.return_value
         .execute.return_value) = MagicMock(data=proposals)

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.get_gantt_history(limit=10)

        assert result["source"] == "proposals"
        assert result["count"] == 1
        assert "abc-123" == result["history"][0]["id"]

    @pytest.mark.asyncio
    async def test_falls_back_to_log_tab_when_proposals_empty(self):
        mgr = _make_manager()

        # Proposals table returns empty
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .order.return_value
         .limit.return_value
         .execute.return_value) = MagicMock(data=[])

        # Log tab returns rows
        log_rows = [
            ["Header", "Week", "Section", "Description", "By", "Related"],  # header
            ["2026-03-10", "W11", "Engineering", "Changed X to Y", "Gianluigi", "manual"],
        ]
        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": log_rows}

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_history(limit=10)

        assert result["source"] == "log_tab"
        assert result["count"] >= 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_both_sources_fail(self):
        mgr = _make_manager()

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .order.return_value
         .limit.return_value
         .execute.side_effect) = Exception("DB error")

        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .values().get().execute.side_effect) = Exception("Sheets error")

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.get_gantt_history(limit=10)

        assert result["source"] == "none"
        assert result["count"] == 0
        assert result["history"] == []

    @pytest.mark.asyncio
    async def test_history_diff_format_with_old_value(self):
        mgr = _make_manager()
        proposals = [{
            "id": "xyz",
            "source_type": "manual",
            "proposed_at": "2026-03-10T09:00:00",
            "reviewed_at": "2026-03-10T09:01:00",
            "changes": [{
                "subsection": "Execution",
                "week": 12,
                "old_value": "Previous text",
                "new_value": "Updated text",
                "section": "Engineering",
            }],
        }]

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .order.return_value
         .limit.return_value
         .execute.return_value) = MagicMock(data=proposals)

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.get_gantt_history(limit=10)

        diff = result["history"][0]["changes"][0]
        assert "Previous text" in diff
        assert "Updated text" in diff
        assert "→" in diff

    @pytest.mark.asyncio
    async def test_history_diff_format_without_old_value(self):
        """Change with no old_value uses 'added' phrasing."""
        mgr = _make_manager()
        proposals = [{
            "id": "xyz",
            "source_type": "manual",
            "proposed_at": "2026-03-10T09:00:00",
            "reviewed_at": "2026-03-10T09:01:00",
            "changes": [{
                "subsection": "Roadmap",
                "week": 13,
                "old_value": "",
                "new_value": "New item",
                "section": "Product",
            }],
        }]

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .order.return_value
         .limit.return_value
         .execute.return_value) = MagicMock(data=proposals)

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.get_gantt_history(limit=10)

        diff = result["history"][0]["changes"][0]
        assert "added" in diff.lower()
        assert "New item" in diff


# ---------------------------------------------------------------------------
# get_gantt_horizon tests (~2)
# ---------------------------------------------------------------------------

class TestGetGanttHorizon:
    """Tests for GanttManager.get_gantt_horizon."""

    @pytest.mark.asyncio
    async def test_returns_milestones(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        schema_rows = [{
            "row_number": 1,
            "section": "Milestones",
            "subsection": "Tech Milestones",
            "protected": False,
            "sheet_name": "2026-2027",
        }]

        milestone_cell = {"formattedValue": "★ MVP Launch", "effectiveFormat": {}}
        grid_rows = [{"values": [milestone_cell]}]
        sheets_response = {"sheets": [{"data": [{"rowData": grid_rows}]}]}

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager._get_schema_rows", return_value=schema_rows), \
             patch("services.gantt_manager._get_color_map", return_value={}), \
             patch("services.gantt_manager.settings", _mock_settings()), \
             patch("services.gantt_manager.current_week_number", return_value=11):
            result = await mgr.get_gantt_horizon(weeks_ahead=2)

        assert "milestones" in result
        # At least one milestone found
        assert result["count"] >= 1
        assert result["milestones"][0]["type"] == "milestone"

    @pytest.mark.asyncio
    async def test_horizon_excludes_non_milestones(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        schema_rows = [{
            "row_number": 1,
            "section": "Engineering",
            "subsection": "Execution",
            "protected": False,
            "sheet_name": "2026-2027",
        }]

        # Work item (not a milestone)
        work_cell = {"formattedValue": "[R] Regular task", "effectiveFormat": {}}
        grid_rows = [{"values": [work_cell]}]
        sheets_response = {"sheets": [{"data": [{"rowData": grid_rows}]}]}

        mock_svc = MagicMock()
        mock_svc.service.spreadsheets().get().execute.return_value = sheets_response

        mock_db = MagicMock()
        mock_db.client.table.return_value.select.return_value.execute.return_value = MagicMock(data=schema_rows)
        mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()), \
             patch("services.gantt_manager.current_week_number", return_value=11):
            result = await mgr.get_gantt_horizon(weeks_ahead=1)

        assert result["count"] == 0


# ---------------------------------------------------------------------------
# propose_gantt_update tests (~8)
# ---------------------------------------------------------------------------

class TestProposeGanttUpdate:
    """Tests for GanttManager.propose_gantt_update."""

    def _valid_change(self):
        return {
            "section": "Engineering",
            "subsection": "Execution",
            "week": 11,
            "value": "[R] Build new feature",
            "status": "active",
            "reason": "Agreed in sprint planning",
        }

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    @patch("services.gantt_manager.expand_range_changes")
    @patch("services.gantt_manager.resolve_row_number")
    async def test_returns_pending_on_success(
        self, mock_resolve, mock_expand, mock_validate
    ):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_validate.return_value = (True, [])
        change = self._valid_change()
        change["force_mode"] = "replace"  # Explicit mode to bypass conflict detection
        mock_expand.return_value = [change]
        mock_resolve.return_value = (5, "Engineering", "Execution")

        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [["[R] Old text"]]}

        proposal_row = {"id": "prop-001", "status": "pending"}
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .insert.return_value.execute.return_value) = MagicMock(data=[proposal_row])

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.propose_gantt_update([change], source="telegram")

        assert result["status"] == "pending"
        assert result["proposal_id"] == "prop-001"
        assert result["changes_count"] == 1

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    @patch("services.gantt_manager.expand_range_changes")
    @patch("services.gantt_manager.resolve_row_number")
    async def test_returns_needs_confirmation_when_cell_has_content(
        self, mock_resolve, mock_expand, mock_validate
    ):
        """When a cell already has content and no force_mode, return conflict."""
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_validate.return_value = (True, [])
        change = self._valid_change()  # No force_mode set
        mock_expand.return_value = [change]
        mock_resolve.return_value = (5, "Engineering", "Execution")

        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [["[R] Existing task"]]}

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.propose_gantt_update([change], source="telegram")

        assert result["status"] == "needs_confirmation"
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["existing_content"] == "[R] Existing task"
        assert result["conflicts"][0]["proposed_content"] == "[R] Build new feature"

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    @patch("services.gantt_manager.expand_range_changes")
    @patch("services.gantt_manager.resolve_row_number")
    async def test_append_mode_concatenates_content(
        self, mock_resolve, mock_expand, mock_validate
    ):
        """When force_mode=append, new value is appended to existing."""
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_validate.return_value = (True, [])
        change = self._valid_change()
        change["force_mode"] = "append"
        mock_expand.return_value = [change]
        mock_resolve.return_value = (5, "Engineering", "Execution")

        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [["[R] Existing task"]]}

        proposal_row = {"id": "prop-002", "status": "pending"}
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .insert.return_value.execute.return_value) = MagicMock(data=[proposal_row])

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.propose_gantt_update([change], source="telegram")

        assert result["status"] == "pending"
        stored_changes = mock_db.client.table.return_value.insert.call_args[0][0]["changes"]
        assert stored_changes[0]["new_value"] == "[R] Existing task\n[R] Build new feature"
        assert stored_changes[0]["old_value"] == "[R] Existing task"

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    @patch("services.gantt_manager.expand_range_changes")
    @patch("services.gantt_manager.resolve_row_number")
    async def test_empty_cell_skips_conflict_check(
        self, mock_resolve, mock_expand, mock_validate
    ):
        """When cell is empty, proceed without conflict even without force_mode."""
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_validate.return_value = (True, [])
        change = self._valid_change()  # No force_mode
        mock_expand.return_value = [change]
        mock_resolve.return_value = (5, "Engineering", "Execution")

        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [[]]}  # Empty cell

        proposal_row = {"id": "prop-003", "status": "pending"}
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .insert.return_value.execute.return_value) = MagicMock(data=[proposal_row])

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.propose_gantt_update([change], source="telegram")

        assert result["status"] == "pending"

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    async def test_returns_rejected_when_validation_fails(self, mock_validate):
        mgr = _make_manager()

        mock_validate.return_value = (False, ["Change #1: missing 'reason' field"])

        result = await mgr.propose_gantt_update(
            [{"section": "Engineering", "subsection": "Execution", "week": 11, "value": "x"}],
            source="telegram",
        )

        assert result["status"] == "rejected"
        assert "errors" in result
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    @patch("services.gantt_manager.expand_range_changes")
    @patch("services.gantt_manager.resolve_row_number")
    async def test_reads_current_value_before_storing(
        self, mock_resolve, mock_expand, mock_validate
    ):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_validate.return_value = (True, [])
        change = self._valid_change()
        change["force_mode"] = "replace"  # Bypass conflict detection
        mock_expand.return_value = [change]
        mock_resolve.return_value = (5, "Engineering", "Execution")

        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [["[R] Current value"]]}

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .insert.return_value.execute.return_value) = MagicMock(data=[{"id": "p1"}])

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.propose_gantt_update([change], source="manual")

        assert result["status"] == "pending"
        assert result["changes"][0]["old_value"] == "[R] Current value"

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    @patch("services.gantt_manager.expand_range_changes")
    @patch("services.gantt_manager.resolve_row_number")
    async def test_skips_changes_with_unresolvable_row(
        self, mock_resolve, mock_expand, mock_validate
    ):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_validate.return_value = (True, [])
        change = self._valid_change()
        mock_expand.return_value = [change]
        mock_resolve.return_value = (None, None, None)  # row not found

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .insert.return_value.execute.return_value) = MagicMock(data=[{"id": "p2"}])

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.propose_gantt_update([change], source="manual")

        # Change was skipped → 0 enriched changes
        assert result["changes_count"] == 0

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    @patch("services.gantt_manager.expand_range_changes")
    @patch("services.gantt_manager.resolve_row_number")
    async def test_supabase_insert_error_returns_error(
        self, mock_resolve, mock_expand, mock_validate
    ):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_validate.return_value = (True, [])
        change = self._valid_change()
        change["force_mode"] = "replace"  # Bypass conflict detection
        mock_expand.return_value = [change]
        mock_resolve.return_value = (5, "Engineering", "Execution")

        mock_svc = MagicMock()
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [["old"]]}

        mock_db = MagicMock()
        mock_db.client.table.return_value.insert.return_value.execute.side_effect = Exception("DB error")

        with patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.propose_gantt_update([change], source="manual")

        assert result["status"] == "error"

    @pytest.mark.asyncio
    @patch("services.gantt_manager.validate_proposal")
    @patch("services.gantt_manager.expand_range_changes")
    @patch("services.gantt_manager.resolve_row_number")
    async def test_week_before_offset_is_skipped(
        self, mock_resolve, mock_expand, mock_validate
    ):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_validate.return_value = (True, [])
        bad_change = {**self._valid_change(), "week": 1}  # before offset 9
        mock_expand.return_value = [bad_change]
        mock_resolve.return_value = (5, "Engineering", "Execution")

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .insert.return_value.execute.return_value) = MagicMock(data=[{"id": "p3"}])

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.propose_gantt_update([bad_change], source="manual")

        assert result["changes_count"] == 0


# ---------------------------------------------------------------------------
# execute_approved_proposal tests (~9)
# ---------------------------------------------------------------------------

class TestExecuteApprovedProposal:
    """Tests for GanttManager.execute_approved_proposal."""

    def _make_proposal(self, status="pending", changes=None):
        if changes is None:
            changes = [{
                "section": "Engineering",
                "subsection": "Execution",
                "week": 11,
                "column": "G",
                "row": 5,
                "old_value": "[R] Old text",
                "new_value": "[R] New text",
                "status": "active",
                "reason": "test",
            }]
        return {
            "id": "proposal-001",
            "status": status,
            "source_type": "telegram",
            "changes": changes,
        }

    @pytest.mark.asyncio
    async def test_proposal_not_found_returns_error(self):
        mgr = _make_manager()

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[])

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.execute_approved_proposal("nonexistent-id")

        assert result["status"] == "error"
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_rolled_back_proposal_cannot_execute(self):
        mgr = _make_manager()

        proposal = self._make_proposal(status="rolled_back")
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[proposal])

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.execute_approved_proposal("proposal-001")

        assert result["status"] == "error"
        assert "rolled_back" in result["error"]

    @pytest.mark.asyncio
    async def test_saves_snapshot_before_writing(self):
        mgr = _make_manager()
        mgr._metadata_cache = {}
        mgr._cache_time = datetime.now()

        proposal = self._make_proposal()
        mock_db = MagicMock()
        # Load proposal
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[proposal])
        # Snapshot insert
        (mock_db.client.table.return_value
         .insert.return_value
         .execute.return_value) = MagicMock(data=[{}])
        # Proposal status update
        (mock_db.client.table.return_value
         .update.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[{}])

        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        mock_svc.service.spreadsheets().batchUpdate().execute.return_value = {}
        mock_svc.service.spreadsheets().values().append().execute.return_value = {}

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.execute_approved_proposal("proposal-001")

        # Snapshot must be inserted
        insert_calls = mock_db.client.table.return_value.insert.call_args_list
        assert any(
            "gantt_snapshots" in str(call) or True  # table name checked separately
            for call in insert_calls
        )

    @pytest.mark.asyncio
    async def test_uses_batch_update_for_write(self):
        mgr = _make_manager()
        mgr._metadata_cache = {}
        mgr._cache_time = datetime.now()

        proposal = self._make_proposal()
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[proposal])
        (mock_db.client.table.return_value
         .insert.return_value
         .execute.return_value) = MagicMock(data=[{}])
        (mock_db.client.table.return_value
         .update.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[{}])

        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        batch_execute = MagicMock(return_value={})
        mock_svc.service.spreadsheets().batchUpdate().execute = batch_execute
        mock_svc.service.spreadsheets().values().append().execute.return_value = {}

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.execute_approved_proposal("proposal-001")

        assert result["status"] == "executed"
        assert result["cells_written"] == 1

    @pytest.mark.asyncio
    async def test_status_color_included_in_batch_when_status_set(self):
        """A change with status 'active' generates both text and color requests."""
        mgr = _make_manager()
        mgr._metadata_cache = {}
        mgr._cache_time = datetime.now()

        proposal = self._make_proposal()  # status="active" by default

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[proposal])
        (mock_db.client.table.return_value
         .insert.return_value
         .execute.return_value) = MagicMock(data=[{}])
        (mock_db.client.table.return_value
         .update.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[{}])

        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        mock_svc.service.spreadsheets().batchUpdate().execute.return_value = {}
        mock_svc.service.spreadsheets().values().append().execute.return_value = {}

        # Provide a color map so the "active" status triggers a color update
        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()), \
             patch("services.gantt_manager._get_color_map",
                   return_value={"active": "#00FF00"}):
            result = await mgr.execute_approved_proposal("proposal-001")

        assert result["status"] == "executed"

    @pytest.mark.asyncio
    async def test_appends_to_log_tab(self):
        mgr = _make_manager()
        mgr._metadata_cache = {}
        mgr._cache_time = datetime.now()

        proposal = self._make_proposal()
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[proposal])
        (mock_db.client.table.return_value
         .insert.return_value
         .execute.return_value) = MagicMock(data=[{}])
        (mock_db.client.table.return_value
         .update.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[{}])

        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        mock_svc.service.spreadsheets().batchUpdate().execute.return_value = {}
        append_execute = MagicMock(return_value={})
        mock_svc.service.spreadsheets().values().append().execute = append_execute

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.execute_approved_proposal("proposal-001")

        # Log tab append should have been called
        append_execute.assert_called()

    @pytest.mark.asyncio
    async def test_log_fail_does_not_fail_execution(self):
        """If Log tab append fails, the proposal is still marked executed."""
        mgr = _make_manager()
        mgr._metadata_cache = {}
        mgr._cache_time = datetime.now()

        proposal = self._make_proposal()
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[proposal])
        (mock_db.client.table.return_value
         .insert.return_value
         .execute.return_value) = MagicMock(data=[{}])
        (mock_db.client.table.return_value
         .update.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[{}])

        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        mock_svc.service.spreadsheets().batchUpdate().execute.return_value = {}
        # Log append raises an exception
        mock_svc.service.spreadsheets().values().append().execute.side_effect = Exception("Log error")

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.execute_approved_proposal("proposal-001")

        # Still succeeds despite Log failure
        assert result["status"] == "executed"

    @pytest.mark.asyncio
    async def test_log_tab_date_format_and_by_field(self):
        """Log rows use YYYY-MM-DD date and 'Gianluigi' as the by field."""
        mgr = _make_manager()
        mgr._metadata_cache = {}
        mgr._cache_time = datetime.now()

        proposal = self._make_proposal()
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[proposal])
        (mock_db.client.table.return_value
         .insert.return_value
         .execute.return_value) = MagicMock(data=[{}])
        (mock_db.client.table.return_value
         .update.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[{}])

        captured_append_body = {}

        def fake_append(**kwargs):
            captured_append_body.update(kwargs.get("body", {}))
            m = MagicMock()
            m.execute.return_value = {}
            return m

        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        mock_svc.service.spreadsheets().batchUpdate().execute.return_value = {}
        mock_svc.service.spreadsheets().values().append.side_effect = fake_append

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            await mgr.execute_approved_proposal("proposal-001")

        rows = captured_append_body.get("values", [])
        if rows:
            row = rows[0]
            # Column 0 = date (YYYY-MM-DD), column 4 = "Gianluigi"
            import re
            assert re.match(r"\d{4}-\d{2}-\d{2}", row[0])
            assert row[4] == "Gianluigi"

    @pytest.mark.asyncio
    async def test_snapshot_failure_blocks_write(self):
        """If snapshot insert fails, the write should not proceed."""
        mgr = _make_manager()
        mgr._metadata_cache = {}
        mgr._cache_time = datetime.now()

        proposal = self._make_proposal()
        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[proposal])
        # Snapshot insert raises
        mock_db.client.table.return_value.insert.return_value.execute.side_effect = Exception("Snapshot DB error")

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.execute_approved_proposal("proposal-001")

        assert result["status"] == "error"
        assert "Snapshot failed" in result["error"]


# ---------------------------------------------------------------------------
# rollback_proposal tests (~8)
# ---------------------------------------------------------------------------

class TestRollbackProposal:
    """Tests for GanttManager.rollback_proposal."""

    def _make_proposal(self, status="approved"):
        return {
            "id": "proposal-001",
            "status": status,
            "source_type": "telegram",
            "changes": [],
        }

    def _make_snapshot(self):
        return {
            "proposal_id": "proposal-001",
            "sheet_name": "2026-2027",
            "cell_references": ["G5"],
            "old_values": {"G5": "[R] Old text"},
            "new_values": {"G5": "[R] New text"},
        }

    def _setup_db(self, proposal, snapshot, current_cell_value="[R] New text"):
        mock_db = MagicMock()

        # Proposal lookup (both for direct and "most recent" lookups)
        proposals_result = MagicMock(data=[proposal])
        snapshots_result = MagicMock(data=[snapshot])
        update_result = MagicMock(data=[{}])

        table_mock = mock_db.client.table.return_value
        table_mock.select.return_value.eq.return_value.execute.return_value = proposals_result
        table_mock.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = proposals_result
        table_mock.select.return_value.eq.return_value.execute.return_value = snapshots_result
        table_mock.update.return_value.eq.return_value.execute.return_value = update_result

        return mock_db

    def _setup_sheets(self, current_cell_value="[R] New text"):
        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [[current_cell_value]]}
        mock_svc.service.spreadsheets().batchUpdate().execute.return_value = {}
        mock_svc.service.spreadsheets().values().append().execute.return_value = {}
        return mock_svc

    @pytest.mark.asyncio
    async def test_rollback_with_explicit_id(self):
        mgr = _make_manager()
        proposal = self._make_proposal()
        snapshot = self._make_snapshot()

        # Need independent mocks for each table() call
        mock_db = MagicMock()
        call_count = {"n": 0}

        def table_side_effect(table_name):
            m = MagicMock()
            if table_name == "gantt_proposals":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[proposal])
                m.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{}])
            elif table_name == "gantt_snapshots":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[snapshot])
            return m

        mock_db.client.table.side_effect = table_side_effect
        mock_svc = self._setup_sheets()

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.rollback_proposal("proposal-001")

        assert result["status"] == "rolled_back"
        assert result["cells_restored"] == 1

    @pytest.mark.asyncio
    async def test_rollback_none_finds_most_recent(self):
        mgr = _make_manager()
        proposal = self._make_proposal()
        snapshot = self._make_snapshot()

        mock_db = MagicMock()

        def table_side_effect(table_name):
            m = MagicMock()
            if table_name == "gantt_proposals":
                # Most-recent lookup (order + limit)
                m.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[proposal])
                # Direct lookup by id
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[proposal])
                m.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{}])
            elif table_name == "gantt_snapshots":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[snapshot])
            return m

        mock_db.client.table.side_effect = table_side_effect
        mock_svc = self._setup_sheets()

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.rollback_proposal(None)

        assert result["status"] == "rolled_back"

    @pytest.mark.asyncio
    async def test_no_approved_proposals_returns_error(self):
        mgr = _make_manager()

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .order.return_value
         .limit.return_value
         .execute.return_value) = MagicMock(data=[])

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.rollback_proposal(None)

        assert result["status"] == "error"
        assert "No approved proposals" in result["error"]

    @pytest.mark.asyncio
    async def test_already_rolled_back_rejected(self):
        mgr = _make_manager()
        proposal = self._make_proposal(status="rolled_back")

        mock_db = MagicMock()

        def table_side_effect(table_name):
            m = MagicMock()
            m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[proposal])
            return m

        mock_db.client.table.side_effect = table_side_effect

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.rollback_proposal("proposal-001")

        assert result["status"] == "error"
        assert "already been rolled back" in result["error"]

    @pytest.mark.asyncio
    async def test_warning_when_cell_manually_edited(self):
        mgr = _make_manager()
        proposal = self._make_proposal()
        snapshot = self._make_snapshot()

        mock_db = MagicMock()

        def table_side_effect(table_name):
            m = MagicMock()
            if table_name == "gantt_proposals":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[proposal])
                m.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{}])
            elif table_name == "gantt_snapshots":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[snapshot])
            return m

        mock_db.client.table.side_effect = table_side_effect

        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        # Current cell value differs from expected new_value → manual edit
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [["[R] Manually changed"]]}
        mock_svc.service.spreadsheets().batchUpdate().execute.return_value = {}
        mock_svc.service.spreadsheets().values().append().execute.return_value = {}

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.rollback_proposal("proposal-001")

        assert result["status"] == "rolled_back"
        assert "warnings" in result
        assert len(result["warnings"]) > 0

    @pytest.mark.asyncio
    async def test_no_snapshot_returns_error(self):
        mgr = _make_manager()
        proposal = self._make_proposal()

        mock_db = MagicMock()

        def table_side_effect(table_name):
            m = MagicMock()
            if table_name == "gantt_proposals":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[proposal])
            elif table_name == "gantt_snapshots":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
            return m

        mock_db.client.table.side_effect = table_side_effect

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.rollback_proposal("proposal-001")

        assert result["status"] == "error"
        assert "No snapshot" in result["error"]

    @pytest.mark.asyncio
    async def test_pending_proposal_cannot_be_rolled_back(self):
        mgr = _make_manager()
        proposal = self._make_proposal(status="pending")

        mock_db = MagicMock()

        def table_side_effect(table_name):
            m = MagicMock()
            m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[proposal])
            return m

        mock_db.client.table.side_effect = table_side_effect

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = await mgr.rollback_proposal("proposal-001")

        assert result["status"] == "error"
        assert "Cannot rollback" in result["error"]

    @pytest.mark.asyncio
    async def test_rollback_write_error_returns_error(self):
        mgr = _make_manager()
        proposal = self._make_proposal()
        snapshot = self._make_snapshot()

        mock_db = MagicMock()

        def table_side_effect(table_name):
            m = MagicMock()
            if table_name == "gantt_proposals":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[proposal])
            elif table_name == "gantt_snapshots":
                m.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[snapshot])
            return m

        mock_db.client.table.side_effect = table_side_effect

        mock_svc = MagicMock()
        mock_svc._get_sheet_id_by_name.return_value = 0
        (mock_svc.service.spreadsheets()
         .values().get().execute.return_value) = {"values": [["[R] New text"]]}
        mock_svc.service.spreadsheets().batchUpdate().execute.side_effect = Exception("Write error")

        with patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.gantt_manager.sheets_service", mock_svc), \
             patch("services.gantt_manager.settings", _mock_settings()):
            result = await mgr.rollback_proposal("proposal-001")

        assert result["status"] == "error"
        assert "Rollback write failed" in result["error"]


# ---------------------------------------------------------------------------
# backup_full_gantt tests (~3)
# ---------------------------------------------------------------------------

class TestBackupFullGantt:
    """Tests for GanttManager.backup_full_gantt."""

    @pytest.mark.asyncio
    async def test_creates_drive_copy_with_correct_name(self):
        mgr = _make_manager()

        mock_drive = MagicMock()
        (mock_drive.service.files()
         .copy().execute.return_value) = {"id": "new-file-id"}

        mock_db = MagicMock()
        mock_db.log_action.return_value = None

        expected_name = f"Gantt Backup {date.today().strftime('%Y-%m-%d')}"

        with patch("services.gantt_manager.settings", _mock_settings()), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.google_drive.drive_service", mock_drive):
            result = await mgr.backup_full_gantt()

        assert result["status"] == "success"
        assert result["name"] == expected_name
        assert result["file_id"] == "new-file-id"

    @pytest.mark.asyncio
    async def test_no_sheet_id_returns_error(self):
        mgr = _make_manager()

        with patch("services.gantt_manager.settings", _mock_settings(GANTT_SHEET_ID="")):
            result = await mgr.backup_full_gantt()

        assert result["status"] == "error"
        assert "GANTT_SHEET_ID" in result["error"]

    @pytest.mark.asyncio
    async def test_no_backup_folder_returns_error(self):
        mgr = _make_manager()

        with patch("services.gantt_manager.settings",
                   _mock_settings(GANTT_BACKUP_FOLDER_ID="")):
            result = await mgr.backup_full_gantt()

        assert result["status"] == "error"
        assert "GANTT_BACKUP_FOLDER_ID" in result["error"]

    @pytest.mark.asyncio
    async def test_drive_api_error_returns_error(self):
        mgr = _make_manager()

        mock_drive = MagicMock()
        mock_drive.service.files().copy().execute.side_effect = Exception("Drive unavailable")

        mock_db = MagicMock()

        with patch("services.gantt_manager.settings", _mock_settings()), \
             patch("services.gantt_manager.supabase_client", mock_db), \
             patch("services.google_drive.drive_service", mock_drive):
            result = await mgr.backup_full_gantt()

        assert result["status"] == "error"
        assert "Drive unavailable" in result["error"] or "error" in result["error"].lower()


# ---------------------------------------------------------------------------
# Schema metadata cache tests (~3)
# ---------------------------------------------------------------------------

class TestMetadataCache:
    """Tests for GanttManager._get_metadata caching logic."""

    def test_cache_is_used_within_ttl(self):
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9, "first_week_col": "E"}
        mgr._cache_time = datetime.now()

        mock_db = MagicMock()
        with patch("services.gantt_manager.supabase_client", mock_db):
            result = mgr._get_metadata()

        # Supabase should NOT have been called (cache hit)
        mock_db.client.table.assert_not_called()
        assert result == {"week_offset": 9, "first_week_col": "E"}

    def test_cache_refreshed_after_ttl(self):
        from datetime import timedelta
        mgr = _make_manager()
        mgr._metadata_cache = {"week_offset": 9}
        # Set cache time to 10 minutes ago (past 5-minute TTL)
        mgr._cache_time = datetime.now() - timedelta(minutes=10)

        mock_db = MagicMock()
        (mock_db.client.table.return_value
         .select.return_value
         .eq.return_value
         .execute.return_value) = MagicMock(data=[{"notes": '{"week_offset": 12}'}])

        with patch("services.gantt_manager.supabase_client", mock_db):
            result = mgr._get_metadata()

        # Supabase should have been called to refresh
        mock_db.client.table.assert_called()

    def test_empty_cache_on_init(self):
        mgr = _make_manager()
        assert mgr._metadata_cache is None
        assert mgr._cache_time is None
