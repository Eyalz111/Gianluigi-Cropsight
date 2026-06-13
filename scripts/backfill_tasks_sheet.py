"""
DEPRECATED — do not use. (audit P6-04)

This script used to clear the live Tasks sheet on a bare
`python scripts/backfill_tasks_sheet.py` and rewrite it with the STALE 9-column
`A:I` header — no urgency/area columns (K-L) and no col-J UUID. That is exactly
the "tasks vanished" / layout-regression incident class: it wiped Eyal's manual
deadline/area edits and broke the reconcile col-J UUID lockstep, with no dry-run
and no env guard.

Use the safe, current-layout sibling instead:

    python scripts/repopulate_tasks_sheet.py --apply

(It restores rows with col-J UUIDs and the urgency/area layout, behind an
explicit --apply gate.) Or `scripts/rebuild_sheets.py`, which routes through the
guarded `rebuild_tasks_sheet` service method (force_empty guard + backup).
"""

import sys

_MSG = (
    "backfill_tasks_sheet.py is DEPRECATED (it wiped the live sheet and wrote a "
    "stale layout). Use:  python scripts/repopulate_tasks_sheet.py --apply"
)

if __name__ == "__main__":
    print(_MSG, file=sys.stderr)
    sys.exit(1)
