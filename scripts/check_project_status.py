"""Read-only health check of the Project Status workbook. Writes NOTHING.

Run it after a working session to see, in one place, whether what you typed has
landed and what the next cycle intends to do:

    python scripts/check_project_status.py

It reads through the SAME code the engine uses — `_read_tabs`, `parse_tab`,
`build_plan` — so "the plan is empty" here means the plan is empty in
production. A check that reimplements the thing it checks agrees with itself,
not with the system; an earlier version of this enumerated the tabs on its own
and reported a layout mismatch the engine never had.

Nothing here writes. `build_plan` is pure: it decides what WOULD change and
returns it, which is the same property that makes shadow mode honest.
"""

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings                            # noqa: E402
from processors import project_status_reconcile as psr          # noqa: E402
from services.google_sheets import (                            # noqa: E402
    MEETING_STATUSES, MEETING_TERMINAL_STATUSES, sheets_service,
)
from services.project_status_rows import (                      # noqa: E402
    AMBIGUOUS, COL_NUM, COL_PRIORITY, COL_PROJECT, COL_TOPIC, HUMAN_ACTION,
    HUMAN_PROJECT, INCOMPLETE, SYSTEM_ACTION, SYSTEM_PROJECT,
    find_duplicate_uids, parse_tab, unresolved_columns,
)
from services.supabase_client import supabase_client            # noqa: E402

OK, BAD, NOTE = "  ok  ", " !!   ", "  ·   "


def _h(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _area_tabs() -> dict:
    return psr._read_tabs(settings.PROJECT_STATUS_SHEET_ID)


def check_layout(grids: dict) -> int:
    """Every area tab must declare all 14 columns, or the engine skips it."""
    _h("LAYOUT")
    problems = 0
    for tab, grid in grids.items():
        header = grid[2] if len(grid) > 2 else []
        missing = unresolved_columns(header)
        if missing:
            print(f"{BAD}{tab}: header does not declare {missing}")
            print(f"{NOTE}the engine SKIPS this tab entirely until it does")
            problems += 1
        else:
            print(f"{OK}{tab}")
    return problems


def check_rows(grids: dict) -> int:
    """Numbering, row kinds, and the things a human gesture leaves behind."""
    _h("ROWS")
    problems = 0
    all_blocks, all_orphans = [], []
    for tab, grid in grids.items():
        blocks, orphans, _cols = parse_tab(grid)
        all_blocks.extend(blocks)
        all_orphans.extend(orphans)

        kinds = Counter()
        for b in blocks:
            if b.project:
                kinds[b.project.kind] += 1
            for a in b.actions:
                kinds[a.kind] += 1

        nums = [b.project.values.get(COL_NUM) for b in blocks if b.project]
        actual = [int(n) if str(n).strip().isdigit() else n for n in nums]
        numbered = actual == list(range(1, len(actual) + 1))

        bits = [f"{len(blocks)} block(s)"]
        for k in (SYSTEM_ACTION, HUMAN_ACTION, HUMAN_PROJECT, INCOMPLETE, AMBIGUOUS):
            if kinds.get(k):
                bits.append(f"{kinds[k]} {k}")
        if orphans:
            bits.append(f"{len(orphans)} orphan(s)")
        flag = OK if (numbered and not kinds.get(AMBIGUOUS)) else BAD
        print(f"{flag}{tab}: {', '.join(bits)}")

        if not numbered:
            print(f"{NOTE}numbering is {actual} — it is renumbered on the next "
                  "02:00 pass, so this is only wrong if it stays wrong")
        for b in blocks:
            for row in ([b.project] if b.project else []) + list(b.actions):
                if row.kind == AMBIGUOUS:
                    problems += 1
                    print(f"{BAD}r{row.row_number}: Project AND Action are both "
                          f"filled — clear one, the system will not guess")
                if row.kind == INCOMPLETE:
                    print(f"{NOTE}r{row.row_number}: started but has no Action "
                          f"text, so nothing is created from it")
                if row.kind == SYSTEM_PROJECT and row.values.get(COL_PRIORITY):
                    print(f"{NOTE}r{row.row_number}: a project row has a "
                          "Priority — priorities belong to actions")
                if row.kind == SYSTEM_ACTION and row.values.get(COL_PROJECT):
                    print(f"{BAD}r{row.row_number}: an action row has the "
                          "Project cell filled — that makes it ambiguous")
                    problems += 1

    dups = find_duplicate_uids(all_blocks, all_orphans)
    if dups:
        print(f"{NOTE}{len(dups)} duplicated identity/ies (a copied row): "
              f"{list(dups)[:4]}")
        print(f"{NOTE}the topmost keeps it; a copy whose TEXT differs is "
              "released and becomes a new item next cycle")
    return problems


def check_priorities(grids: dict) -> None:
    _h("PRIORITIES")
    counts, odd = Counter(), []
    for tab, grid in grids.items():
        blocks, _o, _c = parse_tab(grid)
        for b in blocks:
            for a in b.actions:
                raw = (a.values.get(COL_PRIORITY) or "").strip()
                counts[raw or "(blank)"] += 1
                if raw and raw not in ("Urgent", "H", "M", "L"):
                    odd.append((tab, a.row_number, raw))
    print(f"{OK}on the sheet: {dict(counts)}")
    for tab, row, raw in odd:
        print(f"{BAD}{tab} r{row}: {raw!r} is not one of Urgent/H/M/L — it is "
              "left in the cell but never saved")


def check_plan() -> int:
    """The one that matters: what would the next cycle actually do?"""
    _h("WHAT THE NEXT CYCLE WOULD DO")
    grids = _area_tabs()
    plan = psr.build_plan(grids)
    summary = {k: v for k, v in plan.summary().items() if v}
    if not summary:
        print(f"{OK}nothing — the sheet and the database agree")
        return 0
    print(f"{OK if not plan.skipped_tabs else BAD}{summary}")
    for label, items in (("pull into the DB", plan.task_updates),
                         ("project updates", plan.project_updates)):
        if items:
            print(f"{NOTE}{label}: {len(items)}")
    for o in plan.overrides[:20]:
        print(f"{NOTE}{o}")
    if len(plan.overrides) > 20:
        print(f"{NOTE}... and {len(plan.overrides) - 20} more")
    if plan.skipped_tabs:
        print(f"{BAD}SKIPPED tabs: {plan.skipped_tabs}")
        return 1
    return 0


async def check_meetings() -> int:
    _h("MEETINGS POOL")
    print(f"{OK}workbook: {sheets_service.meetings_workbook()}")
    rows = await sheets_service.get_all_meetings()
    if not rows:
        print(f"{BAD}the Meetings tab read back EMPTY — check the workbook id")
        return 1
    counts = Counter((r.get("status") or "").strip() for r in rows)
    print(f"{OK}{len(rows)} row(s): {dict(counts)}")

    problems = 0
    unknown = {s for s in counts if s and s not in MEETING_STATUSES}
    if unknown:
        print(f"{BAD}status values nothing recognises: {sorted(unknown)}")
        print(f"{NOTE}valid: {', '.join(MEETING_STATUSES)}")
        problems += 1
    # A BOOKED MEETING WITH NO DATE IS A CONTRADICTION. `scheduled` and
    # `recurring` both claim the meeting is happening, and nothing will remind
    # anyone about a date that does not exist. The pool exists to get things
    # into the calendar, so this is the one state it should never sit in
    # quietly. [2026-08-10]
    undated = [r for r in rows
               if (r.get("status") or "").strip().lower() in ("scheduled",)
               and not (r.get("proposed_date_raw") or "").strip()]
    if undated:
        problems += 1
        print(f"{BAD}{len(undated)} meeting(s) marked scheduled with NO date:")
        for r in undated:
            print(f"{NOTE}  {(r.get('title') or '')[:66]}")

    blank = [r for r in rows if not (r.get("id") or "").strip()]
    if blank:
        print(f"{NOTE}{len(blank)} hand-typed row(s) with no UUID yet — they "
              "are created and stamped on the next cycle")
    no_title = [r for r in rows if not (r.get("title") or "").strip()]
    if no_title:
        print(f"{NOTE}{len(no_title)} row(s) with no title — ignored, never "
              "created")

    db = {m["id"]: m for m in
          (supabase_client.list_follow_up_meetings(limit=2000,
                                                   include_pending=True) or [])}
    print(f"{OK}database holds {len(db)} follow-up meeting(s)")
    for r in rows:
        mid = (r.get("id") or "").strip()
        dm = db.get(mid)
        if not mid or not dm:
            continue
        s_status = (r.get("status") or "").strip().lower()
        d_status = (dm.get("status") or "").strip().lower()
        if s_status and s_status != d_status:
            terminal = d_status in MEETING_TERMINAL_STATUSES
            print(f"{BAD if terminal else NOTE}{(r.get('title') or '')[:45]!r}: "
                  f"sheet={s_status} db={d_status}"
                  + ("  <- the DB state is TERMINAL, so the cell will be "
                     "refreshed rather than pulled" if terminal
                     else "  <- will be pulled on the next cycle"))
    return problems


def check_coverage() -> int:
    _h("COVERAGE (is anything missing from the sheet?)")
    grouped = supabase_client.get_open_tasks_by_project()
    unfiled = sum(len(v) for k, v in grouped.items() if not k)
    attached = sum(len(v) for k, v in grouped.items() if k)
    print(f"{OK if not unfiled else BAD}{attached} open task(s) attached to a "
          f"project, {unfiled} unattached")
    if unfiled:
        for t in [t for k, v in grouped.items() if not k for t in v][:6]:
            print(f"{NOTE}no project: {(t.get('title') or '')[:70]}")
        print(f"{NOTE}unattached tasks appear on NO tab — that absence is "
              "silent, which is why it is checked here")

    on_sheet = set()
    for _tab, grid in _area_tabs().items():
        blocks, orphans, _c = parse_tab(grid)
        for b in blocks:
            for a in b.actions:
                if a.uid:
                    on_sheet.add(a.uid)
    missing = [t for v in grouped.values() for t in v
               if t["id"] not in on_sheet]
    if missing:
        print(f"{NOTE}{len(missing)} open task(s) not yet rendered — "
              + ("auto-injection is ON, so they appear on the next 02:00 pass"
                 if settings.PROJECT_STATUS_AUTO_INJECT_ENABLED
                 else "auto-injection is OFF, so they will not appear"))
        for t in missing[:5]:
            print(f"{NOTE}  {(t.get('title') or '')[:70]}")
    return 1 if unfiled else 0


async def main(skip_meetings: bool) -> int:
    print(f"Project Status : {settings.PROJECT_STATUS_SHEET_ID}")
    print(f"Meetings       : {sheets_service.meetings_workbook()}")
    print(f"auto-inject={settings.PROJECT_STATUS_AUTO_INJECT_ENABLED}  "
          f"structural slots={settings.PROJECT_STATUS_STRUCTURAL_SLOTS}")

    grids = _area_tabs()
    if not grids:
        print(f"\n{BAD}no area tabs read — nothing to check")
        return 1

    problems = check_layout(grids)
    problems += check_rows(grids)
    check_priorities(grids)
    problems += check_plan()
    if not skip_meetings:
        problems += await check_meetings()
    problems += check_coverage()

    _h("VERDICT")
    print("  all clear — nothing to fix" if not problems
          else f"  {problems} thing(s) above need a look")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-meetings", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().skip_meetings)))
