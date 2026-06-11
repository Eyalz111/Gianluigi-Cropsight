"""
One-off diagnostic (2026-06-11): inspect live Sheet + DB + audit_log after
Eyal's manual sheet edits, before the category/area realignment work.

Read-only. Usage: python scripts/inspect_live_state.py
"""

import asyncio
import json
import os
import sys
from collections import Counter

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from services.supabase_client import supabase_client


async def main():
    out = {}

    # --- 1. Live sheet raw header + rows ---
    from services.google_sheets import sheets_service
    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"
    rows = await sheets_service._read_sheet_range(
        sheet_id=settings.TASK_TRACKER_SHEET_ID,
        range_name=f"'{tab}'!A1:N",
    )
    header = rows[0] if rows else []
    data = rows[1:] if rows else []
    out["sheet_tab"] = tab
    out["sheet_header"] = header
    out["sheet_row_count"] = len(data)

    def col(row, i):
        return row[i].strip() if i < len(row) and row[i] else ""

    # column positions by header name (robust to his column deletion)
    hidx = {h.strip().lower(): i for i, h in enumerate(header)}
    out["header_index"] = hidx

    # deadline format census + uuid presence + value census
    import re
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    dmy = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$")
    deadline_formats = Counter()
    deadline_samples = {}
    uuid_col = None
    for name in ("id", "task id", "uuid", "db id"):
        if name in hidx:
            uuid_col = hidx[name]
            break
    uuids_present = 0
    cat_vals = Counter()
    urg_vals = Counter()
    area_vals = Counter()
    prio_vals = Counter()
    status_vals = Counter()
    sheet_uuids = set()
    for r in data:
        d = col(r, hidx.get("deadline", -1)) if "deadline" in hidx else ""
        if d:
            if iso.match(d):
                k = "iso"
            elif dmy.match(d):
                k = "d/m/y-ish"
            else:
                k = "other"
            deadline_formats[k] += 1
            deadline_samples.setdefault(k, []).append(d) if len(deadline_samples.get(k, [])) < 8 else None
        if uuid_col is not None:
            u = col(r, uuid_col)
            if u:
                uuids_present += 1
                sheet_uuids.add(u)
        if "category" in hidx:
            cat_vals[col(r, hidx["category"]) or "(blank)"] += 1
        if "urgency" in hidx:
            urg_vals[col(r, hidx["urgency"]) or "(blank)"] += 1
        if "area" in hidx:
            area_vals[col(r, hidx["area"]) or "(blank)"] += 1
        if "priority" in hidx:
            prio_vals[col(r, hidx["priority"]) or "(blank)"] += 1
        if "status" in hidx:
            status_vals[col(r, hidx["status"]) or "(blank)"] += 1
    out["deadline_formats"] = dict(deadline_formats)
    out["deadline_samples"] = deadline_samples
    out["uuids_present"] = uuids_present
    out["category_values"] = dict(cat_vals)
    out["urgency_values"] = dict(urg_vals)
    out["area_values"] = dict(area_vals)
    out["priority_values"] = dict(prio_vals)
    out["status_values"] = dict(status_vals)

    # --- 2. DB tasks ---
    db_tasks = supabase_client.get_tasks(status=None, limit=2000, include_pending=True)
    out["db_task_count"] = len(db_tasks)
    out["db_status"] = dict(Counter((t.get("status") or "(none)") for t in db_tasks))
    out["db_category"] = dict(Counter((t.get("category") or "(none)") for t in db_tasks))
    out["db_area_label"] = dict(Counter((t.get("area_label") or "(none)") for t in db_tasks))
    out["db_urgency"] = dict(Counter((t.get("urgency") or "(none)") for t in db_tasks))
    bad_deadlines = [
        {"id": t["id"][:8], "title": (t.get("title") or "")[:40], "deadline": str(t.get("deadline"))}
        for t in db_tasks
        if t.get("deadline") and not iso.match(str(t.get("deadline"))[:10])
    ]
    out["db_nonISO_deadlines"] = bad_deadlines[:15]
    out["db_nonISO_deadline_count"] = len(bad_deadlines)

    # open DB tasks missing from sheet (= rows Eyal deleted, candidates for re-add)
    open_statuses = ("pending", "in_progress", "overdue")
    db_open_not_in_sheet = [
        {"id": t["id"], "title": (t.get("title") or "")[:60], "status": t.get("status"),
         "assignee": t.get("assignee"), "deadline": str(t.get("deadline") or "")}
        for t in db_tasks
        if t.get("id") and t["id"] not in sheet_uuids and (t.get("status") or "pending") in open_statuses
    ]
    out["db_open_missing_from_sheet_count"] = len(db_open_not_in_sheet)
    out["db_open_missing_from_sheet"] = db_open_not_in_sheet[:40]

    # --- 3. areas table ---
    try:
        areas = supabase_client.get_areas()
        out["areas"] = [{"name": a.get("name"), "gantt_section": a.get("gantt_section"),
                         "status": a.get("status")} for a in areas]
    except Exception as e:
        out["areas_error"] = str(e)

    # --- 4. audit log since June 9 ---
    try:
        resp = (
            supabase_client.client.table("audit_log")
            .select("created_at,action,details,triggered_by")
            .gte("created_at", "2026-06-09T00:00:00")
            .in_("action", ["reconcile_applied", "shadow_reconcile", "reconcile_dryrun",
                            "sheets_sync_applied", "scheduler_heartbeat"])
            .order("created_at", desc=True)
            .limit(60)
            .execute()
        )
        entries = []
        for e in resp.data or []:
            det = e.get("details") or {}
            if e["action"] == "scheduler_heartbeat" and det.get("scheduler") not in ("reconcile", "gantt_reconcile"):
                continue
            entries.append({"at": e["created_at"], "action": e["action"], "details": det})
        out["audit_recent"] = entries
    except Exception as e:
        out["audit_error"] = str(e)

    # --- 5. snapshots ---
    try:
        snaps = supabase_client.get_sheet_snapshots()
        out["snapshot_count"] = len(snaps)
    except Exception as e:
        out["snapshot_error"] = str(e)

    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
