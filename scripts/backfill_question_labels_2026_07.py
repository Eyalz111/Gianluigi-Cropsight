#!/usr/bin/env python3
"""
Backfill the Project (label) column on open_questions. [2026-07-22]

Sibling of backfill_meeting_labels_2026_07.py. `open_questions.label` was added
by migrate_project_area_hierarchy.sql and never populated, so all 73 open
questions render with a blank Project — and because Area is derived THROUGH the
project, the Areas tab reported "General: 73 open questions" while every real
area showed 0. The rollup was honest about the data and useless as a view.

Haiku matches each question against the canonical project vocabulary and may
answer null; returned names are validated against the live vocabulary before
use, so a hallucinated project can never be written.

Only fills BLANK labels, never overwrites. Dry run by default. Snapshot +
--rollback. Audit-logged.

Usage:
    python scripts/backfill_question_labels_2026_07.py            # dry run
    python scripts/backfill_question_labels_2026_07.py --apply
    python scripts/backfill_question_labels_2026_07.py --rollback FILE
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

_SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backfill_snapshots")
_BATCH = 15


def _classify(questions: list[dict], projects: list[dict]) -> dict:
    from config.settings import settings
    from core.llm import call_llm

    vocab = [{"name": p["name"], "description": p.get("description", ""),
              "aliases": p.get("aliases") or []} for p in projects]
    valid = {p["name"] for p in projects}
    out: dict[str, str] = {}

    for i in range(0, len(questions), _BATCH):
        batch = questions[i:i + _BATCH]
        items = [{"id": q["id"], "question": (q.get("question") or "")[:400],
                  "raised_by": q.get("raised_by", "")} for q in batch]
        prompt = f"""Assign each open question to ONE canonical CropSight project, or null.

CANONICAL PROJECTS:
{json.dumps(vocab, indent=2)}

OPEN QUESTIONS:
{json.dumps(items, indent=2, ensure_ascii=False)}

Rules:
- Use a project's EXACT "name" string when the question is clearly about it.
- Match on subject matter, not on who raised it.
- Return null when no project genuinely fits. Area is derived from the project,
  so a wrong label misfiles the question in every rollup — worse than blank.
- Do NOT invent project names.

Return ONLY a JSON array: [{{"id": "...", "project": "Moldova Pilot"}}, {{"id": "...", "project": null}}]"""
        try:
            response, _ = call_llm(prompt=prompt, model=settings.model_simple,
                                   max_tokens=1500, call_site="backfill_question_labels")
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            start, end = text.find("["), text.rfind("]")
            for row in (json.loads(text[start:end + 1]) if start >= 0 else []):
                if row.get("project") in valid:
                    out[row["id"]] = row["project"]
            logger.info(f"  classified {min(i + _BATCH, len(questions))}/{len(questions)}")
        except Exception as e:
            logger.warning(f"  batch at {i} failed ({e}) — those stay unlabelled")
    return out


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
            sc.client.table("open_questions").update({"label": None}).eq("id", row["id"]).execute()
        logger.info(f"Cleared {len(plan)} label(s)")
        return 0

    projects = sc.get_canonical_projects(status="active")
    if not projects:
        logger.error("No canonical projects — refusing to plan.")
        return 1
    rows = (
        sc.client.table("open_questions")
        .select("id, question, raised_by, label, status")
        .eq("status", "open").limit(1000).execute().data or []
    )
    blank = [q for q in rows if not (q.get("label") or "").strip()]
    logger.info(f"{len(blank)} of {len(rows)} open questions have no Project")
    if not blank:
        logger.info("Nothing to label.")
        return 0

    mapping = _classify(blank, projects)
    plan = [{"id": q["id"], "question": (q.get("question") or "")[:90],
             "label": mapping[q["id"]]} for q in blank if mapping.get(q["id"])]
    if not plan:
        logger.info("No confident matches — everything stays blank.")
        return 0

    by_proj: dict[str, list[str]] = {}
    for r in plan:
        by_proj.setdefault(r["label"], []).append(r["question"])
    print("\n=== PROPOSED PROJECT ASSIGNMENTS ===")
    for proj, qs in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  {proj}  ({len(qs)})")
        for q in qs[:4]:
            print(f"      {q}")
        if len(qs) > 4:
            print(f"      ... +{len(qs) - 4} more")
    print(f"\nTotal to label: {len(plan)} of {len(blank)}  (rest stay blank)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SNAPSHOT_DIR, f"question_labels_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)

    n = 0
    for row in plan:
        try:
            sc.client.table("open_questions").update(
                {"label": row["label"]}).eq("id", row["id"]).execute()
            n += 1
        except Exception as e:
            logger.error(f"FAILED {row['id']}: {e}")
    sc.log_action(action="question_label_backfill",
                  details={"labelled": n, "total": len(plan)}, triggered_by="eyal")
    print(f"\nLabelled {n}/{len(plan)}.")
    print(f"Rollback:  python scripts/backfill_question_labels_2026_07.py --rollback {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
