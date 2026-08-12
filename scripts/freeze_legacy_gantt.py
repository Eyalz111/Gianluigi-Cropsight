"""Freeze the old Gantt board. Phase 5 of docs/GANTT_V2_PLAN.md.

Eyal, 2026-08-12: *"dont make it run in parallel, just dont delete it."*

WHAT THIS DOES NOT DO. It does not delete the workbook, it does not move it, and
it does not touch the 2028-2029 or 2030-2031 tabs. The board stays exactly where
it is, readable forever.

WHAT "RUNNING IN PARALLEL" ACTUALLY MEANT. Nothing has written that board for
months — `GANTT_SHADOW_MODE` defaults True and `reconcile_scheduler._run_gantt`
is documented "Never paints the board". The live coupling is the weekly READ:
`reconcile_gantt_lanes()` pulls board → knowledge and `compute_gantt_nudges()`
derives nudges from it. Left on, a frozen board would keep feeding March lane
text into knowledge forever and the nudges would compare the morning brief
against a board nobody maintains. Turning that off is one flag,
`GANTT_RECONCILE_ENABLED=false`, and it is not this script's job — it is a
Cloud Run env change, done deliberately at cutover.

WHAT THIS SCRIPT IS FOR: the other half. An archive with live formulas is not
archived. Its cells keep re-evaluating against tabs nobody maintains, so the
board can rot AFTER everyone stops looking at it — and it already carries
`#REF!` errors across several weeks of the `All Meetings (Aggregate)` row.
Converting every formula to its current value freezes the record as it stands.

    --dry-run   report what would change, write nothing   (DEFAULT)
    --apply     convert formulas to values on the named tab
    --tab NAME  which tab (default: the live 2026-2027 board)

DRY RUN IS THE DEFAULT AND THAT IS DELIBERATE. Repair scripts in this project
have twice done more damage than the bug they fixed, because they bypassed
guards the engine has. This one reads first, prints, and requires an explicit
--apply.
"""

import argparse
import asyncio
import logging
import sys
from collections import Counter

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])

from config.settings import settings              # noqa: E402
from services.google_sheets import sheets_service  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("freeze-gantt")

ERROR_VALUES = ("#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NULL!")


async def survey(tab: str) -> dict:
    """What is on the board right now: formulas, errors, and extent."""
    ssid = settings.GANTT_SHEET_ID
    if not ssid:
        return {"error": "GANTT_SHEET_ID is not set"}

    def _get(render):
        return sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=ssid, range=f"'{tab}'",
                valueRenderOption=render))

    formulas = (_get("FORMULA").get("values") or [])
    values = (_get("FORMATTED_VALUE").get("values") or [])

    n_formula = 0
    errors: list[str] = []
    for r, row in enumerate(formulas, start=1):
        for c, cell in enumerate(row):
            if isinstance(cell, str) and cell.startswith("="):
                n_formula += 1
    for r, row in enumerate(values, start=1):
        for c, cell in enumerate(row):
            if isinstance(cell, str) and cell.strip() in ERROR_VALUES:
                errors.append(f"r{r}c{c + 1}={cell.strip()}")

    # error_cells is COMPLETE, not truncated — freeze() clears from this list,
    # and a display cap here would silently leave the rest frozen as literal
    # "#REF!" text. Truncation belongs in the printout, not the data.
    return {"tab": tab, "rows": len(values), "formulas": n_formula,
            "errors": len(errors), "error_cells": errors,
            "error_kinds": dict(Counter(e.split("=")[-1] for e in errors))}


async def freeze(tab: str, apply: bool) -> dict:
    """Replace every formula with the value it currently shows."""
    ssid = settings.GANTT_SHEET_ID
    if not ssid:
        return {"error": "GANTT_SHEET_ID is not set"}

    report = await survey(tab)
    if report.get("error"):
        return report

    if not apply:
        report["mode"] = "dry-run"
        report["would_convert"] = report["formulas"]
        return report

    if not report["formulas"]:
        return {**report, "mode": "apply", "converted": 0,
                "note": "no formulas left; the board is already frozen"}

    # copyPaste onto ITSELF with PASTE_VALUES is the whole operation: Sheets
    # evaluates each formula and writes back the result. It is one request,
    # atomic, and it cannot half-finish the way a cell-by-cell rewrite could.
    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=ssid, fields="sheets(properties(sheetId,title))"))
    sid = next((s["properties"]["sheetId"] for s in meta.get("sheets", [])
                if s["properties"]["title"] == tab), None)
    if sid is None:
        return {"error": f"tab {tab!r} not found"}

    # CLEAR THE ERROR CELLS FIRST. PASTE_VALUES writes each cell's DISPLAYED
    # value, and the displayed value of a broken formula is the string "#REF!".
    # Freezing those would immortalise sixteen cells of noise as literal text —
    # worse than the formula, because at least a formula announces itself as
    # machinery. A #REF! is a reference that no longer resolves: it holds no
    # recoverable information, so blanking it loses nothing and leaves an
    # archive that reads clean.
    cleared = []
    for cell in report["error_cells"]:
        loc = cell.split("=")[0]
        r = int(loc[1:loc.index("c")])
        c = int(loc[loc.index("c") + 1:])
        cleared.append({"range": f"'{tab}'!{_a1(c - 1)}{r}", "values": [[""]]})
    if cleared:
        sheets_service._execute_with_retry(
            lambda: sheets_service.service.spreadsheets().values().batchUpdate(
                spreadsheetId=ssid,
                body={"valueInputOption": "RAW", "data": cleared}))

    rng = {"sheetId": sid}
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=ssid, body={"requests": [{"copyPaste": {
                "source": rng, "destination": rng,
                "pasteType": "PASTE_VALUES"}}]}))

    after = await survey(tab)
    return {"mode": "apply", "tab": tab,
            "converted": report["formulas"] - after["formulas"],
            "formulas_before": report["formulas"],
            "formulas_after": after["formulas"],
            "errors_before": report["errors"],
            "errors_after": after["errors"],
            "error_cells_cleared": len(cleared)}


def _a1(idx: int) -> str:
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually convert formulas to values")
    ap.add_argument("--tab", default=None, help="tab name (default: the live board)")
    args = ap.parse_args()

    await sheets_service.authenticate()
    tab = args.tab or settings.GANTT_MAIN_TAB
    out = await freeze(tab, apply=args.apply)

    for k, v in out.items():
        if k == "error_cells" and isinstance(v, list) and len(v) > 12:
            logger.info(f"{k}: {v[:12]} … and {len(v) - 12} more")
        else:
            logger.info(f"{k}: {v}")
    if not args.apply and not out.get("error"):
        logger.info("")
        logger.info("Dry run. Re-run with --apply to freeze the board.")
        logger.info("Remember: this is only half of Phase 5. The other half is")
        logger.info("GANTT_RECONCILE_ENABLED=false on Cloud Run, which stops the")
        logger.info("weekly read that would otherwise keep feeding a dead board")
        logger.info("into knowledge.")


if __name__ == "__main__":
    asyncio.run(main())
