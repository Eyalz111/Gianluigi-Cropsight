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


# Task Tracker column configuration
TASK_TRACKER_COLUMNS = [
    "Task",
    "Assignee",
    "Source Meeting",
    "Deadline",
    "Status",
    "Priority",
    "Created Date",
    "Updated Date",
]

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
        created_date: str
    ) -> bool:
        """
        Add a new task to the Task Tracker sheet.

        Args:
            task: Task description.
            assignee: Who is responsible.
            source_meeting: Name of the meeting where task was created.
            deadline: Due date (YYYY-MM-DD) or None.
            status: 'pending', 'in_progress', 'done', 'overdue'.
            priority: 'H', 'M', or 'L'.
            created_date: When the task was created (YYYY-MM-DD).

        Returns:
            True if task was added successfully.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        values = [
            task,
            assignee,
            source_meeting,
            deadline or "",
            status,
            priority,
            created_date,
            created_date,  # Updated date = created date initially
        ]

        return await self._append_row(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values
        )

    async def update_task_status(
        self,
        row_number: int,
        status: str,
        updated_date: str
    ) -> bool:
        """
        Update a task's status in the Task Tracker.

        Args:
            row_number: The row number of the task (1-indexed, header is row 1).
            status: New status value.
            updated_date: Current date (YYYY-MM-DD).

        Returns:
            True if update was successful.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return False

        try:
            # Update Status (column E) and Updated Date (column H)
            range_name = f"Sheet1!E{row_number}:H{row_number}"
            values = [[status, None, None, updated_date]]

            # Use COLUMNS input option to update only specific columns
            result = self.service.spreadsheets().values().update(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                range=range_name,
                valueInputOption="RAW",
                body={"values": values}
            ).execute()

            logger.info(f"Updated task row {row_number}: status={status}")
            return True

        except Exception as e:
            logger.error(f"Error updating task status: {e}")
            return False

    async def get_all_tasks(self) -> list[dict]:
        """
        Get all tasks from the Task Tracker.

        Returns:
            List of task dicts with all columns.
        """
        if not settings.TASK_TRACKER_SHEET_ID:
            logger.warning("TASK_TRACKER_SHEET_ID not configured")
            return []

        rows = await self._read_sheet_range(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            range_name="Sheet1!A:H"
        )

        if not rows or len(rows) < 2:
            return []

        # Skip header row
        tasks = []
        for i, row in enumerate(rows[1:], start=2):
            # Pad row if needed
            while len(row) < len(TASK_TRACKER_COLUMNS):
                row.append("")

            tasks.append({
                "row_number": i,
                "task": row[0],
                "assignee": row[1],
                "source_meeting": row[2],
                "deadline": row[3],
                "status": row[4],
                "priority": row[5],
                "created_date": row[6],
                "updated_date": row[7] if len(row) > 7 else "",
            })

        return tasks

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

        rows = await self._read_sheet_range(
            sheet_id=settings.STAKEHOLDER_TRACKER_SHEET_ID,
            range_name="Sheet1!A:P"  # 16 columns
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
            values.append([
                task.get("task", ""),
                task.get("assignee", ""),
                task.get("source_meeting", ""),
                task.get("deadline", ""),
                task.get("status", "pending"),
                task.get("priority", "M"),
                task.get("created_date", datetime.now().strftime("%Y-%m-%d")),
                task.get("created_date", datetime.now().strftime("%Y-%m-%d")),
            ])

        return await self._append_rows(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values
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
                task_desc,
                led_by,
                source_meeting,
                "",  # deadline — follow-ups often don't have a parseable date
                "pending",
                "H",
                created_date,
                created_date,
            ])

        return await self._append_rows(
            sheet_id=settings.TASK_TRACKER_SHEET_ID,
            values=values
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
            if not name or name.lower() in existing_names:
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
            self.service.spreadsheets().values().append(
                spreadsheetId=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                range="Sheet1!A:P",
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

    async def _read_sheet_range(
        self,
        sheet_id: str,
        range_name: str
    ) -> list[list[Any]]:
        """
        Read a range of cells from a sheet.

        Args:
            sheet_id: Google Sheets ID.
            range_name: A1 notation range (e.g., "Sheet1!A1:Z100").

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
        values: list[Any]
    ) -> bool:
        """
        Append a row to the end of a sheet.

        Args:
            sheet_id: Google Sheets ID.
            values: List of values for the new row.

        Returns:
            True if append was successful.
        """
        return await self._append_rows(sheet_id, [values])

    async def _append_rows(
        self,
        sheet_id: str,
        values: list[list[Any]]
    ) -> bool:
        """
        Append multiple rows to the end of a sheet.

        Args:
            sheet_id: Google Sheets ID.
            values: 2D list of values for the new rows.

        Returns:
            True if append was successful.
        """
        try:
            self.service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="Sheet1!A:H",
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
                            range_name=f"Sheet1!{col}{target_row}",
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

                await self._append_row_to_range(
                    sheet_id=settings.STAKEHOLDER_TRACKER_SHEET_ID,
                    range_name="Sheet1!A:P",
                    values=new_row,
                )

                logger.info(f"Added new stakeholder: {organization}")

            return True

        except Exception as e:
            logger.error(f"Error applying stakeholder update: {e}")
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
                range_name="Sheet1!A1:H1"
            )

            if not rows:
                # Add headers
                await self._write_sheet_range(
                    sheet_id=settings.TASK_TRACKER_SHEET_ID,
                    range_name="Sheet1!A1:H1",
                    values=[TASK_TRACKER_COLUMNS]
                )
                logger.info("Created Task Tracker headers")

            return True

        except Exception as e:
            logger.error(f"Error ensuring headers: {e}")
            return False


# Singleton instance
sheets_service = GoogleSheetsService()
