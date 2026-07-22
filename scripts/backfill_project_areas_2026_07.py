#!/usr/bin/env python3
"""
Assign each canonical project to an Area, then propagate to topic threads.
[2026-07-22]

This is the step that ACTIVATES the whole hierarchy. topic_threading already
writes topic->area ('belongs_to') and decision/task->topic ('advances') links at
extraction, but the chain was dead in practice: 0 of 50 topic_threads had an
area_id, so the `if area_id:` guard at topic_threading.py never fired and no
belongs_to link was ever created.

Design (Eyal, 2026-07-22): AREA IS STORED ONCE, ON THE PROJECT. Decisions, open
questions and follow-up meetings derive their Area through their project rather
than carrying a column each — so reclassifying a project moves everything under
it in a single edit instead of a four-table backfill that drifts apart.

    Area (7)  <-  Project (canonical_projects.area_id)  <-  tasks/decisions/...

DB SAFETY PROTOCOL
  - No deletes. Sets area_id where it is currently NULL; never overwrites an
    existing assignment unless --force.
  - Dry run is the DEFAULT.
  - Snapshots before/after to JSON; --rollback replays it.
  - Audit-logged.

Requires: scripts/migrate_project_area_hierarchy.sql

Usage:
    python scripts/backfill_project_areas_2026_07.py            # dry run
    python scripts/backfill_project_areas_2026_07.py --apply
    python scripts/backfill_project_areas_2026_07.py --rollback FILE
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Seed mapping for the curated vocabulary. Deterministic and reviewable — with
# ~10 projects an LLM adds nothing but nondeterminism. Anything not listed is
# reported as UNMAPPED for Eyal to place rather than guessed into 'General'.
SEED_PROJECT_AREA = {
    "business plan": "FUNDRAISING & INVESTOR RELATIONS",
    "eu grant": "FUNDRAISING & INVESTOR RELATIONS",
    "investor outreach": "FUNDRAISING & INVESTOR RELATIONS",
    "pre-seed fundraising": "FUNDRAISING & INVESTOR RELATIONS",
    "moldova pilot": "CLIENT DELIVERY & OPERATIONS",
    "operational tooling": "PRODUCT & TECHNOLOGY",
    "product v1": "PRODUCT & TECHNOLOGY",
    "satyield accuracy model": "PRODUCT & TECHNOLOGY",
    "website & marketing": "SALES & BUSINESS DEVELOPMENT",
    "team & hr": "TEAM & HUMAN RESOURCES",
}

_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backfill_snapshots")


def build_plan(force: bool = False) -> dict:
    from services.supabase_client import supabase_client as sc

    areas = sc.get_areas()
    if not areas:
        logger.error("No areas found — refusing to plan.")
        return {}
    by_name = {(a.get("name") or "").strip().lower(): a for a in areas}

    projects = sc.get_canonical_projects(status="active")
    plan, unmapped, already = [], [], []
    for p in projects:
        name = (p.get("name") or "").strip()
        if p.get("area_id") and not force:
            already.append(name)
            continue
        target = SEED_PROJECT_AREA.get(name.lower())
        if not target:
            unmapped.append(name)
            continue
        area = by_name.get(target.lower())
        if not area:
            logger.warning(f"Area {target!r} not found for project {name!r}")
            unmapped.append(name)
            continue
        plan.append({
            "project_id": p["id"], "project": name,
            "area_id": area["id"], "area": area["name"],
            "before_area_id": p.get("area_id"),
        })
    return {"plan": plan, "unmapped": unmapped, "already": already}


def _snapshot(plan: list[dict]) -> str:
    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SNAPSHOT_DIR, f"project_areas_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    logger.info(f"Snapshot written: {path}")
    return path


def apply_plan(plan: list[dict]) -> dict:
    """Set project.area_id, then propagate to matching topic_threads."""
    from services.supabase_client import supabase_client as sc

    out = {"projects": 0, "topics": 0}
    for row in plan:
        try:
            sc.client.table("canonical_projects").update(
                {"area_id": row["area_id"]}).eq("id", row["project_id"]).execute()
            out["projects"] += 1
            # Propagate to the topic thread carrying this project's name, so the
            # `if area_id:` guard in topic_threading finally fires and
            # topic->area belongs_to links start being written.
            sc.client.table("topic_threads").update(
                {"area_id": row["area_id"]}
            ).eq("topic_name_lower", row["project"].lower()).is_("valid_to", "null").execute()
            out["topics"] += 1
            sc.log_action(
                action="project_area_assigned",
                details={"project": row["project"], "area": row["area"],
                         "before_area_id": row["before_area_id"]},
                triggered_by="eyal",
            )
        except Exception as e:
            logger.error(f"FAILED {row['project']}: {e}")
    return out


def rollback(path: str) -> int:
    from services.supabase_client import supabase_client as sc

    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    n = 0
    for row in plan:
        try:
            sc.client.table("canonical_projects").update(
                {"area_id": row["before_area_id"]}).eq("id", row["project_id"]).execute()
            n += 1
        except Exception as e:
            logger.error(f"ROLLBACK FAILED {row['project']}: {e}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true", help="reassign already-mapped projects")
    ap.add_argument("--rollback", metavar="FILE")
    args = ap.parse_args()

    if args.rollback:
        logger.info(f"Rolled back {rollback(args.rollback)} project(s)")
        return 0

    result = build_plan(force=args.force)
    if not result:
        return 1
    plan, unmapped, already = result["plan"], result["unmapped"], result["already"]

    print("\n=== PLANNED PROJECT -> AREA ===")
    for r in plan:
        print(f"  {r['project']:<32} -> {r['area']}")
    if already:
        print(f"\n  ({len(already)} already assigned, skipped: {', '.join(already)})")
    if unmapped:
        print("\n=== UNMAPPED — needs your call, not guessed ===")
        for n in unmapped:
            print(f"  {n}")
    print(f"\nTotal to assign: {len(plan)}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    path = _snapshot(plan)
    out = apply_plan(plan)
    print(f"\nAssigned {out['projects']} project(s); propagated to {out['topics']} topic thread(s).")
    print(f"Rollback:  python scripts/backfill_project_areas_2026_07.py --rollback {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
