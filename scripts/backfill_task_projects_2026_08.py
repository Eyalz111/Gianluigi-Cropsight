"""
Backfill tasks.project_id from tasks.label.  [2026-08-07]

Second of the two post-migration follow-ups named at the bottom of
scripts/migrate_project_status_v2.sql. The Project Status sheet is
PROJECT-CENTRIC — a task with no project_id simply does not appear on it — so
this is what makes the sheet non-empty at cutover.

WHY THIS NEEDS MORE THAN A LOOKUP

`label` was never a project. Measured on live data the morning of the seed:
355 approved live tasks, 123 labelled, 111 distinct labels, and
`match_label_to_canonical` scored **zero** hits against the 22 curated
projects. The labels are topics — "AWS Credit Card", "NCPB Follow-up",
"Dafna Introduction". Deterministic matching alone would attach nothing.

So the mapping runs in tiers, most-confident first, and every tier is
reportable before anything is written:

  A  exact / alias match          free, certain, kept even though it hits 0
                                  today — it is the tier that starts paying as
                                  Nechama names real projects in the sheet
  B  LLM over DISTINCT LABELS     36 labels, not 355 tasks. One call, and the
                                  result is a label->project table Eyal reads
  C  LLM over UNLABELLED titles   only for OPEN tasks — see below
  D  area "Others" bucket         category -> that area's Others project

The vocabulary is CLOSED: the model picks a name from the 22 or answers
UNKNOWN. A reply outside the vocabulary is discarded, never fuzzy-matched —
inventing a project is the one failure mode that would pollute the curated
list, which is the thing Eyal asked to protect.

WHAT IS DELIBERATELY LEFT NULL

Tiers C and D run for OPEN tasks only. Of 355 approved tasks just 64 are open,
and only open tasks reach the sheet. Spending a judgement call on a task closed
three months ago — then parking it in an Others bucket — would inflate every
Others block with history nobody will review. A closed unlabelled task keeps
project_id NULL, which is the honest answer. Tier B still applies to closed
tasks for free, because the label table is computed anyway.

Category "General" has no area and therefore no Others bucket; those stay NULL.

STICKINESS

Writes are plain updates. `manual_project_id` is deliberately NOT set: this is
a system guess, and marking it sticky would freeze it against the human
correction that the Project Status sheet exists to capture. Contrast
supabase_client.set_task_project, which DOES mark it — that path is a
deliberate drag into another block, which must survive the next system pass.

Idempotent: only tasks with project_id IS NULL are considered, so a second run
is a no-op. --rollback restores from the JSON snapshot written by --apply.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from core.llm import call_llm, parse_json_array  # noqa: E402
from services.supabase_client import supabase_client  # noqa: E402

OPEN_STATUSES = ("pending", "in_progress", "overdue")

# Area name -> the Others project that catches its untagged work. Mirrors the
# area-qualified names in seed_canonical_projects_2026_08.TARGET.
OTHERS_BY_AREA = {
    "PRODUCT & TECHNOLOGY": "Others — Product & Technology",
    "SALES & BUSINESS DEVELOPMENT": "Others & Nostro",
    "FUNDRAISING & INVESTOR RELATIONS": "Others — Fundraising",
    "TEAM & HUMAN RESOURCES": "Others — Team & HR",
    "CLIENT DELIVERY & OPERATIONS": "Others — Client Delivery",
    # LEGAL has no Others bucket by design — Legal/Corporate/Finance already
    # partition it exhaustively.
}

SYSTEM = """You map CropSight work items onto a FIXED list of projects.

Answer ONLY with a JSON array. One object per input item, in order:
  {"n": <the item's number>, "project": "<a name from the list, or UNKNOWN>"}

Return one object for EVERY numbered item, including the ones you answer
UNKNOWN for.

Rules:
- `project` MUST be copied character-for-character from the allowed list, or be
  the literal string UNKNOWN.
- Answer UNKNOWN whenever you are not confident. UNKNOWN is a good answer; a
  wrong project is not. The items you cannot place are reported to a human.
- Never invent a project name, never merge two, never abbreviate.
- The area hint in brackets is where the item is already filed. Prefer a
  project from that area unless the text clearly says otherwise."""


def _prompt(items: list[str], vocab: list[str], kind: str) -> str:
    listing = "\n".join(f"- {name}" for name in vocab)
    body = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(items))
    return (
        f"Allowed projects:\n{listing}\n\n"
        f"Map each of these {kind} to one project:\n{body}\n\n"
        "Return the JSON array now."
    )


def _classify(items: list[str], vocab: list[str], kind: str,
              call_site: str) -> tuple[dict, bool]:
    """({item -> project name}, healthy) for items the model placed confidently.

    Anything outside the allowed vocabulary is dropped rather than repaired.
    Chunked so one oversized prompt can't truncate the reply mid-array.

    `healthy` is False if any chunk failed to call or parse. The caller uses it
    to SKIP tier D: with the judgement tiers dead, the Others fallback would
    quietly sweep every open task into a bucket and the run would still look
    like it succeeded. Silently mass-filing work under the wrong project is
    worse than filing none of it.
    """
    allowed = {name.lower(): name for name in vocab}
    out: dict = {}
    healthy = True
    if not items:
        return out, healthy

    for start in range(0, len(items), 40):
        chunk = items[start:start + 40]
        try:
            text, _ = call_llm(
                prompt=_prompt(chunk, vocab, kind),
                model=settings.model_agent,
                max_tokens=4000,
                call_site=call_site,
                system=SYSTEM,
            )
        except Exception as e:                      # noqa: BLE001
            print(f"  !! LLM call failed for {kind} chunk {start}: {e}")
            healthy = False
            continue

        rows = parse_json_array(text)
        if rows is None:
            print(f"  !! could not parse the reply for {kind} chunk {start}")
            healthy = False
            continue

        # Keyed by INDEX, not by the model's echo of the item. Titles run to 120
        # chars and carry em-dashes; requiring a character-exact echo silently
        # dropped answers that then read as UNKNOWN in the report. An index
        # cannot be paraphrased.
        for row in rows:
            if not isinstance(row, dict):
                continue
            proj = str(row.get("project", "")).strip()
            try:
                n = int(row.get("n"))
            except (TypeError, ValueError):
                continue
            if not 1 <= n <= len(chunk):
                continue
            original = chunk[n - 1]
            if proj.upper() == "UNKNOWN":
                continue
            hit = allowed.get(proj.lower())
            if hit:
                out[original] = hit
            else:
                print(f"     (discarded off-vocabulary answer: {proj!r})")
    return out, healthy


def build_plan() -> dict:
    projects = supabase_client.get_canonical_projects(status=None)
    active = [p for p in projects if (p.get("status") or "active") != "retired"]
    by_name = {p["name"].lower(): p for p in projects}
    # The judgement tiers are offered NAMED projects only. Measured on the first
    # run: with the Others buckets in the vocabulary the model used them as an
    # escape hatch instead of answering UNKNOWN, and "Others — Client Delivery"
    # collected "BD & Sales", "Consortium Research" and "Thailand Meeting" —
    # none of which are client delivery. An Others bucket means "this area, no
    # named project", which is a fact about the task's CATEGORY, not a judgement
    # call. Tier D reaches them deterministically from that category instead.
    vocab = [p["name"] for p in active if not p["name"].lower().startswith("others")]

    # tasks.category already carries the area NAME, so tier D needs no areas
    # lookup — it maps that name straight onto the area's Others project.
    others_id = {}
    for area_name, proj_name in OTHERS_BY_AREA.items():
        proj = by_name.get(proj_name.lower())
        if proj:
            others_id[area_name] = proj["id"]

    rows = (
        supabase_client.client.table("tasks")
        .select("id,title,label,category,status,project_id")
        .eq("approval_status", "approved")
        .is_("valid_to", "null")
        .is_("project_id", "null")
        .limit(2000)
        .execute()
        .data
        or []
    )
    open_rows = [t for t in rows if (t.get("status") or "") in OPEN_STATUSES]

    plan = {"assign": [], "tiers": Counter(), "unplaced": [], "vocab": vocab}

    # ---- tier A: deterministic, per distinct label ----------------------
    labels = sorted({(t.get("label") or "").strip()
                     for t in rows if (t.get("label") or "").strip()})
    label_map, undecided = {}, []
    for label in labels:
        hit = supabase_client.match_label_to_canonical(label, projects=projects)
        if hit:
            label_map[label] = hit
        else:
            undecided.append(label)

    # ---- tier B: LLM, per distinct label --------------------------------
    hint = {}
    for t in rows:
        label = (t.get("label") or "").strip()
        if label and label not in hint and t.get("category"):
            hint[label] = t["category"]
    prompts = [f"{label}  [{hint.get(label, 'General')}]" for label in undecided]
    decided, ok_labels = _classify(prompts, vocab, "work topics", "backfill_labels")
    tier_b = set()
    for prompted, proj in decided.items():
        label = prompted.rsplit("  [", 1)[0]
        label_map[label] = proj
        tier_b.add(label)

    for t in rows:
        label = (t.get("label") or "").strip()
        proj = label_map.get(label)
        if not proj:
            continue
        target = by_name.get(proj.lower())
        if target:
            plan["assign"].append((t, target, "B" if label in tier_b else "A"))
    assigned = {t["id"] for t, _, _ in plan["assign"]}

    # ---- tier C: LLM by title, OPEN + unlabelled only -------------------
    orphans = [t for t in open_rows
               if t["id"] not in assigned and not (t.get("label") or "").strip()]
    titles = [f"{(t.get('title') or '')[:120]}  [{t.get('category') or 'General'}]"
              for t in orphans]
    by_title, ok_titles = _classify(titles, vocab, "task titles", "backfill_titles")
    for t, prompted in zip(orphans, titles):
        proj = by_title.get(prompted)
        target = by_name.get(proj.lower()) if proj else None
        if target:
            plan["assign"].append((t, target, "C"))
            assigned.add(t["id"])

    # ---- tier D: area Others, OPEN only ---------------------------------
    # Gated on the judgement tiers having actually run. See _classify.
    plan["llm_healthy"] = ok_labels and ok_titles
    if plan["llm_healthy"]:
        for t in open_rows:
            if t["id"] in assigned:
                continue
            area_name = (t.get("category") or "").strip()
            pid = others_id.get(area_name)
            if pid:
                target = next((p for p in projects if p["id"] == pid), None)
                if target:
                    plan["assign"].append((t, target, "D"))
                    assigned.add(t["id"])

    for t, _, tier in plan["assign"]:
        plan["tiers"][tier] += 1
    plan["unplaced"] = [t for t in rows if t["id"] not in assigned]
    plan["open_unplaced"] = [t for t in open_rows if t["id"] not in assigned]
    plan["label_map"] = label_map
    plan["total_candidates"] = len(rows)
    plan["open_candidates"] = len(open_rows)
    return plan


def report(plan: dict) -> None:
    print("=" * 72)
    if not plan.get("llm_healthy"):
        print("  ** THE JUDGEMENT TIERS DID NOT RUN CLEANLY.")
        print("  ** Tier D (Others buckets) was SKIPPED so nothing gets mass-filed")
        print("  ** under the wrong project. Fix the LLM path and re-run.\n")
    print(f"  candidates (approved, live, project_id NULL): "
          f"{plan['total_candidates']}   of which open: {plan['open_candidates']}")
    print(f"  WOULD ASSIGN  {len(plan['assign'])}")
    for tier, label in (("A", "exact/alias match"), ("B", "label -> project (LLM)"),
                        ("C", "title -> project (LLM, open only)"),
                        ("D", "area Others bucket (open only)")):
        print(f"      tier {tier}  {plan['tiers'][tier]:4}   {label}")

    grouped = defaultdict(list)
    for task, target, tier in plan["assign"]:
        grouped[target["name"]].append((task, tier))
    print(f"\n  BY PROJECT ({len(grouped)} projects receive work):")
    for name in sorted(grouped, key=lambda n: -len(grouped[n])):
        entries = grouped[name]
        open_n = sum(1 for t, _ in entries if (t.get("status") or "") in OPEN_STATUSES)
        print(f"      {len(entries):4} ({open_n} open)  {name}")

    print("\n  LABEL -> PROJECT table (what tier B decided):")
    for label in sorted(plan["label_map"]):
        print(f"      {label:38} -> {plan['label_map'][label]}")

    print(f"\n  LEFT NULL  {len(plan['unplaced'])}  "
          f"(of which OPEN and therefore absent from the sheet: "
          f"{len(plan['open_unplaced'])})")
    for t in plan["open_unplaced"]:
        print(f"      [{t.get('category') or 'General'}] {(t.get('title') or '')[:70]}")


def apply(plan: dict) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = Path(__file__).parent / f"_task_projects_snapshot_{stamp}.json"
    snap.write_text(json.dumps(
        [{"task_id": t["id"], "previous_project_id": t.get("project_id")}
         for t, _, _ in plan["assign"]], indent=2), encoding="utf-8")
    print(f"  rollback snapshot: {snap.name}")

    ok = fail = 0
    for task, target, _ in plan["assign"]:
        try:
            # Plain update — NOT set_task_project. A backfill guess must stay
            # correctable; marking manual_project_id would freeze it.
            supabase_client.client.table("tasks").update(
                {"project_id": target["id"]}
            ).eq("id", task["id"]).execute()
            ok += 1
        except Exception as e:                      # noqa: BLE001
            fail += 1
            print(f"  !! {task['id']}: {e}")
    print(f"\n  attached {ok}   failed {fail}")


def rollback(path: Path) -> None:
    rows = json.loads(path.read_text(encoding="utf-8"))
    ok = 0
    for row in rows:
        try:
            supabase_client.client.table("tasks").update(
                {"project_id": row["previous_project_id"]}
            ).eq("id", row["task_id"]).execute()
            ok += 1
        except Exception as e:                      # noqa: BLE001
            print(f"  !! {row['task_id']}: {e}")
    print(f"  restored {ok}/{len(rows)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write to the database")
    ap.add_argument("--rollback", metavar="SNAPSHOT.json",
                    help="restore project_id from a snapshot written by --apply")
    args = ap.parse_args()

    if args.rollback:
        path = Path(args.rollback)
        if not path.is_absolute():
            path = Path(__file__).parent / path
        rollback(path)
        return 0

    print("APPLY" if args.apply else "DRY RUN (nothing written) — re-run with --apply")
    plan = build_plan()
    report(plan)
    if args.apply:
        if not plan.get("llm_healthy"):
            print("\n  REFUSING TO APPLY — the judgement tiers failed (see above).")
            return 1
        apply(plan)
    else:
        print("\n  Nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
