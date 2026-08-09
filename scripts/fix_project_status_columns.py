"""Report hidden-column damage on the area tabs, and repair it.

Priority and Comments were once INVISIBLE on the live sheet: the formatting step
said only "hide/tint/protect the system block" and nothing said what should be
VISIBLE, so when the block moved from columns 8-12 to 10-14 the two columns it
vacated kept the old hidden + white-on-white-8pt styling.

The CHECK lives here because it names that exact symptom in one line per tab.
The REPAIR delegates to scripts/restyle_workbook.py.

WHY IT DELEGATES
----------------
This script used to call `_v2_structure_requests` on its own. That function
later gained a body-wide validation and border WIPE — correct there, because
every other caller re-applies the per-row passes immediately afterwards. This
one did not, so running it stripped every tick box, dropdown, date rule and
project fence off the live workbook with nothing to put them back. The tick box
is the sheet's primary write gesture; the repair would have removed the ability
to mark anything done.

Duplicating those passes here would just let the two copies drift again, which
is how this happened in the first place. One implementation, one caller.
[2026-08-09 code review, finding 5]

    python scripts/fix_project_status_columns.py            # show what is wrong
    python scripts/fix_project_status_columns.py --apply
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings                            # noqa: E402
from processors.project_status_reconcile import NON_AREA_TABS    # noqa: E402
from services.google_sheets import sheets_service               # noqa: E402
from services.project_status_rows import ALL_HEADERS            # noqa: E402
from services.project_status_sheet import N_ALL, N_VISIBLE      # noqa: E402


def _state(ssid: str) -> dict:
    """{tab: (sheetId, [hidden column indexes])}"""
    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=ssid,
            fields="sheets(properties(title,sheetId),"
                   "data(columnMetadata(hiddenByUser)))"))
    out = {}
    for sh in meta.get("sheets", []):
        title = sh["properties"]["title"]
        if title in NON_AREA_TABS:
            continue
        cols = (sh.get("data") or [{}])[0].get("columnMetadata", [])
        hidden = [i for i, c in enumerate(cols[:N_ALL]) if c.get("hiddenByUser")]
        out[title] = (sh["properties"]["sheetId"], hidden)
    return out


async def main(apply_it: bool) -> int:
    ssid = settings.PROJECT_STATUS_SHEET_ID
    print(f"workbook: {ssid}")
    want = list(range(N_VISIBLE, N_ALL))

    wrong = []
    for tab, (_sid, hidden) in _state(ssid).items():
        bad = [ALL_HEADERS[i] for i in hidden if i < N_VISIBLE]
        mark = "  ok  " if hidden == want else " !!   "
        print(f"{mark}{tab:<34} hidden={hidden}"
              + (f"  <- WRONGLY HIDDEN: {bad}" if bad else ""))
        if hidden != want:
            wrong.append(tab)

    if not wrong:
        print("\n  every tab already correct — nothing to do")
        return 0
    if not apply_it:
        print(f"\n  {len(wrong)} tab(s) need repair. Nothing was written. "
              "Re-run with --apply")
        return 0

    print("\n  repairing via scripts/restyle_workbook.py — it re-applies the "
          "per-row tick boxes, dropdowns, date rules and project fences that "
          "the structure pass clears")
    from scripts.restyle_workbook import run as restyle
    rc = await restyle(True)
    if rc:
        return rc

    after = {t: h for t, (_s, h) in _state(ssid).items()}
    ok = all(h == want for h in after.values())
    for tab, hidden in after.items():
        print(f"{'  ok  ' if hidden == want else ' !!   '}{tab:<34} hidden={hidden}")
    print("\n  DONE — Priority and Comments are visible" if ok
          else "\n  ** still wrong, investigate")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().apply)))
