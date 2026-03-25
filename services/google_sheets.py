"""
Google Sheets API integration.

This module handles reading and writing to Google Sheets:
- Task Tracker: Writing extracted tasks, updating status
- Stakeholder Tracker: Reading stakeholder information for context

Sheets:
1. CropSight Task Tracker (NEW - created by Gianluigi)
   Columns: Task, Assignee, Source Meeting, Deadline, Status, Priority, Created Date, Updated Date

2. CropSight Stakeholder Tracker (EXISTING - Eyal's sheet)
   Columns: Organization/Name, Type, Short Description, Contact Person + Email,
            Desired Outcome, Priority, Primary Action Type, Owner, Next Action,
            Due Date, Secondary Action Type, Owner, Next Action, Due Date, Status, Notes

Usage:
    from services.google_sheets import sheets_service

    # Add a task to the tracker
    await sheets_service.add_task(task_data)

    # Look up stakeholder info
    info = await sheets_service.get_stakeholder_info(name="Rita")
"""

import logging
from datetime import datetime
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config.settings import settings

logger = logging.getLogger(__name__)


# =========================================================================
# Color Constants — RGB dicts for Google Sheets API (0.0–1.0 scale)
# =========================================================================

def _hex_color(hex_str: str) -> dict:
    """Convert '#RRGGBB' to Sheets API color dict."""
    return {
        "red": int(hex_str[1:3], 16) / 255,
        "green": int(hex_str[3:5], 16) / 255,
        "blue": int(hex_str[5:7], 16) / 255,
    }


COLORS = {
    # Category colors (Task Tracker — column G in Phase 10 layout)
    "product_tech": _hex_color("#BBDEFB"),       # Light Blue
    "business_dev": _hex_color("#C8E6C9"),        # Light Green
    "marketing": _hex_color("#FFE0B2"),           # Light Orange
    "finance": _hex_color("#E1BEE7"),             # Light Purple
    "legal_ip": _hex_color("#FFCDD2"),            # Light Red
    "operations_hr": _hex_color("#B2DFDB"),       # Light Teal

    # Priority colors
    "priority_high": _hex_color("#FFCDD2"),       # Light Red
    "priority_medium": _hex_color("#FFF9C4"),     # Light Yellow
    "priority_low": _hex_color("#C8E6C9"),        # Light Green

    # Status colors (shared)
    "status_overdue": _hex_color("#FFCDD2"),      # Light Red
    "status_done": _hex_color("#C8E6C9"),         # Light Green
    "status_in_progress": _hex_color("#FFF9C4"),  # Light Yellow
    "status_pending": _hex_color("#BBDEFB"),      # Light Blue

    # Stakeholder status colors
    "status_new": _hex_color("#BBDEFB"),          # Light Blue
    "status_active": _hex_color("#C8E6C9"),       # Light Green
    "status_inactive": _hex_color("#E0E0E0"),     # Light Gray
    "status_completed": _hex_color("#A5D6A7"),    # Medium Green

    # Banding / borders
    "banding_even": _hex_color("#F5F5F5"),        # Very Light Gray
    "banding_odd": _hex_color("#FFFFFF"),          # White
    "border_gray": _hex_color("#E0E0E0"),         # Light Gray

    # Header
    "header_bg": _hex_color("#1A237E"),           # Dark Blue
    "header_text": _hex_color("#FFFFFF"),         # White
}

# Category labels for data validation
TASK_CATEGORIES = [
    "Product & Tech",
    "BD & Sales",
    "Strategy & Research",
    "Finance & Fundraising",
    "Legal & Compliance",
    "Operations & HR",
]

# Status labels for data validation
TASK_STATUSES = ["pending", "in_progress", "done", "overdue"]
STAKEHOLDER_STATUSES = ["New", "Active", "Inactive", "Completed"]

# Priority labels for data validation
PRIORITIES = ["H", "M", "L"]


# =========================================================================
# Formatting Helper Functions
# =========================================================================

def _conditional_format_rule(
    sheet_id: int, col_index: int, text: str, color: dict, rule_index: int = 0
) -> dict:
    """Build one addConditionalFormatRule request for TEXT_CONTAINS."""
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                    "startRowIndex": 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_CONTAINS",
                        "values": [{"userEnteredValue": text}],
                    },
                    "format": {"backgroundColor": color},
                },
            },
            "index": rule_index,
        }
    }


def _column_width_request(sheet_id: int, col_index: int, width_px: int) -> dict:
    """Build one updateDimensionProperties request for a fixed column width."""
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_index,
                "endIndex": col_index + 1,
            },
            "properties": {"pixelSize": width_px},
            "fields": "pixelSize",
        }
    }


def _banding_request(sheet_id: int, num_cols: int) -> dict:
    """Build an addBanding request for alternating row colors (skip header)."""
    return {
        "addBanding": {
            "bandedRange": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "rowProperties": {
                    "firstBandColor": COLORS["banding_odd"],
                    "secondBandColor": COLORS["banding_even"],
                },
            }
        }
    }


def _border_request(sheet_id: int, num_cols: int) -> dict:
    """Build an updateBorders request with light gray borders on all cells."""
    border_style = {
        "style": "SOLID",
        "color": COLORS["border_gray"],
    }
    return {
        "updateBorders": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "top": border_style,
            "bottom": border_style,
            "left": border_style,
            "right": border_style,
            "innerHorizontal": border_style,
            "innerVertical": border_style,
        }
    }


def _data_validation_request(
    sheet_id: int, col_index: int, values: list[str]
) -> dict:
    """Build a setDataValidation request with a dropdown (ONE_OF_LIST)."""
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col_index,
                "endColumnIndex": col_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in values],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    }


def _text_wrap_request(sheet_id: int, col_index: int) -> dict:
    """Build a repeatCell request that sets wrapStrategy: WRAP on a column."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col_index,
                "endColumnIndex": col_index + 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "wrapStrategy": "WRAP",
                }
            },
            "fields": "userEnteredFormat.wrapStrategy",
        }
    }


# =========================================================================
# Column Mapping Constants — single source of truth for column layout
# =========================================================================

# Task Tracker: new Phase 10 layout (reordered for quick scanning)
TASK_COLUMNS = {
    "priority": "A",       # H/M/L — narrow, color-coded
    "label": "B",          # 2-3 word project label — quick-scan column
    "task": "C",           # Full task description
    "owner": "D",          # Assignee
    "deadline": "E",       # Due date
    "status": "F",         # pending/in_progress/done/overdue
    "category": "G",       # Product & Tech, BD & Sales, etc.
    "source_meeting": "H", # Meeting where task originated
    "created": "I",        # Creation date
}

# Column indices (0-based) for formatting operations
TASK_COL_INDEX = {k: ord(v) - ord("A") for k, v in TASK_COLUMNS.items()}

# Decision Tracker columns
DECISION_COLUMNS = {
    "label": "A",
    "decision": "B",
    "rationale": "C",
    "confidence": "D",
    "source_meeting": "E",
    "date": "F",
    "status": "G",
}

DECISION_COL_INDEX = {k: ord(v) - ord("A") for k, v in DECISION_COLUMNS.items()}

# Header labels for sheet display (order matches column mapping)
TASK_TRACKER_HEADERS = [
    "Priority", "Label", "Task", "Owner", "Deadline",
    "Status", "Category", "Source Meeting", "Created",
]

DECISION_TRACKER_HEADERS = [
    "Label", "Decision", "Rationale", "Confidence",
    "Source Meeting", "Date", "Status",
]

# Legacy constant — kept for backward compatibility during transition
TASK_TRACKER_COLUMNS = TASK_TRACKER_HEADERS

# Stakeholder Tracker column configuration (Eyal's existing sheet)
STAKEHOLDER_COLUMNS = [
    "Organization/Name",
    "Type",
    "Short Description",
    "Contact Person + Email",
    "Desired Outcome",
    "Priority",
    "Primary Action Type",
    "Owner",
    "Next Action",
    "Due Date",
    "Secondary Action Type",
    "Secondary Owner",
    "Secondary Next Action",
    "Secondary Due Date",
    "Status",
    "Notes",
]


class GoogleSheetsService:
    """
    Service for Google Sheets API operations.

    Handles both reading (stakeholder tracker) and writing (task tracker).
    """

    def __init__(self):
        """
        Initialize the Google Sheets service with credentials.
        """
        self._service = None
        self._credentials: Credentials | None = None

    @property
    def service(self):
        """
        Lazy initialization of Sheets API service.

        Uses OAuth2 credentials from settings.
        """
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        """Build the Google Sheets API service with OAuth2 credentials."""
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google OAuth credentials not configured")

        if not settings.GOOGLE_REFRESH_TOKEN:
            raise RuntimeError(
                "Google refresh token not configured. "
                "Run the OAuth flow to obtain a refresh token."
            )

        # Create credentials from refresh token
        self._credentials = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
            ],
        )

        # Refresh the token if needed
        if self._credentials.expired or not self._credentials.token:
            self._credentials.refresh(Request())

        return build("sheets", "v4", credentials=self._credentials)

    async def authenticate(self) -> bool:
        """
        Authenticate with Google Sheets API using OAuth2.

        Returns:
            True if authentication successful, False otherwise.
        """
        try:
            # Force service initialization to verify auth
            _ = self.service
            logger.info("Google Sheets API authentication successful")
            return True
        except Exception as e:
            logger.error(f"Google Sheets API authentication failed: {e}")
            return False

    # =========================================================================
    # Task Tracker Operations
    # =========================================================================

    async def add_task(
        self,
        task: str,
        assignee: str,
        source_meeting: str,
        deadline: str | None,
        status: str,
        priority: str,
        created_date: str,
        category: str = "",
        label: str = "",
    ) -> bool:
        """
        Add a new task to the Task Tracker sheet.

        Column order follows TASK_COLUMNS: Priority, Label, Task, Owner,
        Deadline, Status, Category, Source Meeting, Created.

        Args:
            task: Task description.
            assignee: Who is responsible.
            source_meeting: Name of the meeting where task was created.
            deadline: Due date (YYYY-MM-DD) or None.
            status: 'pending', 'in_progress', 'done', 'overdue'.
            priority: 'H', 'M', or 'L'.
            created_date: When the task was created (YYYY-MM-DD).
            category: Task category (e.g., 'Product & Tech').
            label: Project label (e.g., 'Moldova Pilot').

        Returns:
            True if task was added successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        values = [
            priority,
            label,
            task,
            assignee,
            deadline or "",
            status,
            category,
            source_meeting,
            created_date,
        ]

        return await self._append_row(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values,
            tab_name=settings.TASK_TRACKER_TAB_NAME,
        )

    async def update_task_status(
        self,
        row_number: int,
        status: str,
        updated_date: str | None = None,
    ) -> bool:
        """
        Update a task's status in the Task Tracker.

        Args:
            row_number: The row number of the task (1-indexed, header is row 1).
            status: New status value.
            updated_date: Ignored (kept for backward compat). No Updated column in new layout.

        Returns:
            True if update was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        try:
            col = TASK_COLUMNS["status"]
            range_name = f"'{settings.TASK_TRACKER_TAB_NAME}'!{col}{row_number}"
            self.service.spreadsheets().values().update(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range=range_name,
                valueInputOption="RAW",
                body={"values": [[status]]}
            ).execute()

            logger.info(f"Updated task row {row_number}: status={status}")
            return True

        except Exception as e:
            logger.error(f"Error updating task status: {e}")
            return False

    async def find_task_row(self, title: str) -> int | None:
        """
        Find the row number of a task in the Task Tracker by title.

        Args:
            title: Task title to search for (case-insensitive partial match).

        Returns:
            Row number if found, None otherwise.
        """
        all_tasks = await self.get_all_tasks()
        title_lower = title.lower()
        for task in all_tasks:
            if title_lower in task.get("task", "").lower():
                return task["row_number"]
        return None

    async def update_task_row(
        self,
        row_number: int,
        **fields,
    ) -> bool:
        """
        Update specific fields of a task row in the Task Tracker.

        Uses TASK_COLUMNS for column mapping.

        Args:
            row_number: The row number to update (1-indexed, header is row 1).
            **fields: Field names to update. Supported: task, label, category,
                      owner/assignee, deadline, status, priority.

        Returns:
            True if update was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False

        # Map field names to TASK_COLUMNS keys (with aliases)
        field_aliases = {"assignee": "owner"}
        column_map = {
            "task": TASK_COLUMNS["task"],
            "label": TASK_COLUMNS["label"],
            "category": TASK_COLUMNS["category"],
            "owner": TASK_COLUMNS["owner"],
            "assignee": TASK_COLUMNS["owner"],
            "deadline": TASK_COLUMNS["deadline"],
            "status": TASK_COLUMNS["status"],
            "priority": TASK_COLUMNS["priority"],
        }

        try:
            batch_data = []

            for field, value in fields.items():
                col = column_map.get(field)
                if col:
                    batch_data.append({
                        "range": f"'{settings.TASK_TRACKER_TAB_NAME}'!{col}{row_number}",
                        "values": [[value if value is not None else ""]],
                    })

            if batch_data:
                self.service.spreadsheets().values().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={
                        "valueInputOption": "RAW",
                        "data": batch_data,
                    },
                ).execute()
                logger.info(f"Updated task row {row_number}: {list(fields.keys())}")
                return True
            return False

        except Exception as e:
            logger.error(f"Error updating task row {row_number}: {e}")
            return False

    async def get_all_tasks(self) -> list[dict]:
        """
        Get all tasks from the Task Tracker.

        Returns:
            List of task dicts with all columns (uses TASK_COLUMNS mapping).
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return []

        rows = await self._read_sheet_range(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            range_name="A:I"
        )

        if not rows or len(rows) < 2:
            return []

        num_cols = len(TASK_COLUMNS)
        # Skip header row
        tasks = []
        for i, row in enumerate(rows[1:], start=2):
            # Pad row if needed
            while len(row) < num_cols:
                row.append("")

            tasks.append({
                "row_number": i,
                "priority": row[TASK_COL_INDEX["priority"]],
                "label": row[TASK_COL_INDEX["label"]],
                "task": row[TASK_COL_INDEX["task"]],
                "assignee": row[TASK_COL_INDEX["owner"]],
                "source_meeting": row[TASK_COL_INDEX["source_meeting"]],
                "deadline": row[TASK_COL_INDEX["deadline"]],
                "status": row[TASK_COL_INDEX["status"]],
                "category": row[TASK_COL_INDEX["category"]],
                "created_date": row[TASK_COL_INDEX["created"]],
            })

        return tasks

    async def archive_completed_tasks(self, titles_to_archive: list[str]) -> int:
        """
        Move completed tasks from active tab to Archive tab.

        Args:
            titles_to_archive: List of task titles to archive.

        Returns:
            Number of tasks archived.
        """
        if not settings.TASK_TRACKER_SHEET_ID or not titles_to_archive:
            return 0

        try:
            # Ensure Archive tab exists
            tab_name = settings.TASK_TRACKER_TAB_NAME
            meta = self.service.spreadsheets().get(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                fields="sheets.properties",
            ).execute()

            archive_exists = any(
                s.get("properties", {}).get("title") == "Archive"
                for s in meta.get("sheets", [])
            )
            if not archive_exists:
                self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": [{"addSheet": {"properties": {"title": "Archive"}}}]},
                ).execute()
                # Add header row (matches TASK_TRACKER_HEADERS + Archived)
                archive_headers = TASK_TRACKER_HEADERS + ["Archived"]
                self.service.spreadsheets().values().update(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    range=f"Archive!A1:{chr(ord('A') + len(archive_headers) - 1)}1",
                    valueInputOption="RAW",
                    body={"values": [archive_headers]},
                ).execute()

            # Find matching rows in active tab
            all_tasks = await self.get_all_tasks()
            titles_lower = {t.lower() for t in titles_to_archive}
            rows_to_archive = [
                t for t in all_tasks
                if t.get("task", "").lower() in titles_lower and t.get("status", "").lower() == "done"
            ]

            if not rows_to_archive:
                return 0

            # Copy rows to Archive tab (matches TASK_TRACKER_HEADERS + Archived)
            archive_rows = []
            for t in rows_to_archive:
                archive_rows.append([
                    t.get("priority", ""), t.get("label", ""), t.get("task", ""),
                    t.get("assignee", ""), t.get("deadline", ""), t.get("status", ""),
                    t.get("category", ""), t.get("source_meeting", ""),
                    t.get("created_date", ""),
                    datetime.now().strftime("%Y-%m-%d"),
                ])

            num_archive_cols = len(TASK_TRACKER_HEADERS) + 1  # +1 for Archived
            self.service.spreadsheets().values().append(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range=f"Archive!A:{chr(ord('A') + num_archive_cols - 1)}",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": archive_rows},
            ).execute()

            # Delete archived rows from active tab (bottom-up to preserve row numbers)
            row_numbers = sorted([t["row_number"] for t in rows_to_archive], reverse=True)
            sid = self._get_sheet_id_by_name(
                settings.TASK_TRACKER_SHEET_ID, tab_name
            )
            if sid is not None:
                requests = [
                    {"deleteDimension": {
                        "range": {"sheetId": sid, "dimension": "ROWS",
                                  "startIndex": r - 1, "endIndex": r}
                    }}
                    for r in row_numbers
                ]
                self.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": requests},
                ).execute()

            logger.info(f"Archived {len(rows_to_archive)} completed tasks")
            return len(rows_to_archive)

        except Exception as e:
            logger.error(f"Error archiving tasks: {e}")
            return 0

    async def rebuild_tasks_sheet(self, tasks_from_db: list[dict]) -> bool:
        """
        Rebuild the Tasks sheet from Supabase data.

        Clears the sheet completely and writes all tasks with consistent
        formatting. Tasks are sorted by: Status (pending→in_progress→overdue→done)
        → Priority (H→M→L) → Created date (newest first).

        Args:
            tasks_from_db: List of task dicts from Supabase.

        Returns:
            True if rebuild was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False

        try:
            tab_name = settings.TASK_TRACKER_TAB_NAME

            # Sort tasks: active statuses first, then by priority, then newest
            status_order = {"pending": 0, "in_progress": 1, "overdue": 2, "done": 3}
            priority_order = {"H": 0, "M": 1, "L": 2}

            def sort_key(t):
                return (
                    status_order.get(t.get("status", "pending"), 9),
                    priority_order.get(t.get("priority", "M"), 9),
                    t.get("created_at", ""),  # string sort, newest last
                )

            sorted_tasks = sorted(tasks_from_db, key=sort_key)

            # Build rows in TASK_COLUMNS order
            rows = [TASK_TRACKER_HEADERS]  # header first
            for t in sorted_tasks:
                created = str(t.get("created_at", ""))[:10]
                # source_meeting comes from the joined meetings object or direct field
                meeting_info = t.get("meetings") or {}
                source = (
                    t.get("source_meeting", "")
                    or (meeting_info.get("title", "") if isinstance(meeting_info, dict) else "")
                )
                rows.append([
                    t.get("priority", "M"),
                    t.get("label", ""),
                    t.get("title", ""),
                    t.get("assignee", ""),
                    str(t.get("deadline", "") or ""),
                    t.get("status", "pending"),
                    t.get("category", ""),
                    source,
                    created,
                ])

            # Clear the tab and write fresh data
            num_cols = len(TASK_COLUMNS)
            end_col = chr(ord("A") + num_cols - 1)
            clear_range = f"'{tab_name}'!A:{end_col}"

            self.service.spreadsheets().values().clear(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range=clear_range,
            ).execute()

            # Write all data (header + tasks)
            write_range = f"'{tab_name}'!A1:{end_col}{len(rows)}"
            self.service.spreadsheets().values().update(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range=write_range,
                valueInputOption="RAW",
                body={"values": rows},
            ).execute()

            logger.info(f"Rebuilt Tasks sheet: {len(sorted_tasks)} tasks written")

            # Apply formatting
            await self.format_task_tracker()

            return True

        except Exception as e:
            logger.error(f"Error rebuilding Tasks sheet: {e}")
            return False

    async def rebuild_decisions_sheet(self, decisions_from_db: list[dict]) -> bool:
        """
        Rebuild the Decisions sheet from Supabase data.

        Args:
            decisions_from_db: List of decision dicts from Supabase.

        Returns:
            True if rebuild was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False

        try:
            # Ensure tab exists
            sheet_id = await self.ensure_decisions_tab()
            if sheet_id is None:
                return False

            # Sort decisions by date (newest first)
            sorted_decisions = sorted(
                decisions_from_db,
                key=lambda d: d.get("created_at", ""),
                reverse=True,
            )

            # Build rows
            rows = [DECISION_TRACKER_HEADERS]
            for d in sorted_decisions:
                meeting_info = d.get("meetings") or {}
                source = (
                    d.get("source_meeting", "")
                    or (meeting_info.get("title", "") if isinstance(meeting_info, dict) else "")
                )
                rows.append([
                    d.get("label", ""),
                    d.get("description", ""),
                    d.get("rationale", ""),
                    str(d.get("confidence", 3)),
                    source,
                    str(d.get("created_at", ""))[:10],
                    d.get("decision_status", "Active"),
                ])

            # Clear and rewrite
            num_cols = len(DECISION_COLUMNS)
            end_col = chr(ord("A") + num_cols - 1)

            self.service.spreadsheets().values().clear(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range=f"Decisions!A:{end_col}",
            ).execute()

            self.service.spreadsheets().values().update(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range=f"Decisions!A1:{end_col}{len(rows)}",
                valueInputOption="RAW",
                body={"values": rows},
            ).execute()

            logger.info(f"Rebuilt Decisions sheet: {len(sorted_decisions)} decisions written")
            return True

        except Exception as e:
            logger.error(f"Error rebuilding Decisions sheet: {e}")
            return False

    async def find_task_row(self, task_description: str) -> int | None:
        """
        Find the row number for a task by its description.

        Args:
            task_description: The task text to search for.

        Returns:
            Row number if found, None otherwise.
        """
        tasks = await self.get_all_tasks()

        for task in tasks:
            if task["task"].lower() == task_description.lower():
                return task["row_number"]

        return None

    # =========================================================================
    # Stakeholder Tracker Operations
    # =========================================================================

    async def get_stakeholder_info(
        self,
        name: str | None = None,
        organization: str | None = None
    ) -> list[dict]:
        """
        Search the Stakeholder Tracker for matching entries.

        Args:
            name: Filter by contact/organization name (partial match).
            organization: Filter by organization name (partial match).

        Returns:
            List of matching stakeholder records.
        """
        all_stakeholders = await self.get_all_stakeholders()

        if not name and not organization:
            return all_stakeholders

        # Filter by name/organization
        filtered = []
        for s in all_stakeholders:
            if name:
                # Check organization name and contact person
                name_lower = name.lower()
                if (
                    name_lower in s.get("organization_name", "").lower()
                    or name_lower in s.get("contact_person", "").lower()
                ):
                    filtered.append(s)
                    continue

            if organization:
                org_lower = organization.lower()
                if org_lower in s.get("organization_name", "").lower():
                    filtered.append(s)
                    continue

        return filtered

    async def get_all_stakeholders(self) -> list[dict]:
        """
        Get all stakeholders from the Stakeholder Tracker.

        Returns:
            List of all stakeholder records.
        """
        if not settings.STAKEHOLDER_TRACKER_SHEET_ID:
            logger.warning("STAKEHOLDER_TRACKER_SHEET_ID not configured")
            return []

        tab = settings.STAKEHOLDER_TAB_NAME
        rows = await self._read_sheet_range(
            sheet_id=settings.STAKEHOLDER_TRACKER_SHEET_ID,
            range_name=f"'{tab}'!A:P"  # 16 columns
        )

        if not rows or len(rows) < 2:
            return []

        # Skip header row
        stakeholders = []
        for i, row in enumerate(rows[1:], start=2):
            # Pad row if needed
            while len(row) < len(STAKEHOLDER_COLUMNS):
                row.append("")

            stakeholders.append({
                "row_number": i,
                "organization_name": row[0],
                "type": row[1],
                "description": row[2],
                "contact_person": row[3],
                "desired_outcome": row[4],
                "priority": row[5],
                "primary_action_type": row[6],
                "owner": row[7],
                "next_action": row[8],
                "due_date": row[9],
                "secondary_action_type": row[10] if len(row) > 10 else "",
                "secondary_owner": row[11] if len(row) > 11 else "",
                "secondary_next_action": row[12] if len(row) > 12 else "",
                "secondary_due_date": row[13] if len(row) > 13 else "",
                "status": row[14] if len(row) > 14 else "",
                "notes": row[15] if len(row) > 15 else "",
            })

        return stakeholders

    async def suggest_stakeholder_update(
        self,
        organization: str,
        updates: dict
    ) -> dict:
        """
        Prepare a stakeholder update suggestion (for Eyal approval in v0.2).

        Args:
            organization: The organization to update.
            updates: Dict of field names to new values.

        Returns:
            Dict with current values and suggested updates.
        """
        # Find the stakeholder
        stakeholders = await self.get_stakeholder_info(organization=organization)

        if not stakeholders:
            return {
                "found": False,
                "organization": organization,
                "suggested_updates": updates,
            }

        current = stakeholders[0]

        return {
            "found": True,
            "organization": organization,
            "row_number": current["row_number"],
            "current_values": current,
            "suggested_updates": updates,
        }

    # =========================================================================
    # Batch Operations
    # =========================================================================

    async def add_tasks_batch(self, tasks: list[dict]) -> bool:
        """
        Add multiple tasks to the Task Tracker.

        Args:
            tasks: List of task dicts with keys matching add_task params.

        Returns:
            True if all tasks were added successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        if not tasks:
            return True

        values = []
        for task in tasks:
            created = task.get("created_date", datetime.now().strftime("%Y-%m-%d"))
            values.append([
                task.get("priority", "M"),
                task.get("label", ""),
                task.get("task", ""),
                task.get("assignee", ""),
                task.get("deadline", ""),
                task.get("status", "pending"),
                task.get("category", ""),
                task.get("source_meeting", ""),
                created,
            ])

        return await self._append_rows(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values,
            tab_name=settings.TASK_TRACKER_TAB_NAME,
        )

    async def add_follow_ups_as_tasks(
        self,
        follow_ups: list[dict],
        source_meeting: str,
        created_date: str,
    ) -> bool:
        """
        Add follow-up meetings to the Task Tracker as action items.

        Each follow-up becomes a task: "Schedule: [title]" assigned to the leader.

        Args:
            follow_ups: List of follow-up meeting dicts.
            source_meeting: Name of the source meeting.
            created_date: Date the task was created (YYYY-MM-DD).

        Returns:
            True if all follow-ups were added successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        if not follow_ups:
            return True

        values = []
        for f in follow_ups:
            title = f.get("title", "Follow-up meeting")
            led_by = f.get("led_by", "Team")
            proposed = f.get("proposed_date", "")
            prep = f.get("prep_needed", "")

            task_desc = f"Schedule: {title}"
            if proposed:
                task_desc += f" ({proposed})"
            if prep:
                task_desc += f" | Prep: {prep}"

            values.append([
                "H",               # priority
                "",                # label — follow-ups don't have a project label
                task_desc,         # task
                led_by,            # owner
                "",                # deadline — follow-ups often don't have a parseable date
                "pending",         # status
                "Operations & HR", # category
                source_meeting,    # source meeting
                created_date,      # created
            ])

        return await self._append_rows(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values,
            tab_name=settings.TASK_TRACKER_TAB_NAME,
        )

    async def add_stakeholders_batch(
        self,
        stakeholders: list[dict],
        source_meeting: str,
    ) -> bool:
        """
        Add new stakeholders to the Stakeholder Tracker sheet.

        Only adds stakeholders not already present in the sheet.

        Args:
            stakeholders: List of stakeholder dicts with 'name' and 'context' keys.
            source_meeting: Name of the meeting where they were mentioned.

        Returns:
            True if stakeholders were added successfully.
        """
        if not settings.STAKEHOLDER_TRACKER_SHEET_ID:
            logger.warning("STAKEHOLDER_TRACKER_SHEET_ID not configured")
            return False

        if not stakeholders:
            return True

        # Get existing stakeholders to avoid duplicates
        existing = await self.get_all_stakeholders()
        existing_names = {
            s.get("organization_name", "").lower() for s in existing
        }

        values = []
        for s in stakeholders:
            name = s.get("name", "")
            if not name:
                continue
            if name.lower() in existing_names:
                logger.debug(f"Stakeholder '{name}' already exists, skipping")
                continue

            context = s.get("context", "")
            values.append([
                name,                   # Organization/Name
                "",                     # Type (to be filled by Eyal)
                context,                # Short Description
                "",                     # Contact Person + Email
                "",                     # Desired Outcome
                "",                     # Priority
                "",                     # Primary Action Type
                "",                     # Owner
                "",                     # Next Action
                "",                     # Due Date
                "",                     # Secondary Action Type
                "",                     # Secondary Owner
                "",                     # Secondary Next Action
                "",                     # Secondary Due Date
                "New",                  # Status
                f"Mentioned in: {source_meeting}",  # Notes
            ])

        if not values:
            logger.info("No new stakeholders to add (all already exist)")
            return True

        try:
            tab = settings.STAKEHOLDER_TAB_NAME
            logger.info(f"Adding {len(values)} new stakeholders (filtered from {len(stakeholders)} extracted)")
            self.service.spreadsheets().values().append(
                spreadsheetId=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                range=f"'{tab}'!A:P",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values}
            ).execute()

            logger.info(f"Added {len(values)} new stakeholders to tracker")
            return True

        except Exception as e:
            logger.error(f"Error adding stakeholders: {e}")
            return False

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_first_sheet_id(self, spreadsheet_id: str) -> int:
        """
        Get the sheetId of the first tab in a spreadsheet.

        Google Sheets batchUpdate requires numeric sheetId, which is NOT
        always 0. This fetches the actual ID from the spreadsheet metadata.

        Args:
            spreadsheet_id: The Google Sheets spreadsheet ID.

        Returns:
            The numeric sheetId of the first sheet tab.
        """
        metadata = self.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.sheetId",
        ).execute()
        return metadata["sheets"][0]["properties"]["sheetId"]

    def _get_sheet_title(self, spreadsheet_id: str, sheet_id: int) -> str | None:
        """Get the title of a sheet by its numeric sheetId."""
        metadata = self.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") == sheet_id:
                return props.get("title")
        return None

    def _get_sheet_id_by_name(self, spreadsheet_id: str, tab_name: str) -> int | None:
        """
        Get the numeric sheetId of a tab by its name.

        Args:
            spreadsheet_id: The Google Sheets spreadsheet ID.
            tab_name: The name of the tab to find.

        Returns:
            The numeric sheetId, or None if the tab doesn't exist.
        """
        metadata = self.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == tab_name:
                return props.get("sheetId")
        return None

    def _clear_conditional_format_rules(self, spreadsheet_id: str) -> list[dict]:
        """
        Build deleteConditionalFormatRule requests for ALL existing rules.

        Fetches sheet metadata to find how many conditional format rules exist,
        then returns delete requests in reverse index order (so indices stay
        valid as rules are removed). This makes formatting idempotent.

        Args:
            spreadsheet_id: The Google Sheets spreadsheet ID.

        Returns:
            List of deleteConditionalFormatRule request dicts.
        """
        metadata = self.service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.conditionalFormats",
        ).execute()

        # Count existing rules on the first sheet
        sheets = metadata.get("sheets", [])
        if not sheets:
            return []

        rules = sheets[0].get("conditionalFormats", [])
        if not rules:
            return []

        sheet_id = self._get_first_sheet_id(spreadsheet_id)

        # Delete in reverse order so indices don't shift
        return [
            {
                "deleteConditionalFormatRule": {
                    "sheetId": sheet_id,
                    "index": i,
                }
            }
            for i in range(len(rules) - 1, -1, -1)
        ]

    async def _read_sheet_range(
        self,
        sheet_id: str,
        range_name: str
    ) -> list[list[Any]]:
        """
        Read a range of cells from a sheet.

        Args:
            sheet_id: Google Sheets ID.
            range_name: A1 notation range (e.g., "A1:Z100").

        Returns:
            2D list of cell values.
        """
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_name
            ).execute()

            return result.get("values", [])

        except Exception as e:
            logger.error(f"Error reading sheet range: {e}")
            return []

    async def _write_sheet_range(
        self,
        sheet_id: str,
        range_name: str,
        values: list[list[Any]]
    ) -> bool:
        """
        Write values to a range of cells.

        Args:
            sheet_id: Google Sheets ID.
            range_name: A1 notation range.
            values: 2D list of values to write.

        Returns:
            True if write was successful.
        """
        try:
            self.service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=range_name,
                valueInputOption="RAW",
                body={"values": values}
            ).execute()

            logger.info(f"Wrote {len(values)} rows to {range_name}")
            return True

        except Exception as e:
            logger.error(f"Error writing sheet range: {e}")
            return False

    async def _append_row(
        self,
        sheet_id: str,
        values: list[Any],
        tab_name: str | None = None,
    ) -> bool:
        """
        Append a row to the end of a sheet.

        Args:
            sheet_id: Google Sheets ID.
            values: List of values for the new row.
            tab_name: Optional tab name to target (e.g. "Tasks").

        Returns:
            True if append was successful.
        """
        return await self._append_rows(sheet_id, [values], tab_name=tab_name)

    async def _append_rows(
        self,
        sheet_id: str,
        values: list[list[Any]],
        tab_name: str | None = None,
    ) -> bool:
        """
        Append multiple rows to the end of a sheet.

        Args:
            sheet_id: Google Sheets ID.
            values: 2D list of values for the new rows.
            tab_name: Optional tab name to target (e.g. "Tasks").
                      When provided, uses "{tab_name}!A:I" to avoid
                      appending to the wrong tab.

        Returns:
            True if append was successful.
        """
        try:
            range_name = f"'{tab_name}'!A:I" if tab_name else "A:I"
            self.service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=range_name,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values}
            ).execute()

            logger.info(f"Appended {len(values)} rows to sheet")
            return True

        except Exception as e:
            logger.error(f"Error appending rows: {e}")
            return False

    async def _update_cell(self, sheet_id: str, range_name: str, value: str) -> None:
        """Update a single cell value."""
        self.service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()

    async def _append_row_to_range(
        self,
        sheet_id: str,
        range_name: str,
        values: list,
    ) -> None:
        """Append a row to a specific range in a sheet."""
        self.service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()

    async def apply_stakeholder_update(
        self,
        organization: str,
        updates: dict,
    ) -> bool:
        """
        Apply an approved stakeholder update to the Stakeholder Tracker.

        Args:
            organization: Organization name to update.
            updates: Dict of field names to new values.

        Returns:
            True if update was applied successfully.
        """
        if not settings.STAKEHOLDER_TRACKER_SHEET_ID:
            logger.warning("STAKEHOLDER_TRACKER_SHEET_ID not configured")
            return False

        try:
            # Get all stakeholders to find the row
            all_stakeholders = await self.get_all_stakeholders()
            target_row = None

            for s in all_stakeholders:
                if s.get("organization_name", "").lower() == organization.lower():
                    target_row = s.get("row_number")
                    break

            if target_row:
                # Update existing row
                # Map update keys to column letters
                column_map = {
                    "organization_name": "A",
                    "type": "B",
                    "description": "C",
                    "contact_person": "D",
                    "desired_outcome": "E",
                    "priority": "F",
                    "primary_action_type": "G",
                    "owner": "H",
                    "next_action": "I",
                    "due_date": "J",
                    "status": "O",
                    "notes": "P",
                }

                for field, value in updates.items():
                    col = column_map.get(field)
                    if col:
                        await self._update_cell(
                            sheet_id=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                            range_name=f"{col}{target_row}",
                            value=str(value),
                        )

                logger.info(f"Updated stakeholder: {organization} (row {target_row})")
            else:
                # Append new row
                new_row = [
                    updates.get("organization_name", organization),
                    updates.get("type", ""),
                    updates.get("description", ""),
                    updates.get("contact_person", ""),
                    updates.get("desired_outcome", ""),
                    updates.get("priority", ""),
                    updates.get("primary_action_type", ""),
                    updates.get("owner", ""),
                    updates.get("next_action", ""),
                    updates.get("due_date", ""),
                    "",  # secondary_action_type
                    "",  # secondary_owner
                    "",  # secondary_next_action
                    "",  # secondary_due_date
                    updates.get("status", "New"),
                    updates.get("notes", ""),
                ]

                tab = settings.STAKEHOLDER_TAB_NAME
                await self._append_row_to_range(
                    sheet_id=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                    range_name=f"'{tab}'!A:P",
                    values=new_row,
                )

                logger.info(f"Added new stakeholder: {organization}")

            return True

        except Exception as e:
            logger.error(f"Error applying stakeholder update: {e}")
            return False

    # =========================================================================
    # Decisions Dashboard
    # =========================================================================

    async def ensure_decisions_tab(self) -> int | None:
        """
        Ensure a 'Decisions' tab exists in the Task Tracker spreadsheet.

        Creates the tab with a header row if it doesn't exist.

        Returns:
            The sheetId of the Decisions tab, or None if no spreadsheet configured.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return None

        try:
            meta = self.service.spreadsheets().get(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                fields="sheets.properties",
            ).execute()

            for sheet in meta.get("sheets", []):
                props = sheet.get("properties", {})
                if props.get("title") == "Decisions":
                    logger.info(f"Decisions tab already exists (sheetId={props['sheetId']})")
                    return props["sheetId"]

            # Create the tab
            resp = self.service.spreadsheets().batchUpdate(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                body={
                    "requests": [
                        {"addSheet": {"properties": {"title": "Decisions"}}}
                    ]
                },
            ).execute()

            new_sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
            logger.info(f"Created Decisions tab (sheetId={new_sheet_id})")

            # Write header row (Phase 9A schema with label, rationale, confidence)
            headers = [
                "Label", "Decision", "Rationale", "Confidence",
                "Source Meeting", "Date", "Status",
            ]
            self.service.spreadsheets().values().update(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range="Decisions!A1:G1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()

            return new_sheet_id

        except Exception as e:
            logger.error(f"Error ensuring Decisions tab: {e}")
            return None

    async def add_decisions_batch_to_sheet(
        self,
        decisions: list[dict],
        source_meeting: str,
        meeting_date: str,
    ) -> bool:
        """
        Add a batch of decisions to the Decisions tab.

        Args:
            decisions: List of decision dicts from extraction.
            source_meeting: Name of the source meeting.
            meeting_date: Date of the meeting.

        Returns:
            True if decisions were added successfully.
        """
        if not decisions:
            return True

        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        try:
            sheet_id = await self.ensure_decisions_tab()
            if sheet_id is None:
                return False

            rows = []
            for d in decisions:
                rows.append([
                    d.get("label", ""),
                    d.get("description", ""),
                    d.get("rationale", ""),
                    str(d.get("confidence", 3)),
                    source_meeting,
                    str(meeting_date)[:10],
                    d.get("decision_status", "Active"),
                ])

            self.service.spreadsheets().values().append(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range="Decisions!A:G",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()

            logger.info(f"Added {len(rows)} decisions to Decisions tab")
            return True

        except Exception as e:
            logger.error(f"Error adding decisions to sheet: {e}")
            return False

    # =========================================================================
    # Sheet Formatting
    # =========================================================================

    async def format_task_tracker(self) -> bool:
        """
        Apply professional formatting to the Task Tracker sheet.

        Uses TASK_COLUMNS/TASK_COL_INDEX for column positions.
        No data validation dropdowns (they cause errors with existing data).

        Includes:
        - Dark blue header row with white bold text
        - Frozen header row
        - Fixed column widths: A=50, B=120, C=350, D=90, E=90, F=90, G=120, H=150, I=80
        - Text wrapping on Task column (C)
        - Conditional formatting on Status (F), Category (G), Priority (A)
        - Alternating row colors (zebra striping)
        - Light gray borders on all cells

        Returns:
            True if formatting was applied successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        try:
            # Find the Tasks tab by name (don't assume it's the first tab)
            sid = self._get_sheet_id_by_name(
                settings.TASK_TRACKER_SHEET_ID, settings.TASK_TRACKER_TAB_NAME
            )
            if sid is None:
                # Fallback to first sheet if tab not found
                sid = self._get_first_sheet_id(settings.TASK_TRACKER_SHEET_ID)
            num_cols = len(TASK_COLUMNS)
            requests = []

            # --- Clear existing conditional format rules (idempotent) ---
            requests.extend(
                self._clear_conditional_format_rules(settings.TASK_TRACKER_SHEET_ID)
            )

            # --- Frozen header row ---
            requests.append({
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sid,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            })

            # --- Dark blue header with white bold text ---
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": COLORS["header_bg"],
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": COLORS["header_text"],
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

            # --- Fixed column widths (Phase 10 layout) ---
            # A=Priority(50), B=Label(120), C=Task(350), D=Owner(90),
            # E=Deadline(90), F=Status(90), G=Category(120), H=Source(150), I=Created(80)
            col_widths = [50, 120, 350, 90, 90, 90, 120, 150, 80]
            for i, w in enumerate(col_widths):
                requests.append(_column_width_request(sid, i, w))

            # --- Text wrapping on Task column (C) ---
            requests.append(_text_wrap_request(sid, TASK_COL_INDEX["task"]))

            # --- Conditional formatting: Priority column (A) ---
            rule_idx = 0
            priority_rules = [
                ("H", COLORS["priority_high"]),
                ("M", COLORS["priority_medium"]),
                ("L", COLORS["priority_low"]),
            ]
            for text, color in priority_rules:
                requests.append(
                    _conditional_format_rule(sid, TASK_COL_INDEX["priority"], text, color, rule_idx)
                )
                rule_idx += 1

            # --- Conditional formatting: Status column (F) ---
            status_rules = [
                ("overdue", COLORS["status_overdue"]),
                ("done", COLORS["status_done"]),
                ("in_progress", COLORS["status_in_progress"]),
                ("pending", COLORS["status_pending"]),
            ]
            for text, color in status_rules:
                requests.append(
                    _conditional_format_rule(sid, TASK_COL_INDEX["status"], text, color, rule_idx)
                )
                rule_idx += 1

            # --- Conditional formatting: Category column (G) ---
            category_rules = [
                ("Product & Tech", COLORS["product_tech"]),
                ("BD & Sales", COLORS["business_dev"]),
                ("Strategy & Research", COLORS["marketing"]),
                ("Finance & Fundraising", COLORS["finance"]),
                ("Legal & Compliance", COLORS["legal_ip"]),
                ("Operations & HR", COLORS["operations_hr"]),
            ]
            for text, color in category_rules:
                requests.append(
                    _conditional_format_rule(sid, TASK_COL_INDEX["category"], text, color, rule_idx)
                )
                rule_idx += 1

            # --- NO data validation dropdowns (removed — they cause errors) ---

            # --- Remove existing banding then add fresh (idempotent) ---
            try:
                meta = self.service.spreadsheets().get(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    fields="sheets.bandedRanges",
                ).execute()
                for sheet in meta.get("sheets", []):
                    for br in sheet.get("bandedRanges", []):
                        requests.append({
                            "deleteBanding": {"bandedRangeId": br["bandedRangeId"]}
                        })
            except Exception:
                pass
            requests.append(_banding_request(sid, num_cols))

            # --- Light gray borders on all cells ---
            requests.append(_border_request(sid, num_cols))

            # --- Reset data rows (2-200) to white background + black text ---
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 1,
                        "endRowIndex": 200,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                            "textFormat": {
                                "bold": False,
                                "foregroundColor": {"red": 0, "green": 0, "blue": 0},
                                "fontSize": 10,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

            self.service.spreadsheets().batchUpdate(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                body={"requests": requests},
            ).execute()

            logger.info("Applied professional formatting to Task Tracker")
            return True

        except Exception as e:
            logger.error(f"Error formatting Task Tracker: {e}")
            return False

    async def format_stakeholder_tracker(self) -> bool:
        """
        Apply professional formatting to the Stakeholder Tracker sheet.

        Includes:
        - Dark blue header row with white bold text
        - Frozen header row
        - Fixed column widths
        - Text wrapping on Description (C) and Notes (P)
        - Conditional formatting on Status (O), Priority (F)
        - Data validation dropdowns on Status, Priority
        - Alternating row colors (zebra striping)
        - Light gray borders on all cells
        - Clears existing conditional format rules first (idempotent)

        Returns:
            True if formatting was applied successfully.
        """
        if not settings.STAKEHOLDER_TRACKER_SHEET_ID:
            logger.warning("STAKEHOLDER_TRACKER_SHEET_ID not configured")
            return False

        try:
            num_cols = len(STAKEHOLDER_COLUMNS)  # 16 columns
            sid = self._get_first_sheet_id(settings.STAKEHOLDER_TRACKER_SHEET_ID)
            requests = []

            # --- Clear existing conditional format rules (idempotent) ---
            requests.extend(
                self._clear_conditional_format_rules(
                    settings.STAKEHOLDER_TRACKER_SHEET_ID
                )
            )

            # --- Frozen header row ---
            requests.append({
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sid,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            })

            # --- Dark blue header with white bold text ---
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": COLORS["header_bg"],
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": COLORS["header_text"],
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

            # --- Fixed column widths ---
            # A=180, B=90, C=200, D=160, E=160, F=70,
            # G-J=120 each (primary action), K-N=120 each (secondary action),
            # O=90, P=200
            col_widths = [
                180, 90, 200, 160, 160, 70,    # A-F
                120, 120, 120, 120,             # G-J (Primary Action)
                120, 120, 120, 120,             # K-N (Secondary Action)
                90, 200,                        # O-P
            ]
            for i, w in enumerate(col_widths):
                requests.append(_column_width_request(sid, i, w))

            # --- Text wrapping on Description (C=2) and Notes (P=15) ---
            requests.append(_text_wrap_request(sid, 2))
            requests.append(_text_wrap_request(sid, 15))

            # --- Conditional formatting: Status column (O = index 14) ---
            rule_idx = 0
            status_rules = [
                ("New", COLORS["status_new"]),
                ("Active", COLORS["status_active"]),
                ("Inactive", COLORS["status_inactive"]),
                ("Completed", COLORS["status_completed"]),
            ]
            for text, color in status_rules:
                requests.append(
                    _conditional_format_rule(sid, 14, text, color, rule_idx)
                )
                rule_idx += 1

            # --- Conditional formatting: Priority column (F = index 5) ---
            priority_rules = [
                ("H", COLORS["priority_high"]),
                ("M", COLORS["priority_medium"]),
                ("L", COLORS["priority_low"]),
            ]
            for text, color in priority_rules:
                requests.append(
                    _conditional_format_rule(sid, 5, text, color, rule_idx)
                )
                rule_idx += 1

            # --- Data validation dropdowns ---
            requests.append(
                _data_validation_request(sid, 14, STAKEHOLDER_STATUSES)  # Status (O)
            )
            requests.append(
                _data_validation_request(sid, 5, PRIORITIES)  # Priority (F)
            )

            # --- Alternating row colors (zebra striping) ---
            requests.append(_banding_request(sid, num_cols))

            # --- Light gray borders on all cells ---
            requests.append(_border_request(sid, num_cols))

            self.service.spreadsheets().batchUpdate(
                spreadsheetId=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                body={"requests": requests},
            ).execute()

            logger.info("Applied professional formatting to Stakeholder Tracker")
            return True

        except Exception as e:
            logger.error(f"Error formatting Stakeholder Tracker: {e}")
            return False

    async def ensure_task_tracker_headers(self) -> bool:
        """
        Ensure the Task Tracker sheet has proper headers.

        Creates headers if the sheet is empty.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            return False

        try:
            rows = await self._read_sheet_range(
                sheet_id=settings.TASK_TRACKER_SHEET_ID,
                range_name="A1:I1"
            )

            if not rows:
                # Add headers
                await self._write_sheet_range(
                    sheet_id=settings.TASK_TRACKER_SHEET_ID,
                    range_name="A1:I1",
                    values=[TASK_TRACKER_HEADERS]
                )
                logger.info("Created Task Tracker headers")

            return True

        except Exception as e:
            logger.error(f"Error ensuring headers: {e}")
            return False


# Singleton instance
sheets_service = GoogleSheetsService()
