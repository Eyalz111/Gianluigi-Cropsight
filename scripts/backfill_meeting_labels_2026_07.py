#!/usr/bin/env python3
"""
Backfill the Project (label) column on follow_up_meetings. [2026-07-22]

`follow_up_meetings.label` was added by migrate_project_area_hierarchy.sql and
nothing has ever populated it, so all 101 rows land on the Meetings tab with an
empty Project column. Everything else the tab shows already exists in the DB
(led_by 100/101, participants/agenda/prep on those that have them, source
meeting joined) — Project is the only genuine gap.

Project is also load-bearing: Area is derived THROUGH it
(canonical_projects.area_id), so a meeting with no label has no Area either.

Approach: Haiku matches each meeting's title + agenda + source-meeting title
against the canonical project vocabulary. It may answer null — a meeting that
genuinely belongs to no tracked project should stay blank rather than be forced
into the nearest one. Unmatched labels are NOT invented here; that is what the
auto-learn proposal loop is for.

DB SAFETY PROTOCOL
  - Dry run is the DEFAULT and prints every proposed mapping for review.
  - Only ever fills a BLANK label — never overwrites an existing one.
  - Snapshots to JSON; --rollback replays it.
  - Audit-logged.
  - Refreshes the Sheet cells afterwards so the tab matches the DB immediately
    (and re-seeds the snapshot, so reconcile still sees a clean no-op).

Usage:
    python scripts/backfill_meeting_labels_2026_07.py            # dry run
    python scripts/backfill_meeting_labels_2026_07.py --apply
    python scripts/backfill_meeting_labels_2026_07.py --rollback FILE
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backfill_snapshots")
_BATCH = 12


def _classify(meetings: list[dict], projects: list[dict]) -> dict:
    """meeting_id -> canonical project name (or None). Never raises."""
    from config.settings import settings
    from core.llm import call_llm

    vocab = [
        {"name": p["name"], "description": p.get("description", ""),
         "aliases": p.get("aliases") or []}
        for p in projects
    ]
    out: dict[str, str] = {}

    for i in range(0, len(meetings), _BATCH):
        batch = meetings[i:i + _BATCH]
        items = []
        for m in batch:
            mi = m.get("meetings") if isinstance(m.get("meetings"), dict) else {}
            items.append({
                "id": m["id"],
                "meeting": m.get("title", ""),
                "agenda": (m.get("agenda_items") or [])[:3],
                "from_meeting": (mi or {}).get("title", ""),
            })

        prompt = f"""Assign each follow-up meeting to ONE canonical CropSight project, or null.

CANONICAL PROJECTS:
{json.dumps(vocab, indent=2)}

FOLLOW-UP MEETINGS:
{json.dumps(items, indent=2, ensure_ascii=False)}

Rules:
- Use a project's EXACT "name" string when the meeting clearly belongs to it.
- Match on what the meeting is ABOUT, not on who attends.
- Return null when no project genuinely fits. A wrong assignment is worse than
  none: Area is derived from the project, so a bad label misfiles the meeting
  in every downstream view. Do NOT invent new project names.

Return ONLY a JSON array: [{{"id": "...", "project": "Moldova Pilot"}}, {{"id": "...", "project": null}}]"""

        try:
            response, _ = call_llm(
                prompt=prompt,
                model=settings.model_simple,
                max_tokens=1500,
                call_site="backfill_meeting_labels",
            )
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            start, end = text.find("["), text.rfind("]")
            parsed = json.loads(text[start:end + 1]) if start >= 0 else []
            valid = {p["name"] for p in projects}
            for row in parsed:
                proj = row.get("project")
                if proj and proj in valid:          # never trust a hallucinated name
                    out[row["id"]] = proj
            logger.info(f"  classified {min(i + _BATCH, len(meetings))}/{len(meetings)}")
        except Exception as e:
            logger.warning(f"  batch at {i} failed ({e}) — those stay unlabelled")
    return out


def build_plan() -> list[dict]:
    from services.supabase_client import supabase_client as sc

    projects = sc.get_canonical_projects(status="active")
    if not projects:
        logger.error("No canonical projects — refusing to plan.")
        return []
    meetings = sc.list_follow_up_meetings(limit=2000, include_pending=True)
    blank = [m for m in meetings if not (m.get("label") or "").strip()]
    logger.info(f"{len(blank)} of {len(meetings)} meetings have no Project")
    if not blank:
        return []

    mapping = _classify(blank, projects)
    return [
        {"id": m["id"], "title": m.get("title", ""), "label": mapping[m["id"]]}
        for m in blank if mapping.get(m["id"])
    ]


def _snapshot(plan: list[dict]) -> str:
    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SNAPSHOT_DIR, f"meeting_labels_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    logger.info(f"Snapshot: {path}")
    return path


async def _refresh_sheet() -> int:
    """Rewrite the Project cells + re-seed snapshots so the tab matches the DB."""
    from config.settings import settings
    from services.google_sheets import sheets_service, MEETING_COLUMNS, MEETING_TAB_NAME
    from services.supabase_client import supabase_client as sc

    rows = await sheets_service.get_all_meetings()
    if not rows:
        logger.warning("Meetings tab read returned 0 rows — skipping Sheet refresh.")
        return 0
    db = {m["id"]: m for m in sc.list_follow_up_meetings(limit=2000, include_pending=True)}
    writes = []
    for r in rows:
        rid = str(r.get("id") or "").strip()
        m = db.get(rid)
        if not m or not r.get("row_number"):
            continue
        want = (m.get("label") or "").strip()
        if want and want != (r.get("label") or "").strip():
            writes.append({
                "range": f"'{MEETING_TAB_NAME}'!{MEETING_COLUMNS['label']}{r['row_number']}",
                "values": [[want]],
            })
    if writes:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().batchUpdate(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                body={"valueInputOption": "RAW", "data": writes},
            )
        )
    # Re-seed snapshots from the sheet so reconcile still sees snap == sheet == db.
    for r in await sheets_service.get_all_meetings():
        rid = str(r.get("id") or "").strip()
        if rid:
            sc.upsert_meeting_snapshot(
                rid, r.get("row_number"), r.get("title"), r.get("label"),
                r.get("led_by"), r.get("proposed_date"), r.get("participants"),
                r.get("status"))
    return len(writes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", metavar="FILE")
    args = ap.parse_args()

    from services.supabase_client import supabase_client as sc

    if args.rollback:
        with open(args.rollback, encoding="utf-8") as f:
            plan = json.load(f)
        for row in plan:
            try:
                sc.client.table("follow_up_meetings").update(
                    {"label": None}).eq("id", row["id"]).execute()
            except Exception as e:
                logger.error(f"rollback failed for {row['id']}: {e}")
        asyncio.run(_refresh_sheet())
        logger.info(f"Cleared {len(plan)} label(s)")
        return 0

    plan = build_plan()
    if not plan:
        logger.info("Nothing to label.")
        return 0

    by_proj: dict[str, list[str]] = {}
    for r in plan:
        by_proj.setdefault(r["label"], []).append(r["title"])
    print("\n=== PROPOSED PROJECT ASSIGNMENTS ===")
    for proj, titles in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  {proj}  ({len(titles)})")
        for t in titles[:6]:
            print(f"      {t[:78]}")
        if len(titles) > 6:
            print(f"      ... +{len(titles) - 6} more")
    print(f"\nTotal to label: {len(plan)}  (the rest stay blank — no project fits)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    path = _snapshot(plan)
    n = 0
    for row in plan:
        try:
            sc.client.table("follow_up_meetings").update(
                {"label": row["label"]}).eq("id", row["id"]).execute()
            n += 1
        except Exception as e:
            logger.error(f"FAILED {row['id']}: {e}")
    sc.log_action(action="meeting_label_backfill",
                  details={"labelled": n, "total": len(plan)}, triggered_by="eyal")
    cells = asyncio.run(_refresh_sheet())
    print(f"\nLabelled {n}/{len(plan)}; refreshed {cells} Sheet cell(s).")
    print(f"Rollback:  python scripts/backfill_meeting_labels_2026_07.py --rollback {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
