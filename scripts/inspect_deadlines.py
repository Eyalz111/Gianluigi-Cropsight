"""
One-off diagnostic (2026-06-11): for sheet rows with non-ISO deadline text,
show what the DB actually stored after the midday reconcile pull. Also check
stray column-L content. Read-only.
"""

import asyncio
import json
import os
import re
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from services.supabase_client import supabase_client

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def main():
    from services.google_sheets import sheets_service
    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"
    rows = await sheets_service._read_sheet_range(
        sheet_id=settings.TASK_TRACKER_SHEET_ID,
        range_name=f"'{tab}'!A1:N",
    )
    header, data = rows[0], rows[1:]
    db_tasks = {t["id"]: t for t in supabase_client.get_tasks(status=None, limit=2000, include_pending=True)}
    snaps = supabase_client.get_sheet_snapshots()

    out = {"non_iso_rows": [], "col_L_nonempty": 0, "col_L_samples": [], "row_len_census": {}}
    for i, r in enumerate(data, start=2):
        ln = len(r)
        out["row_len_census"][ln] = out["row_len_census"].get(ln, 0) + 1
        if ln > 11:
            out["col_L_nonempty"] += 1
            if len(out["col_L_samples"]) < 5:
                out["col_L_samples"].append({"row": i, "extra": r[11:]})
        d = r[4].strip() if ln > 4 and r[4] else ""
        uid = r[9].strip() if ln > 9 and r[9] else ""
        if d and not ISO.match(d):
            t = db_tasks.get(uid, {})
            snap = snaps.get(uid) or {}
            out["non_iso_rows"].append({
                "row": i,
                "sheet_deadline": d,
                "db_deadline": str(t.get("deadline")),
                "db_deadline_confidence": t.get("deadline_confidence"),
                "snap_deadline": str(snap.get("deadline")),
                "title": (t.get("title") or r[2] if len(r) > 2 else "")[:50],
            })
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
