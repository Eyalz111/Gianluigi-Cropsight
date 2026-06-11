"""
One-shot live-data repair + realignment (2026-06-11) — run AFTER deploying the
category-realignment code (or locally from the same branch).

Fixes, in order:
1. DEADLINES — repair the 2026-06-11 incident: sheet cells like "20.6.26" were
   pulled and stored as NULL. Re-parse every non-ISO deadline cell (day-first),
   write the correct date to the DB, rewrite the cell as ISO, fix the snapshot.
2. ARCHIVE — the ~90 rows Eyal deleted (reconcile re-added them; identifiable
   as the 12-column rows carrying the stray 'non-area' in headerless col L):
   mark status='archived' in the DB. The sheet rebuild below drops them from
   the Tasks tab; they're appended to the Archive tab for context.
3. CATEGORY — canonicalize every task's category to the Gantt-area taxonomy:
   deterministic legacy map first, Haiku classification for the rest.
4. SHEET — rebuild the Tasks tab (A:K layout, no Area column), delete the
   leftover column L, append archived tasks to the Archive tab, reseed
   reconcile snapshots so the next cycle's diff is empty.

Usage:
    python scripts/realign_tasks_2026_06.py            # dry-run report
    python scripts/realign_tasks_2026_06.py --apply    # do it
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections import Counter

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dates import parse_human_date
from services.supabase_client import supabase_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("realign")

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _classify_with_haiku(tasks: list[dict], area_names: list[str]) -> dict[str, str]:
    """Classify leftover tasks into areas with Haiku. Returns {task_id: category}."""
    from core.llm import call_llm
    from config.settings import settings

    out: dict[str, str] = {}
    batch_size = 25
    options = area_names + ["General"]
    for i in range(0, len(tasks), batch_size):
        batch = tasks[i:i + batch_size]
        lines = [
            f'{t["id"]}: title="{(t.get("title") or "")[:120]}" '
            f'label="{t.get("label") or ""}" old_category="{t.get("category") or ""}"'
            for t in batch
        ]
        prompt = (
            "Classify each CropSight task into exactly ONE of these Gantt board areas:\n"
            + "\n".join(f"- {o}" for o in options)
            + "\n\nUse 'General' only for a genuine misfit. CropSight is an AgTech "
            "startup (ML crop-yield forecasting; team: Eyal CEO, Roye CTO, Paolo BD, "
            "Yoram advisor). Hints: investor/fundraising/grant -> FUNDRAISING & "
            "INVESTOR RELATIONS; contracts/incorporation/ESOP/insurance/budget -> "
            "LEGAL, CORPORATE & FINANCE; client pilots/delivery -> CLIENT DELIVERY "
            "& OPERATIONS; hiring/team -> TEAM & HUMAN RESOURCES; model/data/platform "
            "-> PRODUCT & TECHNOLOGY; sales/partners/consortiums/marketing -> SALES "
            "& BUSINESS DEVELOPMENT.\n\nTasks:\n" + "\n".join(lines)
            + '\n\nRespond with ONLY a JSON object: {"<task_id>": "<area name>", ...}'
        )
        text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,  # Haiku tier — classification task
            max_tokens=4096,
            call_site="category_realign_backfill",
        )
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            logger.warning(f"Haiku batch {i // batch_size}: no JSON in response")
            continue
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            logger.warning(f"Haiku batch {i // batch_size}: bad JSON ({e})")
            continue
        valid = set(options)
        for tid, cat in parsed.items():
            out[tid] = cat if cat in valid else "General"
    return out


async def run(apply: bool) -> None:
    from config.settings import settings
    from services.google_sheets import sheets_service, TASK_COLUMNS

    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"
    report: dict = {"applied": apply}

    # ---------- read everything ----------
    raw_rows = await sheets_service._read_sheet_range(
        sheet_id=settings.TASK_TRACKER_SHEET_ID, range_name=f"'{tab}'!A1:N"
    )
    header, data = raw_rows[0], raw_rows[1:]
    db_tasks = supabase_client.get_tasks(
        status=None, limit=2000, include_pending=True, include_archived=True
    )
    db_by_id = {t["id"]: t for t in db_tasks if t.get("id")}
    areas = supabase_client.get_areas()
    area_names = [a["name"] for a in areas]
    report["areas"] = area_names

    # ---------- 1. deadline repair ----------
    deadline_fixes = []  # (task_id, iso, raw_cell)
    for r in data:
        cell = (r[4].strip() if len(r) > 4 and r[4] else "")
        uid = (r[9].strip() if len(r) > 9 and r[9] else "")
        if not cell or not uid or uid not in db_by_id:
            continue
        iso = parse_human_date(cell)
        if not iso:
            logger.warning(f"UNPARSEABLE deadline cell {cell!r} (task {uid}) — needs manual fix")
            continue
        db_d = str(db_by_id[uid].get("deadline") or "")[:10]
        if db_d != iso:
            deadline_fixes.append((uid, iso, cell))
    report["deadline_fixes"] = len(deadline_fixes)
    report["deadline_fix_samples"] = [
        {"task": t[:8], "cell": raw, "->": iso} for t, iso, raw in deadline_fixes[:25]
    ]

    # ---------- 2. archive the resurrected rows (12-col rows w/ stray col L) ----------
    archive_ids = []
    for r in data:
        if len(r) > 11 and (r[11] or "").strip():  # stray col-L value = re-added today
            uid = (r[9].strip() if len(r) > 9 and r[9] else "")
            if uid and uid in db_by_id and db_by_id[uid].get("status") != "archived":
                archive_ids.append(uid)
    report["to_archive"] = len(archive_ids)

    # ---------- 3. category canonicalization ----------
    canonical = set(area_names) | {"General"}
    cat_updates: dict[str, str] = {}
    needs_llm: list[dict] = []
    for t in db_tasks:
        cur = (t.get("category") or "").strip()
        if cur in canonical:
            continue
        resolved = supabase_client.resolve_category(cur, areas=areas)
        if resolved in canonical and resolved != "General":
            cat_updates[t["id"]] = resolved
        else:
            needs_llm.append(t)
    report["category_deterministic"] = len(cat_updates)
    report["category_via_haiku"] = len(needs_llm)

    if not apply:
        report["note"] = "dry-run — nothing written. Haiku classification also skipped."
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # ===== APPLY =====
    # 1. deadlines
    for uid, iso, raw in deadline_fixes:
        supabase_client.update_task(uid, deadline=iso, deadline_confidence="EXPLICIT")
    logger.info(f"deadlines repaired: {len(deadline_fixes)}")

    # 2. archive
    for uid in archive_ids:
        supabase_client.update_task(uid, status="archived")
    logger.info(f"archived: {len(archive_ids)}")

    # 3. categories (deterministic + Haiku)
    if needs_llm:
        llm_map = _classify_with_haiku(needs_llm, area_names)
        cat_updates.update(llm_map)
    for uid, cat in cat_updates.items():
        try:
            supabase_client.update_task(uid, category=cat)
        except Exception as e:
            logger.warning(f"category update failed for {uid}: {e}")
    logger.info(f"categories updated: {len(cat_updates)}")
    report["category_distribution"] = dict(Counter(
        (supabase_client.resolve_category(v) for v in cat_updates.values())
    ))

    # 4. sheet rebuild (fresh reads so the rebuild reflects all updates above)
    fresh = supabase_client.get_tasks(status=None, limit=2000)  # approved, no archived
    ok = await sheets_service.rebuild_tasks_sheet(fresh)
    logger.info(f"sheet rebuild: {ok} ({len(fresh)} active tasks)")

    # delete the leftover column L (index 11) if the grid still has it
    try:
        sid = sheets_service._get_sheet_id_by_name(settings.TASK_TRACKER_SHEET_ID, tab)
        sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            body={"requests": [{"deleteDimension": {"range": {
                "sheetId": sid, "dimension": "COLUMNS", "startIndex": 11, "endIndex": 12
            }}}]},
        ).execute()
        logger.info("column L removed")
    except Exception as e:
        logger.warning(f"column L delete skipped: {e}")

    # append the archived tasks to the Archive tab (records only; rows already
    # gone from the Tasks tab via the rebuild)
    archived_tasks = supabase_client.get_tasks(
        status="archived", limit=2000, include_pending=True
    )
    arch_rows = [
        {
            "priority": t.get("priority", "M"), "label": t.get("label", ""),
            "task": t.get("title", ""), "assignee": t.get("assignee", ""),
            "deadline": str(t.get("deadline") or ""), "status": "archived",
            "category": t.get("category", ""),
            "source_meeting": (t.get("meetings") or {}).get("title", "") if isinstance(t.get("meetings"), dict) else "",
            "created_date": str(t.get("created_at", ""))[:10], "id": t.get("id", ""),
            "urgency": t.get("urgency", "M"),
        }
        for t in archived_tasks
    ]
    if arch_rows:
        sheets_service._ensure_archive_tab()
        # append without deleting rows (rebuild already removed them)
        from services.google_sheets import TASK_TRACKER_HEADERS
        from datetime import datetime as _dt
        values = []
        for t in arch_rows:
            _row = [t["priority"], t["label"], t["task"], t["assignee"], t["deadline"],
                    t["status"], t["category"], t["source_meeting"], t["created_date"], t["id"]]
            if "urgency" in TASK_COLUMNS:
                _row.append(t["urgency"])
            _row.append(_dt.now().strftime("%Y-%m-%d"))
            values.append(_row)
        n = len(TASK_TRACKER_HEADERS) + 1
        sheets_service.service.spreadsheets().values().append(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            range=f"Archive!A:{chr(ord('A') + n - 1)}",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
        logger.info(f"archive tab: {len(values)} rows appended")

    # 5. reseed snapshots from the rebuilt sheet
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

    # apply formatting (category colors etc.)
    try:
        await sheets_service.format_task_tracker()
    except Exception as e:
        logger.warning(f"formatting skipped: {e}")

    supabase_client.log_action(
        "category_realign_2026_06", details={
            "deadline_fixes": len(deadline_fixes), "archived": len(archive_ids),
            "categories_updated": len(cat_updates), "snapshots": seeded,
        }, triggered_by="eyal",
    )
    report["done"] = True
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(apply=args.apply))
