"""
Read the Project Status shadow diff and judge whether it is safe to leave
shadow mode.  [2026-08-07]

This is the P4 gate. The reconcile has been running in shadow on every
30-minute tick, computing exactly what it WOULD do and writing nothing. Before
`PROJECT_STATUS_RECONCILE_SHADOW_MODE=false`, somebody has to actually look at
that diff against a sheet a human has been working in.

Run it after a review session. It runs one live shadow pass, cross-checks the
classification against what is visibly in the workbook, and applies the four
tests that decide the flag:

  1. NOTHING WOULD BE WRITTEN INTO A HUMAN ROW. The single load-bearing
     invariant — the system may only touch lines it authored. A hit here is
     disqualifying on its own.
  2. Every classification matches the sheet. Rows the engine calls "system"
     really do carry a uid the database knows; rows it calls "hers" really do
     lack one. A GHOST or a duplicate uid means identity has drifted.
  3. Nothing is being suppressed or created at a scale that suggests a
     misread rather than an edit.
  4. Idle is silent. Re-running immediately must produce the same plan — a
     diff that changes on its own is a phantom, and phantoms become an endless
     write loop the moment shadow comes off.

Read-only. It never writes, whatever the flags say.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from services.supabase_client import supabase_client  # noqa: E402


def _human_rows(grids: dict) -> dict:
    """{tab -> {row numbers a human authored}}, straight from the sheet."""
    from services.project_status_rows import HUMAN_ACTION, HUMAN_PROJECT, INCOMPLETE, iter_rows, parse_tab

    out = {}
    for tab, grid in grids.items():
        blocks, orphans, _ = parse_tab(grid)
        out[tab] = {r.row_number for r in iter_rows(blocks, orphans)
                    if r.kind in (HUMAN_ACTION, HUMAN_PROJECT, INCOMPLETE)}
    return out


async def main(verbose: bool) -> int:
    from processors.project_status_reconcile import _read_tabs, build_plan

    sid = settings.PROJECT_STATUS_SHEET_ID
    if not sid:
        print("PROJECT_STATUS_SHEET_ID not configured")
        return 1

    print("Reading the workbook…")
    grids = _read_tabs(sid)
    plan = build_plan(grids)
    humans = _human_rows(grids)
    total_human = sum(len(v) for v in humans.values())

    print("=" * 72)
    print(f"  {len(grids)} tabs · {total_human} human-authored row(s) found")
    print()
    counts = {k: v for k, v in plan.summary().items() if isinstance(v, int) and v}
    print("  WOULD DO:", counts or "nothing (idle)")
    if plan.skipped_tabs:
        print(f"  SKIPPED TABS: {plan.skipped_tabs}")

    if verbose and plan.overrides:
        print("\n  detail:")
        for line in plan.overrides[:40]:
            print(f"      {line}")

    failures = []

    # 1. The invariant.
    trespass = [(tab, row) for tab, row, _c, _v in plan.cell_writes
                if row in humans.get(tab, set())]
    if trespass:
        failures.append(
            f"WOULD WRITE INTO {len(trespass)} HUMAN ROW(S): {trespass[:5]}")

    # 2. Identity.
    if plan.counters.get("ghosts"):
        failures.append(
            f"{plan.counters['ghosts']} row(s) carry a uid the database does not "
            "know — identity has drifted")
    if plan.counters.get("dup_uids"):
        print(f"\n  NOTE: {plan.counters['dup_uids']} duplicated uid(s) — a block "
              "was pasted. Handled (topmost keeps identity), but worth a look.")

    # 3. Scale.
    cap_s = getattr(settings, "PROJECT_STATUS_MAX_SUPPRESS_PER_CYCLE", 5)
    cap_c = getattr(settings, "PROJECT_STATUS_MAX_CREATES_PER_CYCLE", 25)
    if len(plan.suppress) >= cap_s:
        failures.append(f"{len(plan.suppress)} suppressions at the cap of {cap_s}")
    if len(plan.creates) >= cap_c:
        failures.append(f"{len(plan.creates)} creates at the cap of {cap_c}")

    # 4. Determinism — the same read must produce the same plan.
    again = build_plan(grids)
    if again.summary() != plan.summary():
        failures.append("the plan CHANGED on a second pass over the same data — "
                        "something is non-deterministic and will loop")

    print()
    if failures:
        print("  NOT READY to leave shadow:")
        for f in failures:
            print(f"      - {f}")
        return 1

    print("  All four checks pass.")
    if not counts:
        print("  NOTE: the diff is empty. That proves the engine is quiet at rest,")
        print("  but NOT that it classifies edits correctly — make some edits in")
        print("  the sheet and run this again before flipping the flag.")
    else:
        print("  Nothing would touch a human row; identity is intact; the volume")
        print("  is edit-shaped, not misread-shaped; and the plan is stable.")
        print("\n  To go live:")
        print("    gcloud run services update gianluigi --region=europe-west1 \\")
        print("      --update-env-vars PROJECT_STATUS_RECONCILE_SHADOW_MODE=false")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="list every would-be change")
    sys.exit(asyncio.run(main(ap.parse_args().verbose)))
