"""
One-off cleanup: home the unassigned ACTIVE topics into Areas via a cheap Haiku
classification (name-matching can't — Area names don't share words with topic
names). Reversible (sets topic_threads.area_id). Prints a topic->area report.

Usage:
    python scripts/assign_homeless_topics.py            # dry-run (LLM + report, no writes)
    python scripts/assign_homeless_topics.py --apply     # apply assignments
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from core.llm import call_llm
from services.supabase_client import supabase_client

ACTIVE = ("active", "blocked", "pending_decision")


def _status(t):
    for s in (t.get("brief_json"), t.get("state_json")):
        if isinstance(s, dict) and s.get("current_status"):
            return str(s["current_status"]).lower()
    return "unknown"


def _snippet(bj, n=110):
    if not isinstance(bj, dict):
        return ""
    txt = bj.get("narrative") or bj.get("summary") or ""
    if not txt:
        facts = bj.get("facts") or []
        if facts and isinstance(facts[0], dict):
            txt = facts[0].get("text", "")
    return str(txt).replace("\n", " ")[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    areas = supabase_client.get_areas()
    rows = (supabase_client.client.table("topic_threads")
            .select("id,topic_name,area_id,brief_json,state_json")
            .is_("valid_to", "null").execute().data or [])
    homeless = [t for t in rows if not t.get("area_id") and _status(t) in ACTIVE]
    print(f"Areas: {len(areas)} | homeless active topics: {len(homeless)} | apply={args.apply}")
    if not homeless:
        return

    area_lines = "\n".join(f"{i+1}. {a['name']} — {_snippet(a.get('brief_json'), 90)}" for i, a in enumerate(areas))
    topic_lines = "\n".join(f"[{t['id']}] {t.get('topic_name','')} — {_snippet(t.get('brief_json'))}" for t in homeless)
    prompt = (
        "Assign each CropSight workstream TOPIC to the single best business AREA.\n\n"
        f"AREAS:\n{area_lines}\n0. NONE (no clear fit)\n\n"
        f"TOPICS:\n{topic_lines}\n\n"
        'Return ONLY JSON: {"assignments":[{"topic_id":"<id>","area":<number>}]} '
        "— one entry per topic, area is the AREA number above (0 = NONE)."
    )
    text, _ = call_llm(prompt, model=settings.model_simple, max_tokens=4000, call_site="assign_homeless_topics")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    data = json.loads(m.group(0)) if m else {"assignments": []}

    by_id = {t["id"]: t for t in homeless}
    applied, none_cnt = 0, 0
    print("\n=== ASSIGNMENTS ===")
    for a in data.get("assignments", []):
        tid, num = a.get("topic_id"), a.get("area")
        t = by_id.get(tid)
        if not t:
            continue
        if not num or num < 1 or num > len(areas):
            none_cnt += 1
            print(f"  NONE   {t.get('topic_name','')[:46]}")
            continue
        area = areas[num - 1]
        print(f"  {area['name'][:24]:24}  <-  {t.get('topic_name','')[:46]}")
        if args.apply:
            supabase_client.set_topic_area(tid, area["id"])
            applied += 1
    print(f"\nassigned: {applied if args.apply else '(dry-run)'} | NONE: {none_cnt} | total in LLM output: {len(data.get('assignments', []))}")


if __name__ == "__main__":
    main()
