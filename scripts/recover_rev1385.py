"""
One-off recovery (2026-06-11): export revision 1385 of the Task Tracker —
Eyal's sheet exactly as he left it (07:45Z, post-edits, pre-reconcile-undo) —
and derive:
  1. kept_uuids — rows he KEPT (col J UUIDs present)
  2. deadline_edits — every (uuid, parsed ISO deadline) from his cells
Writes the result to scripts/rev1385_recovered.json. Read-only against live.
"""
import csv
import io
import json
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from core.dates import parse_human_date
from services.google_drive import drive_service

REV = "1385"

svc = drive_service.service  # ensures fresh credentials
creds = drive_service._credentials

import google.auth.transport.requests as gatr
session_request = gatr.Request()
creds.refresh(session_request) if (creds.expired or not creds.token) else None

import urllib.request

url = (
    f"https://www.googleapis.com/drive/v3/files/{settings.TASK_TRACKER_SHEET_ID}"
    f"/export?mimeType=text%2Fcsv&revisionId={REV}"
)
# Drive v3 export doesn't take revisionId — use the revision exportLinks instead.
rev = svc.revisions().get(
    fileId=settings.TASK_TRACKER_SHEET_ID, revisionId=REV,
    fields="id,modifiedTime,exportLinks",
).execute()
links = rev.get("exportLinks") or {}
csv_url = links.get("text/csv")
if not csv_url:
    print(json.dumps({"error": "no text/csv exportLink", "links": list(links)}))
    sys.exit(1)

req = urllib.request.Request(csv_url, headers={"Authorization": f"Bearer {creds.token}"})
with urllib.request.urlopen(req) as resp:
    raw = resp.read().decode("utf-8-sig")

rows = list(csv.reader(io.StringIO(raw)))
header = rows[0] if rows else []
print(f"revision {rev['id']} @ {rev['modifiedTime']} — {len(rows)-1} data rows")
print("header:", header)

if not header or header[2].strip().lower() != "task":
    print(json.dumps({"error": "unexpected header — wrong tab exported?", "header": header}))
    sys.exit(1)

kept_uuids = []
deadline_edits = []
for r in rows[1:]:
    r = r + [""] * (12 - len(r))
    uid = r[9].strip()
    if not uid:
        continue
    kept_uuids.append(uid)
    cell = r[4].strip()
    if cell:
        iso = parse_human_date(cell)
        deadline_edits.append({"id": uid, "cell": cell, "iso": iso,
                               "title": r[2][:60]})

out = {
    "revision": rev["id"],
    "modified": rev["modifiedTime"],
    "rows": len(rows) - 1,
    "kept_uuids": kept_uuids,
    "deadline_edits": deadline_edits,
}
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rev1385_recovered.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f"kept rows w/ UUID: {len(kept_uuids)}; deadline cells: {len(deadline_edits)}; "
      f"unparseable: {[d['cell'] for d in deadline_edits if not d['iso']]}")
print(f"written: {path}")
