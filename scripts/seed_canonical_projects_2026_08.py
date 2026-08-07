"""
Seed the canonical project list Eyal defined on 2026-08-07.

    python scripts/seed_canonical_projects_2026_08.py            # DRY RUN (default)
    python scripts/seed_canonical_projects_2026_08.py --apply
    python scripts/seed_canonical_projects_2026_08.py --rollback <snapshot.json>

WHY: `canonical_projects` held 10 seed-era rows that nobody curated, while the
real project dimension lived nowhere — tasks associate by the free-text `label`,
and only 12 of 355 approved tasks carried a label matching a project. The ~226
distinct labels are TOPICS, one task each; they were never going to be the
project list. Eyal defined the list top-down instead.

FOUR RULES THIS SCRIPT OBEYS
  1. DRY RUN BY DEFAULT. Nothing is written without --apply.
  2. RENAME, never delete-and-recreate. rename_canonical_project keeps the old
     name as an alias AND backfills `label` across tasks/decisions/
     open_questions/follow_up_meetings, so history follows the rename.
  3. RETIRE, never delete. A deleted project orphans the provenance of every
     row that referenced it. Retired rows keep their aliases so resolve_label
     still canonicalizes historical labels.
  4. UNMAPPED IS REPORTED, NEVER GUESSED. Anything the plan cannot place
     cleanly is printed for Eyal to decide.

THE PROJECTS TAB MUST BE REWRITTEN IN THE SAME RUN. reconcile_projects
(processors/sheets_sync.py:1652) compares the sheet's name against the DB name
with NO snapshot, so it reads any DB-side rename as a sheet-side rename in
reverse and undoes it on the next 30-minute tick. Rebuilding the tab from the
DB at the end of the run is what makes these renames survive.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.supabase_client import supabase_client  # noqa: E402

PROD = "PRODUCT & TECHNOLOGY"
SALES = "SALES & BUSINESS DEVELOPMENT"
FUND = "FUNDRAISING & INVESTOR RELATIONS"
LEGAL = "LEGAL, CORPORATE & FINANCE"
TEAM = "TEAM & HUMAN RESOURCES"
DELIV = "CLIENT DELIVERY & OPERATIONS"

# Eyal's list, 2026-08-07. `order` drives the block order within an area tab.
#
# "Others" buckets are deliberate: untagged work lands there instead of forcing
# a fake project or vanishing. They need a periodic review or they become the
# new 226 — the weekly pass surfaces their size.
#
# NOTE the Others names are AREA-QUALIFIED. canonical_projects.name is UNIQUE
# (migrate_phase10.sql), and add_canonical_project is idempotent BY NAME — five
# rows literally called "Others" would collapse into ONE row that ping-pongs
# between areas as each seed pass re-pointed its area_id. Each area tab shows
# only its own bucket, so the qualifier is invisible in normal use.
TARGET = [
    (PROD,  "Product V1",                     10),
    (PROD,  "MVP Delivery & Productization",  20),
    (PROD,  "CropSight Accuracy Model",       30),
    (PROD,  "Cloud Infrastructure",           40),
    (PROD,  "Others — Product & Technology",  90),

    (SALES, "Italy",                          10),
    (SALES, "Governments & WFP",              20),
    (SALES, "Commodity Trading Vertical",     30),
    (SALES, "Insurance Vertical",             40),
    (SALES, "Marketing & Brand",              50),
    (SALES, "Others & Nostro",                90),   # Eyal's own name for this one

    (FUND,  "Fundraising & Investors",        10),
    # Kept as its own project (Eyal, 2026-08-07): pipeline building is distinct
    # from the raise itself. Sits next to it rather than merging into it.
    (FUND,  "Investor Outreach",              15),
    (FUND,  "Grants & Contributions",         20),
    (FUND,  "Investors Materials",            30),
    (FUND,  "Business Plan",                  40),
    (FUND,  "Others — Fundraising",           90),

    (LEGAL, "Legal",                          10),
    (LEGAL, "Corporate",                      20),
    (LEGAL, "Finance",                        30),

    # Eyal: "don't name any projects yet" for these two areas. An Others bucket
    # is not a named project — it is where their work lands so nothing is
    # homeless. Flagged in the plan so he can veto.
    (TEAM,  "Others — Team & HR",             90),
    (DELIV, "Others — Client Delivery",       90),
]

# Existing row -> new name. rename_canonical_project folds the old name into
# aliases and backfills every `label` reference.
RENAMES = {
    "Website & Marketing": "Marketing & Brand",
    "EU Grant": "Grants & Contributions",
    "Pre-Seed Fundraising": "Fundraising & Investors",
}

RETIRE = {
    "Moldova Pilot": "superseded by the curated project list 2026-08-07",
    "Operational Tooling": "Gianluigi itself — not review material",
    # Eyal 2026-08-07: TEAM & HUMAN RESOURCES gets no named projects yet, so the
    # legacy row goes and the area keeps only its Others bucket. Its 2 decisions
    # still resolve — retire keeps the row and its aliases.
    "Team & HR": "area has no named projects yet; superseded by Others — Team & HR",
}

# Kept active but NOT in Eyal's list. Reported, never touched.
EXPECT_UNMAPPED_NOTE: dict = {}


def build_plan() -> dict:
    areas = {a["name"]: a["id"] for a in supabase_client.get_areas()}
    existing = supabase_client.get_canonical_projects(status=None)
    by_name = {(p.get("name") or "").strip().lower(): p for p in existing}

    plan = {"creates": [], "renames": [], "retires": [], "updates": [],
            "unmapped": [], "conflicts": []}

    # Renames first — they change what "already exists" means for the creates.
    renamed_to = {}
    for old, new in RENAMES.items():
        row = by_name.get(old.lower())
        if not row:
            plan["unmapped"].append((old, "rename source not found — already renamed?"))
            continue
        clash = by_name.get(new.lower())
        if clash and clash["id"] != row["id"]:
            plan["conflicts"].append(
                (old, new, "target name already exists — would need a merge, skipping")
            )
            continue
        plan["renames"].append((row["id"], row["name"], new))
        renamed_to[new.lower()] = row["id"]

    for name, reason in RETIRE.items():
        row = by_name.get(name.lower())
        if not row:
            plan["unmapped"].append((name, "retire target not found"))
        elif row.get("status") == "retired":
            pass  # already there — idempotent
        else:
            plan["retires"].append((row["id"], row["name"], reason))

    # Creates / area+order updates.
    for area_name, proj_name, order in TARGET:
        area_id = areas.get(area_name)
        if not area_id:
            plan["unmapped"].append((proj_name, f"area '{area_name}' not found"))
            continue
        key = proj_name.lower()
        row = by_name.get(key)
        pid = row["id"] if row else renamed_to.get(key)
        if pid:
            plan["updates"].append((pid, proj_name, area_name, order))
        else:
            plan["creates"].append((proj_name, area_name, area_id, order))

    # Anything active that the plan never touches.
    touched = {n.lower() for _, n, _, _ in plan["updates"]}
    touched |= {n.lower() for _, _, n in plan["renames"]}
    touched |= {n.lower() for _, n, _ in plan["retires"]}
    touched |= {n.lower() for n, _, _, _ in plan["creates"]}
    for p in existing:
        nm = (p.get("name") or "").strip()
        if p.get("status") == "retired":
            continue
        if nm.lower() in touched or nm in RENAMES:
            continue
        plan["unmapped"].append((nm, EXPECT_UNMAPPED_NOTE.get(nm, "not in Eyal's list")))
    return plan


def print_plan(plan: dict) -> None:
    print(f"  RENAME  {len(plan['renames'])}")
    for _, old, new in plan["renames"]:
        print(f"      {old}  ->  {new}")
    print(f"  RETIRE  {len(plan['retires'])}   (status only — row + aliases kept)")
    for _, name, reason in plan["retires"]:
        print(f"      {name}  ({reason})")
    print(f"  CREATE  {len(plan['creates'])}")
    for name, area, _, order in plan["creates"]:
        print(f"      [{order:>2}] {name:<32} {area}")
    print(f"  SET AREA/ORDER on existing  {len(plan['updates'])}")
    for _, name, area, order in plan["updates"]:
        print(f"      [{order:>2}] {name:<32} {area}")
    if plan["conflicts"]:
        print(f"\n  !! CONFLICTS  {len(plan['conflicts'])} — NOT applied:")
        for old, new, why in plan["conflicts"]:
            print(f"      {old} -> {new}: {why}")
    if plan["unmapped"]:
        print(f"\n  ?? UNMAPPED  {len(plan['unmapped'])} — reported, never guessed:")
        for name, why in plan["unmapped"]:
            print(f"      {name}: {why}")


def snapshot(path: Path) -> None:
    rows = supabase_client.get_canonical_projects(status=None)
    path.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    print(f"\n  snapshot -> {path}")


def apply_plan(plan: dict) -> dict:
    done = {"renamed": 0, "retired": 0, "created": 0, "updated": 0, "errors": []}

    for pid, old, new in plan["renames"]:
        try:
            supabase_client.rename_canonical_project(pid, new)
            done["renamed"] += 1
            print(f"    renamed {old} -> {new}")
        except Exception as e:
            done["errors"].append(f"rename {old}: {e}")

    for pid, name, reason in plan["retires"]:
        try:
            supabase_client.retire_canonical_project(pid, reason)
            done["retired"] += 1
            print(f"    retired {name}")
        except Exception as e:
            done["errors"].append(f"retire {name}: {e}")

    for name, area_name, area_id, order in plan["creates"]:
        try:
            row = supabase_client.add_canonical_project(name=name, area_id=area_id)
            pid = (row or {}).get("id")
            if pid:
                supabase_client.update_canonical_project(
                    pid, area_id=area_id, display_order=order)
            done["created"] += 1
            print(f"    created {name} ({area_name})")
        except Exception as e:
            done["errors"].append(f"create {name}: {e}")

    for pid, name, area_name, order in plan["updates"]:
        try:
            areas = {a["name"]: a["id"] for a in supabase_client.get_areas()}
            supabase_client.update_canonical_project(
                pid, area_id=areas.get(area_name), display_order=order)
            done["updated"] += 1
        except Exception as e:
            done["errors"].append(f"update {name}: {e}")
    return done


async def preflight_sheet() -> tuple:
    """Prove the Projects tab is REACHABLE before any DB write.

    Learned the hard way on the first run: the tab rebuild is the safety net
    that stops reconcile reverting the renames, but it ran LAST and returned a
    quiet False — after every rename and retire had already been applied. The
    cause was a stale local .env still pointing TASK_TRACKER_SHEET_ID at the
    pre-migration spreadsheet, which the org account cannot open (403).

    Failing up front turns that into a no-op instead of a half-applied seed.
    """
    from config.settings import settings
    from services.google_sheets import sheets_service
    sid = settings.TASK_TRACKER_SHEET_ID
    if not sid:
        return False, "TASK_TRACKER_SHEET_ID is not set"
    if not await sheets_service.authenticate():
        return False, "Google Sheets authentication failed"
    try:
        rows = await sheets_service.get_all_projects()
    except Exception as e:
        return False, f"cannot read the Projects tab of {sid}: {e}"
    return True, f"Projects tab reachable ({len(rows)} rows) on {sid}"


async def rebuild_projects_tab() -> bool:
    """Push the DB state onto the Projects tab.

    NOT optional. reconcile_projects has no snapshot rail — it compares the
    sheet's name against the DB's and treats a difference as a sheet-side
    rename, so a stale tab would rename everything back within 30 minutes.
    """
    from services.google_sheets import sheets_service
    if not await sheets_service.authenticate():
        print("    !! Sheets auth failed — Projects tab NOT rebuilt.")
        print("       Renames WILL be reverted by reconcile within ~30 min.")
        return False
    projects = supabase_client.get_canonical_projects(status=None)
    areas = {a["id"]: a.get("name", "") for a in supabase_client.get_areas()}
    ok = await sheets_service.rebuild_projects_sheet(projects, areas)
    print(f"    Projects tab rebuilt with {len(projects)} row(s): {ok}")
    return ok


def rollback(path: Path) -> None:
    rows = json.loads(path.read_text(encoding="utf-8"))
    print(f"ROLLBACK from {path} — {len(rows)} project row(s)")
    for r in rows:
        try:
            supabase_client.client.table("canonical_projects").update({
                "name": r.get("name"), "status": r.get("status"),
                "area_id": r.get("area_id"), "aliases": r.get("aliases"),
                "retired_at": r.get("retired_at"),
                "retired_reason": r.get("retired_reason"),
                "display_order": r.get("display_order"),
            }).eq("id", r["id"]).execute()
        except Exception as e:
            print(f"  !! {r.get('name')}: {e}")
    print("  done — projects created AFTER the snapshot are left in place")


def main() -> None:
    import asyncio

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default = dry run)")
    ap.add_argument("--rollback", metavar="SNAPSHOT.json")
    ap.add_argument("--skip-sheet", action="store_true",
                    help="do NOT rebuild the Projects tab (renames will revert)")
    args = ap.parse_args()

    if args.rollback:
        rollback(Path(args.rollback))
        return

    plan = build_plan()
    print("APPLY" if args.apply else "DRY RUN (nothing written) — re-run with --apply")
    print("=" * 72)
    print_plan(plan)

    # Preflight BEFORE anything is written. The tab rebuild is what stops
    # reconcile reverting the renames, so an unreachable sheet must abort the
    # whole run rather than leave a half-applied seed behind.
    if not args.skip_sheet:
        ok, why = asyncio.run(preflight_sheet())
        print(f"\n  PREFLIGHT: {why}")
        if not ok:
            print("  ABORTING — nothing written. Fix the sheet access first, or")
            print("  re-run with --skip-sheet if you will rebuild the tab yourself.")
            return

    if not args.apply:
        print("\n  Nothing was written.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot(Path(__file__).with_name(f"_projects_snapshot_{stamp}.json"))
    print("\nAPPLYING")
    done = apply_plan(plan)
    print(f"\n  {done}")

    if args.skip_sheet:
        print("\n  !! Projects tab NOT rebuilt (--skip-sheet). Renames will be")
        print("     reverted by reconcile within ~30 minutes.")
    else:
        print("\nREBUILDING PROJECTS TAB (or renames revert within ~30 min)")
        asyncio.run(rebuild_projects_tab())

    live = supabase_client.get_canonical_projects(status="active")
    print(f"\n  active projects now: {len(live)}")


if __name__ == "__main__":
    main()
