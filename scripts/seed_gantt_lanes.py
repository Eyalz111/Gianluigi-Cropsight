"""
PR-A: mirror the EXISTING Gantt's lanes into gantt_rows (DB only, no sheet writes).

Reads gantt_schema, and for each work subsection under an AREA section creates a
gantt_rows lane (sheet_name, area_id, lane_type, lane_index, owner=NULL,
display_order=row_number, topic_id NULL). Gives linkage/read-back/nudge a stable
DB handle per (Area x lane). No structure change; pure mirror.

Usage:
    python scripts/seed_gantt_lanes.py            # dry-run: print the lane table
    python scripts/seed_gantt_lanes.py --apply     # upsert gantt_rows
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from services.supabase_client import supabase_client

# sections that are NOT areas (skip for the area-lane mirror)
_NON_AREA = ("strategic milestones", "operational rules", "management")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _lane_type(subsection):
    s = (subsection or "").lower()
    if "planning" in s:
        return "planning"
    if s.strip().startswith("execution"):
        return "execution"
    if "meeting" in s:
        return "meetings"
    if "human resources" in s or s.strip() == "hr":
        return "hr"
    return "execution"  # area extras (Marketing, Finance & Admin, etc.) -> execution bucket


def _area_for(section, areas):
    ns = _norm(section)
    if any(k in ns for k in _NON_AREA):
        return None
    for a in areas:
        for cand in (a.get("gantt_section"), a.get("name")):
            nc = _norm(cand)
            if nc and (nc == ns or nc in ns or ns in nc or set(nc.split()) & set(ns.split())):
                return a["id"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rows = (supabase_client.client.table("gantt_schema")
            .select("section,subsection,row_number,protected,notes").execute().data or [])
    rows = [r for r in rows if r.get("row_number") and not (r.get("section") or "").startswith("_")]
    rows.sort(key=lambda r: r["row_number"])
    areas = supabase_client.get_areas()
    area_name = {a["id"]: a["name"] for a in areas}
    tab = settings.GANTT_MAIN_TAB

    lanes, idx = [], {}  # idx[(area_id,lane_type)] -> running index
    for r in rows:
        notes = r.get("notes") or ""
        if notes.startswith("section_header") or not (r.get("subsection") or "").strip():
            continue
        aid = _area_for(r.get("section"), areas)
        if not aid:
            continue
        lt = _lane_type(r.get("subsection"))
        key = (aid, lt)
        idx[key] = idx.get(key, 0) + 1
        lanes.append({"sheet_name": tab, "area_id": aid, "lane_type": lt, "lane_index": idx[key],
                      "owner": None, "display_order": r["row_number"]})

    # report
    from collections import defaultdict
    by_area = defaultdict(list)
    for ln in lanes:
        by_area[ln["area_id"]].append(ln)
    print(f"Mirroring {len(lanes)} lanes across {len(by_area)} areas (tab '{tab}'), apply={args.apply}:")
    for aid, alanes in by_area.items():
        types = ", ".join(f"{l['lane_type']}#{l['lane_index']}(r{l['display_order']})" for l in alanes)
        print(f"  {area_name.get(aid,'?')[:30]:30} | {types}")

    if not args.apply:
        print("\n[dry-run] no DB writes")
        return

    n = 0
    for ln in lanes:
        existing = (supabase_client.client.table("gantt_rows").select("id")
                    .eq("sheet_name", ln["sheet_name"]).eq("area_id", ln["area_id"])
                    .eq("lane_type", ln["lane_type"]).eq("lane_index", ln["lane_index"])
                    .is_("valid_to", "null").execute().data or [])
        if existing:
            supabase_client.client.table("gantt_rows").update(
                {"display_order": ln["display_order"]}).eq("id", existing[0]["id"]).execute()
        else:
            supabase_client.client.table("gantt_rows").insert(ln).execute()
        n += 1
    print(f"\nupserted {n} lanes")
    try:
        supabase_client.log_action("gantt_lanes_seeded", details={"lanes": n, "tab": tab}, triggered_by="system")
    except Exception:
        pass


if __name__ == "__main__":
    main()
