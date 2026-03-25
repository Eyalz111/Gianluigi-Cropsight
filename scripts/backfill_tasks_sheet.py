"""
One-time backfill: rebuild the Tasks sheet from Supabase DB.

Clears the Tasks tab, writes a clean header, then populates all
tasks from the database. Run manually when Sheets data is out of sync.

Usage:
    python scripts/backfill_tasks_sheet.py
"""

import asyncio
import os
import sys

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


async def backfill():
    from config.settings import settings
    from services.google_sheets import sheets_service
    from services.supabase_client import supabase_client

    tab_name = settings.TASK_TRACKER_TAB_NAME or "Tasks"
    sheet_id = settings.TASK_TRACKER_SHEET_ID

    if not sheet_id:
        print("ERROR: TASK_TRACKER_SHEET_ID not set")
        return

    print(f"Backfilling Tasks sheet (tab: '{tab_name}', sheet: {sheet_id[:20]}...)")

    # 1. Get all tasks from Supabase
    all_tasks = []
    for status in ["pending", "in_progress", "done", "overdue"]:
        tasks = supabase_client.get_tasks(status=status, limit=500)
        all_tasks.extend(tasks)

    print(f"Found {len(all_tasks)} tasks in Supabase")

    if not all_tasks:
        print("No tasks to backfill")
        return

    # 2. Clear the sheet (keep only header)
    try:
        sheets_service.service.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A:I",
        ).execute()
        print("Cleared existing sheet data")
    except Exception as e:
        print(f"Error clearing sheet: {e}")
        return

    # 3. Write header row
    headers = [["Task", "Category", "Assignee", "Source Meeting", "Deadline",
                "Status", "Priority", "Created Date", "Updated Date"]]
    sheets_service.service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!A1:I1",
        valueInputOption="RAW",
        body={"values": headers},
    ).execute()
    print("Wrote header row")

    # 4. Build rows from tasks
    rows = []
    for t in all_tasks:
        meeting = t.get("meetings", {}) or {}
        meeting_title = meeting.get("title", "")
        rows.append([
            t.get("title", ""),
            t.get("category", ""),
            t.get("assignee", ""),
            meeting_title,
            str(t.get("deadline", "") or ""),
            t.get("status", "pending"),
            t.get("priority", "M"),
            str(t.get("created_at", ""))[:10],
            str(t.get("updated_at", ""))[:10],
        ])

    # 5. Write all rows at once
    sheets_service.service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!A:I",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()

    print(f"Wrote {len(rows)} task rows to '{tab_name}' tab")
    print("Backfill complete!")


if __name__ == "__main__":
    asyncio.run(backfill())
