"""Re-assert the column visibility/tint/protection on every area tab.

Priority and Comments were INVISIBLE on the live sheet: the formatting step said
only "hide/tint/protect the system block" and nothing said what should be
VISIBLE, so when the block moved from columns 8-12 to 10-14 the two columns it
vacated kept the old hidden + white-on-white-8pt styling.

FORMATTING ONLY. No cell value is read or written, so this cannot lose data and
can be re-run at any time — `_v2_structure_requests` now describes the whole
state rather than adding to it, which is exactly what makes re-running it a
repair rather than another layer.

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
from services.project_status_sheet import (                     # noqa: E402
    N_ALL, N_VISIBLE, _v2_structure_requests,
)
from services.project_status_rows import ALL_HEADERS            # noqa: E402


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

    before = _state(ssid)
    wrong = []
    for tab, (_sid, hidden) in before.items():
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
        print(f"\n  WOULD re-assert formatting on {len(wrong)} tab(s). "
              "Nothing was written. Re-run with --apply")
        return 0

    # The protected range is re-added by _v2_structure_requests, so the existing
    # one has to go first or every run stacks another copy on the same columns.
    from services.project_status_sheet import _PROTECT_DESC
    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=ssid, fields="sheets(properties.title,protectedRanges)"))
    drops = [{"deleteProtectedRange": {"protectedRangeId": pr["protectedRangeId"]}}
             for sh in meta.get("sheets", [])
             if sh["properties"]["title"] not in NON_AREA_TABS
             for pr in (sh.get("protectedRanges") or [])
             if pr.get("description") == _PROTECT_DESC]

    reqs = list(drops)
    for tab, (sid, _hidden) in before.items():
        reqs.extend(_v2_structure_requests(
            sid, settings.PROJECT_STATUS_DUE_SOON_DAYS))
    # The colour rules come along with the structure requests and would stack a
    # duplicate set on every run, so the existing ones are cleared first.
    rules = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=ssid,
            fields="sheets(properties(title,sheetId),conditionalFormats)"))
    clears = []
    for sh in rules.get("sheets", []):
        if sh["properties"]["title"] in NON_AREA_TABS:
            continue
        n = len(sh.get("conditionalFormats") or [])
        for i in range(n - 1, -1, -1):
            clears.append({"deleteConditionalFormatRule": {
                "sheetId": sh["properties"]["sheetId"], "index": i}})
    reqs = drops + clears + reqs[len(drops):]

    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=ssid, body={"requests": reqs}))
    print(f"\n  applied to {len(before)} tab(s)")

    after = _state(ssid)
    ok = True
    for tab, (_sid, hidden) in after.items():
        good = hidden == want
        ok = ok and good
        print(f"{'  ok  ' if good else ' !!   '}{tab:<34} hidden={hidden}")
    print("\n  DONE — Priority and Comments are visible"
          if ok else "\n  ** still wrong, investigate")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().apply)))
