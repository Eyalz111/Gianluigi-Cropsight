"""
Tests for task category system (Change 2).

Tests:
- TaskCategory enum values
- Task model accepts category field
- supabase_client.create_task() passes category
- supabase_client.create_tasks_batch() passes category
- supabase_client.get_tasks() filters by category
- Tool definitions include category enum
- Agent passes category in tool calls
- Google Sheets add_task() includes category column
- Google Sheets add_tasks_batch() includes category column
- Google Sheets add_follow_ups_as_tasks() includes category column
- Google Sheets get_all_tasks() returns category
- Extraction prompt mentions categories
- Transcript processor extraction schema includes category
- Weekly digest format includes category columns
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from models.schemas import TaskCategory, Task


# =============================================================================
# TaskCategory Enum
# =============================================================================

class TestTaskCategoryEnum:
    """Tests for the TaskCategory enum."""

    def test_has_six_categories(self):
        """Should have exactly 6 categories."""
        assert len(TaskCategory) == 6

    def test_product_tech(self):
        assert TaskCategory.PRODUCT_TECH == "Product & Tech"

    def test_bd_sales(self):
        assert TaskCategory.BD_SALES == "BD & Sales"

    def test_legal_compliance(self):
        assert TaskCategory.LEGAL_COMPLIANCE == "Legal & Compliance"

    def test_finance_fundraising(self):
        assert TaskCategory.FINANCE_FUNDRAISING == "Finance & Fundraising"

    def test_operations_hr(self):
        assert TaskCategory.OPERATIONS_HR == "Operations & HR"

    def test_strategy_research(self):
        assert TaskCategory.STRATEGY_RESEARCH == "Strategy & Research"


# =============================================================================
# Task Model
# =============================================================================

class TestTaskModelCategory:
    """Tests for category field on Task model."""

    def test_task_accepts_category(self):
        """Task model should accept a category field."""
        task = Task(
            title="Build dashboard",
            assignee="Roye",
            category=TaskCategory.PRODUCT_TECH,
        )
        assert task.category == TaskCategory.PRODUCT_TECH

    def test_task_category_defaults_to_none(self):
        """Category should default to None when not provided."""
        task = Task(title="Generic task", assignee="Team")
        assert task.category is None

    def test_task_accepts_string_category(self):
        """Category field should accept matching string values."""
        task = Task(
            title="File patent",
            assignee="Eyal",
            category="Legal & Compliance",
        )
        assert task.category == TaskCategory.LEGAL_COMPLIANCE


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
            data=[{"id": "task-1", "title": "Test", "category": "Product & Tech"}]
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
            category="Product & Tech",
        )

        insert_data = tasks_table.insert.call_args[0][0]
        assert insert_data["category"] == "Product & Tech"

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
            {"id": "t1", "category": "Product & Tech"},
            {"id": "t2", "category": "BD & Sales"},
        ])

        tasks = [
            {"title": "Build API", "assignee": "Roye", "category": "Product & Tech"},
            {"title": "Call investor", "assignee": "Eyal", "category": "BD & Sales"},
        ]

        client.create_tasks_batch("meeting-1", tasks)

        insert_data = mock_table.insert.call_args[0][0]
        assert insert_data[0]["category"] == "Product & Tech"
        assert insert_data[1]["category"] == "BD & Sales"


# =============================================================================
# Supabase Client — get_tasks with category filter
# =============================================================================

class TestSupabaseGetTasksCategory:
    """Tests for category filter in get_tasks()."""

    def test_get_tasks_filters_by_category(self):
        """get_tasks() should add .eq('category', ...) when category is provided."""
        from services.supabase_client import SupabaseClient
        client = SupabaseClient.__new__(SupabaseClient)
        mock_internal = MagicMock()
        object.__setattr__(client, '_client', mock_internal)

        mock_table = MagicMock()
        mock_internal.table.return_value = mock_table
        mock_select = MagicMock()
        mock_table.select.return_value = mock_select

        # Chain: select().eq().order().limit().execute()
        mock_eq = MagicMock()
        mock_select.eq.return_value = mock_eq
        mock_order = MagicMock()
        mock_eq.order.return_value = mock_order
        mock_limit = MagicMock()
        mock_order.limit.return_value = mock_limit
        mock_limit.execute.return_value = MagicMock(data=[])

        client.get_tasks(category="Product & Tech")

        # Verify .eq was called with category
        mock_select.eq.assert_called_with("category", "Product & Tech")


# =============================================================================
# Tool Definitions
# =============================================================================

class TestToolDefinitionsCategory:
    """Tests for category in tool definitions."""

    def test_create_task_tool_has_category(self):
        """create_task tool should have a category property with enum."""
        from core.tools import TOOL_CREATE_TASK

        props = TOOL_CREATE_TASK["input_schema"]["properties"]
        assert "category" in props
        assert props["category"]["type"] == "string"
        assert len(props["category"]["enum"]) == 6
        assert "Product & Tech" in props["category"]["enum"]

    def test_get_tasks_tool_has_category(self):
        """get_tasks tool should have a category filter property."""
        from core.tools import TOOL_GET_TASKS

        props = TOOL_GET_TASKS["input_schema"]["properties"]
        assert "category" in props
        assert props["category"]["type"] == "string"
        assert len(props["category"]["enum"]) == 6


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

    def test_columns_has_nine_entries(self):
        """Should now have 9 columns."""
        from services.google_sheets import TASK_TRACKER_HEADERS

        assert len(TASK_TRACKER_HEADERS) == 9


# =============================================================================
# Extraction Prompt
# =============================================================================

class TestExtractionPromptCategory:
    """Tests for category in extraction prompt."""

    def test_extraction_prompt_mentions_categories(self):
        """Extraction prompt should list task categories."""
        from core.system_prompt import get_summary_extraction_prompt

        prompt = get_summary_extraction_prompt(
            transcript="Test transcript",
            meeting_title="Test",
            meeting_date="2026-02-26",
            participants=["Eyal"],
        )

        assert "Product & Tech" in prompt
        assert "BD & Sales" in prompt
        assert "Legal & Compliance" in prompt
        assert "Finance & Fundraising" in prompt
        assert "Operations & HR" in prompt
        assert "Strategy & Research" in prompt


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
                "category": "Product & Tech",
                "assignee": "Roye",
            }],
            tasks_overdue=[],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "| Category |" in doc
        assert "Product & Tech" in doc

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
                "category": "Legal & Compliance",
                "assignee": "Eyal",
                "deadline": "2026-02-10",
            }],
            tasks_upcoming=[],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "Legal & Compliance" in doc

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
                "category": "Finance & Fundraising",
                "assignee": "Eyal",
                "deadline": "2026-02-28",
                "priority": "H",
            }],
            open_questions=[],
            upcoming_meetings=[],
        )

        assert "Finance & Fundraising" in doc
