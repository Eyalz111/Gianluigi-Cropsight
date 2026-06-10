"""One-time backfill: fill `urgency` + `area` on existing tasks. NEVER deadlines.

Run once after applying migrate_phase_operational_floor.sql + landing PR3, BEFORE
the PR6 output flip (else history reads as urgency='M'/area='non-area' everywhere).

- URGENCY is derived DETERMINISTICALLY from each task's existing deadline /
  deadline_confidence / title: an EXPLICIT deadline <=3 days/past -> H, <=14 days
  -> M, else L; no usable deadline + urgency language in the title -> H; else M.
- AREA is matched CONSERVATIVELY against the live Gantt areas, biased by the task's
  label/category, then a phrase match on the title; unmatched -> 'non-area' (never
  guessed — Eyal corrects those via the sheet/MCP).
- DEADLINES ARE NEVER TOUCHED (the firm no-invented-dates guardrail). The update
  payload provably contains only urgency/area_id/area_label.

Idempotent: re-runs produce 0 changes (it only writes when a value would change).
Dry-run by default; --apply to write.

Usage:
    python scripts/backfill_task_urgency_area.py            # dry-run preview
    python scripts/backfill_task_urgency_area.py --apply    # write
"""
import argparse
import os
import sys
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.supabase_client import supabase_client

_URGENT_WORDS = (
    "asap", "urgent", "blocking", "right now", "today", "immediately", "critical",
)


def _derive_urgency(task: dict) -> str:
    """Deterministic urgency H/M/L from existing fields. Never reads/writes a deadline."""
    deadline = task.get("deadline")
    if deadline and task.get("deadline_confidence") == "EXPLICIT":
        try:
            days = (date.fromisoformat(str(deadline)[:10]) - date.today()).days
            if days <= 3:
                return "H"
            if days <= 14:
                return "M"
            return "L"
        except (ValueError, TypeError):
            pass
    title = (task.get("title") or "").lower()
    if any(w in title for w in _URGENT_WORDS):
        return "H"
    return "M"


def _resolve_area(task: dict, by_name: dict) -> tuple[str | None, str]:
    """(area_id, area_label) — label/category exact match, then a title phrase match,
    else (None, 'non-area'). Conservative: never guesses."""
    for hint in (task.get("label"), task.get("category")):
        m = by_name.get((hint or "").strip().lower())
        if m:
            return m.get("id"), m.get("name")
    text = (task.get("title") or "").lower()
    for nm, a in by_name.items():
        if nm and len(nm) > 3 and nm in text:
            return a.get("id"), a.get("name")
    return None, "non-area"


def _build_update(task: dict, by_name: dict) -> dict:
    """The fields this backfill writes — urgency + area ONLY. Never deadlines."""
    aid, alabel = _resolve_area(task, by_name)
    return {"urgency": _derive_urgency(task), "area_id": aid, "area_label": alabel}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write (default: dry-run)")
    args = ap.parse_args()

    tasks = supabase_client.get_tasks(status=None, limit=5000, include_pending=True)
    areas = supabase_client.get_areas() or []
    by_name = {(a.get("name") or "").strip().lower(): a for a in areas}

    plan = []
    for t in tasks:
        upd = _build_update(t, by_name)
        # only write when something actually changes (idempotent)
        if upd["urgency"] == (t.get("urgency") or "M") and \
                upd["area_label"] == (t.get("area_label") or "non-area"):
            continue
        plan.append((t["id"], t.get("title", ""), upd))

    print(f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(plan)}/{len(tasks)} tasks to update "
          f"(areas available: {len(areas)})")
    print("  urgency:", dict(Counter(u['urgency'] for _, _, u in plan)))
    print("  area:", dict(Counter(u['area_label'] for _, _, u in plan)))
    for _tid, title, u in plan[:25]:
        print(f"  {u['urgency']} {u['area_label'][:22]:22} {str(title)[:48]}")

    if not args.apply:
        print("\nRe-run with --apply to write. Deadlines are NEVER touched.")
        return

    for tid, _title, upd in plan:
        assert "deadline" not in upd and "deadline_confidence" not in upd
        supabase_client.update_task(tid, **upd)
    supabase_client.log_action(
        action="backfill_task_urgency_area",
        details={"updated": len(plan), "scanned": len(tasks)},
        triggered_by="auto",
    )
    print(f"\nApplied {len(plan)} updates.")


if __name__ == "__main__":
    main()
