"""
Tests for task category system (2026-06 category realignment).

`tasks.category` carries the Gantt-area taxonomy. Canonical values come from
the live `areas` table; models.schemas.TaskCategory is the static mirror
(6 Gantt areas + General) and GENERAL_CATEGORY is the blank/misfit fallback.

Tests:
- TaskCategory enum values (7: 6 Gantt areas + General) + GENERAL_CATEGORY
- Task model accepts category as a plain string (no enum coercion)
- supabase_client.create_task() passes category
- supabase_client.create_tasks_batch() passes category
- supabase_client.get_tasks() filters by category + excludes archived by default
- Tool definitions include the 7-value category enum
- Google Sheets task tracker keeps the Category column + canonical constants
- Extraction prompt builds the CATEGORY rule from the live areas
- Transcript processor extraction schema includes category
- Weekly digest format includes category columns
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from models.schemas import GENERAL_CATEGORY, TaskCategory, Task


# =============================================================================
# TaskCategory Enum
# =============================================================================

class TestTaskCategoryEnum:
    """Tests for the TaskCategory enum (static mirror of the live areas table)."""

    def test_has_seven_categories(self):
        """Should have exactly 7 categories: the 6 Gantt areas + General."""
        assert len(TaskCategory) == 7

    def test_product_tech(self):
        assert TaskCategory.PRODUCT_TECH == "PRODUCT & TECHNOLOGY"

    def test_sales_bd(self):
        assert TaskCategory.SALES_BD == "SALES & BUSINESS DEVELOPMENT"

    def test_fundraising_ir(self):
        assert TaskCategory.FUNDRAISING_IR == "FUNDRAISING & INVESTOR RELATIONS"

    def test_legal_corp_finance(self):
        assert TaskCategory.LEGAL_CORP_FINANCE == "LEGAL, CORPORATE & FINANCE"

    def test_client_delivery_ops(self):
        assert TaskCategory.CLIENT_DELIVERY_OPS == "CLIENT DELIVERY & OPERATIONS"

    def test_team_hr(self):
        assert TaskCategory.TEAM_HR == "TEAM & HUMAN RESOURCES"

    def test_general_fallback(self):
        assert TaskCategory.GENERAL == "General"
        assert GENERAL_CATEGORY == "General"


# =============================================================================
# Task Model
# =============================================================================

class TestTaskModelCategory:
    """Tests for category field on Task model (plain string, no enum coercion)."""

    def test_task_accepts_category(self):
        """Task model should accept a category field."""
        task = Task(
            title="Build dashboard",
            assignee="Roye",
            category=TaskCategory.PRODUCT_TECH,
        )
        assert task.category == "PRODUCT & TECHNOLOGY"

    def test_task_category_defaults_to_none(self):
        """Category should default to None when not provided."""
        task = Task(title="Generic task", assignee="Team")
        assert task.category is None

    def test_task_accepts_string_category(self):
        """Category is `str | None` — canonical strings pass through unchanged."""
        task = Task(
            title="File patent",
            assignee="Eyal",
            category="LEGAL, CORPORATE & FINANCE",
        )
        assert task.category == "LEGAL, CORPORATE & FINANCE"

    def test_task_keeps_non_canonical_string(self):
        """No enum coercion: an off-list value is kept as-is (canonical values
        live in the areas table; resolve_category canonicalizes free text)."""
        task = Task(title="t", assignee="a", category="Something Custom")
        assert task.category == "Something Custom"


# =============================================================================
# Supabase Client — create_task
# =============================================================================

class TestSupabaseCreateTaskCategory:
    """Tests for category in supabase_client.create_task()."""

    def _make_client(self):
        """Create a SupabaseClient with a mocked _client using per-table mocks."""
        from services.supabase_client import SupabaseClient
        client = SupabaseClient.__new__(SupabaseClient)
        mock_internal = MagicMock()
        object.__setattr__(client, '_client', mock_internal)

        # Use separate mocks for tasks vs audit_log tables
        tasks_table = MagicMock()
        audit_table = MagicMock()
        tasks_table.insert.return_value.execute.return_value = MagicMock(
            data=[{"id": "task-1", "title": "Test", "category": "PRODUCT & TECHNOLOGY"}]
        )

        def table_router(name):
            if name == "tasks":
                return tasks_table
            return audit_table

        mock_internal.table.side_effect = table_router
        return client, tasks_table

    def test_create_task_passes_category(self):
        """create_task() should include category in the insert data."""
        client, tasks_table = self._make_client()

        client.create_task(
            title="Build API",
            assignee="Roye",
            priority="H",
            category="PRODUCT & TECHNOLOGY",
        )

        insert_data = tasks_table.insert.call_args[0][0]
        assert insert_data["category"] == "PRODUCT & TECHNOLOGY"

    def test_create_task_category_defaults_to_none(self):
        """create_task() should pass category=None when not specified."""
        client, tasks_table = self._make_client()

        client.create_task(
            title="Generic task",
            assignee="Team",
        )

        insert_data = tasks_table.insert.call_args[0][0]
        assert insert_data["category"] is None


# =============================================================================
# Supabase Client — create_tasks_batch
# =============================================================================

class TestSupabaseCreateTasksBatchCategory:
    """Tests for category in create_tasks_batch()."""

    def test_batch_passes_category(self):
        """create_tasks_batch() should include category for each task."""
        from services.supabase_client import SupabaseClient
        client = SupabaseClient.__new__(SupabaseClient)
        mock_internal = MagicMock()
        object.__setattr__(client, '_client', mock_internal)

        mock_table = MagicMock()
        mock_internal.table.return_value = mock_table
        mock_insert = MagicMock()
        mock_table.insert.return_value = mock_insert
        mock_insert.execute.return_value = MagicMock(data=[
            {"id": "t1", "category": "PRODUCT & TECHNOLOGY"},
            {"id": "t2", "category": "SALES & BUSINESS DEVELOPMENT"},
        ])

        tasks = [
            {"title": "Build API", "assignee": "Roye", "category": "PRODUCT & TECHNOLOGY"},
            {"title": "Call investor", "assignee": "Eyal", "category": "SALES & BUSINESS DEVELOPMENT"},
        ]

        client.create_tasks_batch("meeting-1", tasks)

        insert_data = mock_table.insert.call_args[0][0]
        assert insert_data[0]["category"] == "PRODUCT & TECHNOLOGY"
        assert insert_data[1]["category"] == "SALES & BUSINESS DEVELOPMENT"


# =============================================================================
# Supabase Client — get_tasks with category filter
# =============================================================================

class TestSupabaseGetTasksCategory:
    """Tests for category filter (+ archived exclusion) in get_tasks()."""

    def _make_chain(self):
        from services.supabase_client import SupabaseClient
        client = SupabaseClient.__new__(SupabaseClient)
        mock_internal = MagicMock()
        object.__setattr__(client, '_client', mock_internal)

        # Self-referential chain: every filter call returns the same mock so a
        # sequence of .eq(...).neq(...).eq(...) works and every call anywhere
        # in the chain is observable via mock.<method>.call_args_list.
        mock_chain = MagicMock()
        mock_chain.eq.return_value = mock_chain
        mock_chain.neq.return_value = mock_chain  # 2026-06: archived exclusion
        mock_chain.in_.return_value = mock_chain
        mock_chain.is_.return_value = mock_chain  # v2.5: valid_to IS NULL
        mock_chain.ilike.return_value = mock_chain
        mock_chain.order.return_value = mock_chain
        mock_chain.limit.return_value = mock_chain
        mock_chain.execute.return_value = MagicMock(data=[])

        mock_internal.table.return_value = mock_chain
        mock_chain.select.return_value = mock_chain
        return client, mock_chain

    def test_get_tasks_filters_by_category(self):
        """get_tasks() should add .eq('category', ...) when category is provided."""
        client, mock_chain = self._make_chain()

        client.get_tasks(category="PRODUCT & TECHNOLOGY")

        # Verify .eq was called with category somewhere in the chain
        # (post-T3.1 there's also an approval_status filter before it).
        mock_chain.eq.assert_any_call("category", "PRODUCT & TECHNOLOGY")
        mock_chain.eq.assert_any_call("approval_status", "approved")

    def test_get_tasks_excludes_archived_by_default(self):
        """With no status filter, archived tasks (sanctioned removals) are
        excluded unless include_archived=True."""
        client, mock_chain = self._make_chain()
        client.get_tasks()
        mock_chain.neq.assert_any_call("status", "archived")

    def test_get_tasks_include_archived_skips_exclusion(self):
        client, mock_chain = self._make_chain()
        client.get_tasks(include_archived=True)
        mock_chain.neq.assert_not_called()


# =============================================================================
# Tool Definitions
# =============================================================================

class TestToolDefinitionsCategory:
    """Tests for category in tool definitions."""

    def test_create_task_tool_has_category(self):
        """create_task tool should have a category property with the 7-value enum."""
        from core.tools import TOOL_CREATE_TASK

        props = TOOL_CREATE_TASK["input_schema"]["properties"]
        assert "category" in props
        assert props["category"]["type"] == "string"
        assert len(props["category"]["enum"]) == 7
        assert "PRODUCT & TECHNOLOGY" in props["category"]["enum"]
        assert "General" in props["category"]["enum"]

    def test_get_tasks_tool_has_category(self):
        """get_tasks tool should have a category filter property."""
        from core.tools import TOOL_GET_TASKS

        props = TOOL_GET_TASKS["input_schema"]["properties"]
        assert "category" in props
        assert props["category"]["type"] == "string"
        assert len(props["category"]["enum"]) == 7
        assert "SALES & BUSINESS DEVELOPMENT" in props["category"]["enum"]


# =============================================================================
# Google Sheets — Task Tracker Columns
# =============================================================================

class TestSheetsTaskTrackerColumns:
    """Tests for category in Google Sheets Task Tracker."""

    def test_columns_include_category(self):
        """TASK_TRACKER_HEADERS should include Category."""
        from services.google_sheets import TASK_TRACKER_HEADERS

        assert "Category" in TASK_TRACKER_HEADERS
        # Phase 10 layout: Priority, Label, Task, Owner, Deadline, Status, Category, Source Meeting, Created
        assert TASK_TRACKER_HEADERS[0] == "Priority"
        assert TASK_TRACKER_HEADERS[1] == "Label"
        assert TASK_TRACKER_HEADERS[2] == "Task"

    def test_columns_has_ten_entries_with_id(self):
        """v3 reconcile appended the UUID 'ID' column at the end (10 total).
        The urgency flag appends only 'Urgency'/K after that — no Area/L."""
        from services.google_sheets import TASK_TRACKER_HEADERS, TASK_COLUMNS
        from config.settings import settings

        base_headers = [h for h in TASK_TRACKER_HEADERS if h != "Urgency"]
        assert len(base_headers) == 10
        assert base_headers[9] == "ID"
        assert TASK_COLUMNS["id"] == "J"  # appended; A-I positions unchanged
        assert "Area" not in TASK_TRACKER_HEADERS
        assert "area" not in TASK_COLUMNS
        if getattr(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
            assert TASK_COLUMNS["urgency"] == "K"

    def test_task_categories_constant_is_canonical(self):
        """TASK_CATEGORIES (column-G validation) = the 6 Gantt areas + General."""
        from services.google_sheets import TASK_CATEGORIES

        assert TASK_CATEGORIES == [
            "PRODUCT & TECHNOLOGY",
            "SALES & BUSINESS DEVELOPMENT",
            "FUNDRAISING & INVESTOR RELATIONS",
            "LEGAL, CORPORATE & FINANCE",
            "CLIENT DELIVERY & OPERATIONS",
            "TEAM & HUMAN RESOURCES",
            "General",
        ]

    def test_task_statuses_include_archived(self):
        from services.google_sheets import TASK_STATUSES

        assert "archived" in TASK_STATUSES


# =============================================================================
# Extraction Prompt
# =============================================================================

class TestExtractionPromptCategory:
    """Tests for the category rule in the extraction prompt build.

    The CATEGORY rule is built at runtime inside extract_structured_data from
    the live areas table (get_areas), with the TaskCategory mirror as fallback,
    so we verify the prompt-construction source (same style as the schema test
    below — the function itself calls the LLM).
    """

    def test_extraction_prompt_builds_category_rule_from_areas(self):
        import inspect
        from processors.transcript_processor import extract_structured_data

        source = inspect.getsource(extract_structured_data)
        # rule built from the live area names, always present (not flag-gated)
        assert "CATEGORY: assign exactly ONE of these CropSight Gantt areas" in source
        assert "get_areas" in source
        # the JSON schema's category line is the dynamic option list
        assert '"category": "{category_options}"' in source
        # static mirror is the fallback when the areas table is unreachable
        assert "TaskCategory" in source
        # the per-task area field is gone from the extraction JSON
        assert '"area"' not in source


# =============================================================================
# Transcript Processor — Extraction Schema
# =============================================================================

class TestTranscriptProcessorCategory:
    """Tests for category in transcript processor extraction schema."""

    def test_extraction_schema_includes_category(self):
        """The JSON extraction schema should include category field in tasks."""
        import inspect
        from processors.transcript_processor import extract_structured_data

        source = inspect.getsource(extract_structured_data)
        assert "category" in source


# =============================================================================
# Summary Template
# =============================================================================

class TestSummaryTemplateCategory:
    """Tests for category column in summary template."""

    def test_summary_template_has_category_column(self):
        """SUMMARY_TEMPLATE should have Category in task table header."""
        from core.system_prompt import SUMMARY_TEMPLATE

        assert "| Category |" in SUMMARY_TEMPLATE


# =============================================================================
# Weekly Digest Format
# =============================================================================

class TestWeeklyDigestCategory:
    """Tests for category columns in weekly digest."""

    def test_completed_tasks_table_has_category(self):
        """Completed tasks section should include Category column."""
        from processors.weekly_digest import format_digest_document

        doc = format_digest_document(
            week_of="2026-02-17",
            meetings=[],
            decisions=[],
            tasks_completed=[{
                "title": "Build API",
                "category": "PRODUCT & TECHNOLOGY",
                "assignee": "Roye",
            }],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "| Category |" in doc
        assert "PRODUCT & TECHNOLOGY" in doc

    def test_overdue_tasks_table_has_category(self):
        """Overdue tasks section should include Category column."""
        from processors.weekly_digest import format_digest_document

        doc = format_digest_document(
            week_of="2026-02-17",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[{
                "title": "File papers",
                "category": "LEGAL, CORPORATE & FINANCE",
                "assignee": "Eyal",
                "deadline": "2026-02-10",
            }],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "LEGAL, CORPORATE & FINANCE" in doc

    def test_upcoming_tasks_table_has_category(self):
        """Upcoming tasks section should include Category column."""
        from processors.weekly_digest import format_digest_document

        doc = format_digest_document(
            week_of="2026-02-17",
            meetings=[],
            decisions=[],
            tasks_completed=[],
            tasks_overdue=[],
            tasks_upcoming=[{
                "title": "Prepare pitch",
                "category": "FUNDRAISING & INVESTOR RELATIONS",
                "assignee": "Eyal",
                "deadline": "2026-02-28",
                "priority": "H",
            }],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "FUNDRAISING & INVESTOR RELATIONS" in doc
