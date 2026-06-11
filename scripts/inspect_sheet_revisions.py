"""One-off: list Drive revisions of the Task Tracker spreadsheet (read-only).
Goal: find a revision of the Tasks sheet from after Eyal's manual edits but
before the 2026-06-11 10:00Z midday reconcile re-added his deleted rows."""
import json, os, sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from services.google_drive import drive_service

svc = drive_service.service
file_id = settings.TASK_TRACKER_SHEET_ID

revs = []
page_token = None
while True:
    resp = svc.revisions().list(
        fileId=file_id,
        fields="nextPageToken,revisions(id,modifiedTime,exportLinks)",
        pageSize=200,
        pageToken=page_token,
    ).execute()
    revs.extend(resp.get("revisions", []))
    page_token = resp.get("nextPageToken")
    if not page_token:
        break

print(f"total revisions: {len(revs)}")
for r in revs[-30:]:
    has_export = "xlsx" if any("spreadsheetml" in k for k in (r.get("exportLinks") or {})) else "-"
    print(r["id"], r["modifiedTime"], has_export)
