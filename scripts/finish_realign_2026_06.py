"""
Final repair pass (2026-06-11) — uses scripts/rev1385_recovered.json (Eyal's
sheet exactly as he left it, recovered from Drive revision 1385) as ground
truth for his edit session. Run AFTER recover_rev1385.py.

MUST run with TASK_SHEET_URGENCY_AREA_ENABLED=true in the environment so the
sheet layout matches production (A:K with Urgency) — the script asserts this.

Steps:
1. Restore his deadline edits to the DB (incl. the 19 nulled by the incident).
2. Archive every approved task he deleted (not in the kept-82, created before
   his session) — status='archived'.
3. Rebuild the Tasks tab from the DB: A:K, correct urgency, ISO deadlines,
   canonical categories, no archived tasks. Clears the stale misaligned K data.
4. Remove leftover column L if the grid still has one.
5. Append the archived tasks to the Archive tab.
6. Reseed reconcile snapshots; re-apply formatting; audit-log.

Usage:
    $env:TASK_SHEET_URGENCY_AREA_ENABLED='true'
    python scripts/finish_realign_2026_06.py            # dry-run
    python scripts/finish_realign_2026_06.py --apply
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.supabase_client import supabase_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("finish_realign")

EDIT_SESSION_CUTOFF = "2026-06-11T07:45:08Z"  # revision 1385 timestamp


async def run(apply: bool) -> None:
    from config.settings import settings
    from services.google_sheets import (
        sheets_service, TASK_COLUMNS, TASK_TRACKER_HEADERS,
    )

    assert getattr(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False), (
        "Run with TASK_SHEET_URGENCY_AREA_ENABLED=true so the rebuild writes "
        "the K (Urgency) column — prod runs with this flag on."
    )
    assert "urgency" in TASK_COLUMNS

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "rev1385_recovered.json"), encoding="utf-8") as f:
        rec = json.load(f)
    kept = set(rec["kept_uuids"])
    deadline_edits = [d for d in rec["deadline_edits"] if d.get("iso")]

    db_tasks = supabase_client.get_tasks(
        status=None, limit=2000, include_pending=True, include_archived=True
    )
    db_by_id = {t["id"]: t for t in db_tasks if t.get("id")}

    report = {"applied": apply, "kept_rows": len(kept)}

    # ---- 1. deadline restore ----
    fixes = []
    for d in deadline_edits:
        t = db_by_id.get(d["id"])
        if not t:
            continue
        if str(t.get("deadline") or "")[:10] != d["iso"]:
            fixes.append((d["id"], d["iso"], d["cell"], d["title"]))
    report["deadline_fixes"] = len(fixes)
    report["deadline_fix_list"] = [
        {"task": i[:8], "cell": c, "->": iso, "title": ti} for i, iso, c, ti in fixes
    ]

    # ---- 2. archive set ----
    to_archive = []
    for t in db_tasks:
        tid = t.get("id")
        if not tid or tid in kept:
            continue
        if t.get("status") == "archived":
            continue
        if t.get("approval_status") != "approved":
            continue  # approval flow owns pending items
        created = str(t.get("created_at") or "")
        if created and created > "2026-06-11T07:45":
            continue  # created after his session — not part of his cleanup
        to_archive.append(t)
    report["to_archive"] = len(to_archive)
    report["archive_sample"] = [
        {"id": t["id"][:8], "status": t.get("status"), "title": (t.get("title") or "")[:55]}
        for t in to_archive[:15]
    ]

    if not apply:
        report["note"] = "dry-run — nothing written"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # ===== APPLY =====
    for tid, iso, _cell, _title in fixes:
        supabase_client.update_task(tid, deadline=iso, deadline_confidence="EXPLICIT")
    logger.info(f"deadlines restored: {len(fixes)}")

    for t in to_archive:
        supabase_client.update_task(t["id"], status="archived")
    logger.info(f"archived: {len(to_archive)}")

    # ---- 3. rebuild Tasks tab (A:K, fresh everything) ----
    fresh = supabase_client.get_tasks(status=None, limit=2000)  # approved, non-archived
    ok = await sheets_service.rebuild_tasks_sheet(fresh)
    logger.info(f"sheet rebuild: {ok} ({len(fresh)} active tasks)")
    if not ok:
        print(json.dumps({**report, "error": "rebuild failed"}, indent=2))
        return

    # ---- 4. delete leftover column L if the grid is wider than 11 cols ----
    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"
    try:
        meta = sheets_service.service.spreadsheets().get(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            fields="sheets(properties(sheetId,title,gridProperties(columnCount)))",
        ).execute()
        for s in meta.get("sheets", []):
            p = s["properties"]
            if p.get("title") == tab and p.get("gridProperties", {}).get("columnCount", 0) > 11:
                sheets_service.service.spreadsheets().batchUpdate(
                    spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                    body={"requests": [{"deleteDimension": {"range": {
                        "sheetId": p["sheetId"], "dimension": "COLUMNS",
                        "startIndex": 11,
                        "endIndex": p["gridProperties"]["columnCount"],
                    }}}]},
                ).execute()
                logger.info(f"trimmed columns L+ ({p['gridProperties']['columnCount']} -> 11)")
    except Exception as e:
        logger.warning(f"column trim skipped: {e}")

    # ---- 5. Archive tab: rewrite from the full archived set ----
    archived_tasks = supabase_client.get_tasks(
        status="archived", limit=2000, include_pending=True
    )
    if archived_tasks:
        sheets_service._ensure_archive_tab()
        today = datetime.now().strftime("%Y-%m-%d")
        values = []
        for t in archived_tasks:
            meeting_info = t.get("meetings") if isinstance(t.get("meetings"), dict) else {}
            row = [
                t.get("priority", "M"), t.get("label", ""), t.get("title", ""),
                t.get("assignee", ""), str(t.get("deadline") or ""), "archived",
                t.get("category", ""), (meeting_info or {}).get("title", ""),
                str(t.get("created_at", ""))[:10], t.get("id", ""),
                t.get("urgency", "M"), today,
            ]
            values.append(row)
        n = len(TASK_TRACKER_HEADERS) + 1
        # clear data rows then write fresh (idempotent across repair re-runs)
        sheets_service.service.spreadsheets().values().clear(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            range=f"Archive!A2:{chr(ord('A') + n - 1)}",
        ).execute()
        sheets_service.service.spreadsheets().values().update(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            range=f"Archive!A2:{chr(ord('A') + n - 1)}{len(values) + 1}",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
        logger.info(f"archive tab rewritten: {len(values)} rows")

    # ---- 6. snapshots + formatting + audit ----
    rows = await sheets_service.get_all_tasks()
    seeded = 0
    for r in rows:
        rid = (r.get("id") or "").strip()
        if rid:
            supabase_client.upsert_sheet_snapshot(
                rid, r.get("row_number"), r.get("status"), r.get("deadline"),
                r.get("priority"), r.get("assignee"),
            )
            seeded += 1
    logger.info(f"snapshots reseeded: {seeded}")
    try:
        await sheets_service.format_task_tracker()
    except Exception as e:
        logger.warning(f"formatting skipped: {e}")

    supabase_client.log_action(
        "category_realign_finish_2026_06",
        details={"deadline_fixes": len(fixes), "archived": len(to_archive),
                 "active_rows": len(fresh), "snapshots": seeded,
                 "source_revision": rec["revision"]},
        triggered_by="eyal",
    )
    report["done"] = True
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(run(apply=ap.parse_args().apply))
