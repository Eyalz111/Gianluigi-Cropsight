"""One-off (2026-06-11): verify the 'WC gambler' content wasn't deleted today.
1. Lists the Task Tracker's tab names AT REVISION 1216 (01:37Z, before any of
   today's edits/wipes/rebuilds) by exporting that revision as xlsx and reading
   xl/workbook.xml — no openpyxl needed.
2. Searches Drive for files whose name matches gambler/WC patterns.
Read-only."""
import io
import os
import re
import sys
import urllib.request
import zipfile

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from services.google_drive import drive_service

svc = drive_service.service
creds = drive_service._credentials

# --- 1. tab names at revision 1216 (pre-everything-today) ---
rev = svc.revisions().get(
    fileId=settings.TASK_TRACKER_SHEET_ID, revisionId="1216",
    fields="id,modifiedTime,exportLinks",
).execute()
xlsx_url = (rev.get("exportLinks") or {}).get(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
if xlsx_url:
    req = urllib.request.Request(xlsx_url, headers={"Authorization": f"Bearer {creds.token}"})
    with urllib.request.urlopen(req) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        wb = z.read("xl/workbook.xml").decode("utf-8")
    names = re.findall(r'<sheet[^>]*name="([^"]+)"', wb)
    print(f"Task Tracker tabs at revision 1216 ({rev['modifiedTime']}):")
    for n in names:
        print("  -", n)
else:
    print("no xlsx exportLink on revision 1216")

# --- 2. Drive-wide name search ---
print("\nDrive files matching gambler/WC patterns:")
for q in ("name contains 'gambler'", "name contains 'Gambler'",
          "name contains 'WC '", "name contains 'world cup'",
          "name contains 'World Cup'", "name contains 'mundial'"):
    try:
        res = svc.files().list(
            q=q + " and trashed=false",
            fields="files(id,name,mimeType,modifiedTime)",
            pageSize=10,
        ).execute()
        for f in res.get("files", []):
            print(f"  [{q}] {f['name']}  ({f['mimeType'].split('.')[-1]}, modified {f['modifiedTime']})")
    except Exception as e:
        print(f"  [{q}] error: {e}")

# also check the trash explicitly, in case something WAS deleted
print("\nTrashed files matching:")
for q in ("name contains 'gambler'", "name contains 'Gambler'", "name contains 'World Cup'"):
    try:
        res = svc.files().list(
            q=q + " and trashed=true",
            fields="files(id,name,mimeType,modifiedTime)",
            pageSize=10,
        ).execute()
        for f in res.get("files", []):
            print(f"  [TRASH] {f['name']}  (modified {f['modifiedTime']})")
    except Exception as e:
        print(f"  [{q}] error: {e}")
print("done")
