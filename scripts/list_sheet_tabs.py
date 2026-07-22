"""One-off: list all tabs in the Task Tracker + Gantt spreadsheets. Read-only."""
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from services.google_sheets import sheets_service

for label, sid in (
    ("TASK TRACKER", settings.TASK_TRACKER_SHEET_ID),
    ("GANTT", getattr(settings, "GANTT_SHEET_ID", None)),
    ("STAKEHOLDER", getattr(settings, "STAKEHOLDER_TRACKER_SHEET_ID", None)),
):
    if not sid:
        continue
    try:
        meta = sheets_service.service.spreadsheets().get(
            spreadsheetId=sid, fields="sheets.properties"
        ).execute()
        print(f"{label} ({sid}):")
        for s in meta["sheets"]:
            p = s["properties"]
            gp = p.get("gridProperties", {})
            print(f"  - {p['title']}  (rows={gp.get('rowCount')}, cols={gp.get('columnCount')}, hidden={p.get('hidden', False)})")
    except Exception as e:
        print(f"{label}: error {e}")
