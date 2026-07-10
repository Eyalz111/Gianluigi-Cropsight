"""One-off recovery (2026-07-10): remove duplicate-UUID rows from the Tasks sheet.

Incident: a live reconcile read the sheet as EMPTY (transient Google API read
returning [] instead of raising) and re-appended 106 approved-open DB tasks it
already had, creating ~100 duplicate-UUID rows.

This DE-DUPES by keeping the FIRST occurrence of each UUID (the original top rows,
which carry Eyal's un-synced manual edits) and deleting the later (appended) copies.
It never rebuilds from the DB (that would lose the un-synced sheet edits).

    python scripts/dedupe_tasks_sheet.py            # dry-run (report only)
    python scripts/dedupe_tasks_sheet.py --apply    # delete the duplicate rows

Reads live Google Sheets. Deletes rows via one batchUpdate (bottom-to-top so row
indices don't shift). Idempotent: a second run finds nothing to delete.
"""

import asyncio
import sys

from config.settings import settings
from services.google_sheets import sheets_service


async def main(apply: bool) -> None:
    rows = await sheets_service.get_all_tasks()
    print(f"Read {len(rows)} data rows from the Tasks sheet.")

    by_id: dict[str, list[int]] = {}
    for r in rows:
        rid = str(r.get("id") or "").strip()
        rn = r.get("row_number")
        if rid and rn:
            by_id.setdefault(rid, []).append(int(rn))

    # For each UUID with >1 row, keep the FIRST (min row) and delete the rest.
    to_delete: list[int] = []
    for rid, rns in by_id.items():
        keep = min(rns)
        to_delete.extend(x for x in rns if x != keep)
    to_delete = sorted(set(to_delete))

    dup_ids = sum(1 for rns in by_id.values() if len(rns) > 1)
    print(f"Duplicate UUIDs: {dup_ids}")
    print(f"Rows to delete (later copies): {len(to_delete)}")
    if to_delete:
        print(f"  row range: {to_delete[0]}..{to_delete[-1]}")

    if not to_delete:
        print("Nothing to delete — sheet is already clean.")
        return
    if not apply:
        print("\nDry-run only. Re-run with --apply to delete these rows.")
        return

    sid = sheets_service._get_sheet_id_by_name(
        settings.TASK_TRACKER_SHEET_ID, settings.TASK_TRACKER_TAB_NAME or "Tasks"
    )
    if sid is None:
        print("ERROR: could not resolve the Tasks tab sheetId — aborting.")
        return

    # Delete bottom-to-top so earlier row numbers stay valid as we remove rows.
    requests = [
        {"deleteDimension": {"range": {
            "sheetId": sid, "dimension": "ROWS",
            "startIndex": rn - 1, "endIndex": rn,  # 1-based sheet row -> 0-based
        }}}
        for rn in sorted(to_delete, reverse=True)
    ]
    sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().batchUpdate(
            spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
            body={"requests": requests},
        )
    )
    print(f"Deleted {len(requests)} duplicate rows.")

    # Verify
    after = await sheets_service.get_all_tasks()
    ids = [str(r.get("id") or "").strip() for r in after if str(r.get("id") or "").strip()]
    remaining_dupes = len({i for i in ids if ids.count(i) > 1})
    print(f"After: {len(after)} rows, {remaining_dupes} duplicate UUIDs remaining.")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
