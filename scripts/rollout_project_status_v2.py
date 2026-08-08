"""
Project Status v2 cutover — backup, rebuild in block layout, seed snapshots.
[2026-08-07]

Third and last step of P1/P2, after migrate_project_status_v2.sql and the two
backfills. Turns the generated snapshot that v1 produced into the bidirectional
working document Nechama owns.

FOUR STEPS, IN THIS ORDER, AND THE ORDER IS LOAD-BEARING

  1. PREFLIGHT   prove the schema, the projects and the backfill are all in
                 place, and that the workbook is readable. Nothing is written
                 until every check passes — a rebuild against a half-migrated
                 database would produce a file that reconcile then "corrects".

  2. BACKUP      Drive files.copy to a dated name. The rebuild clears every
                 tab; this is the only undo.

  3. REBUILD     the one and only time a tab is ever cleared again. From here
                 the reconcile engine edits in place.

  4. SEED        read the tabs BACK and write sheet_snapshots from what is
                 actually in the file — never from what we intended to write.

Step 4 is the one that looks skippable and is not. sheet_snapshots is the base
of a three-way merge: with no base, the first reconcile pass sees sheet !=
snapshot on every populated cell, concludes a human typed all of it, pulls the
entire sheet into the database and marks every field sticky. Reading back
rather than trusting the write is the same instinct — if Sheets coerced a value
(a date, a number, a leading apostrophe), the snapshot must record what the
next read will actually see, or every cell diverges on cycle one.

Dry run by default. --apply performs all four steps.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from services.supabase_client import supabase_client  # noqa: E402


def preflight() -> tuple[bool, list[str]]:
    """Everything that must already be true. Returns (ok, lines)."""
    lines, ok = [], True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        lines.append(f"    [{'OK ' if passed else 'FAIL'}] {label}"
                     + (f" — {detail}" if detail else ""))
        ok = ok and passed

    sid = settings.PROJECT_STATUS_SHEET_ID
    check("PROJECT_STATUS_SHEET_ID configured", bool(sid), sid or "unset")

    # Schema: probe a NEW column. A value a prior migration could have set
    # would prove nothing; an absent column raises PostgREST 42703.
    try:
        supabase_client.client.table("canonical_projects").select("objective").limit(1).execute()
        check("migrate_project_status_v2 applied (canonical_projects.objective)", True)
    except Exception as e:                                  # noqa: BLE001
        check("migrate_project_status_v2 applied", False, str(e)[:70])
    try:
        supabase_client.client.table("tasks").select("project_id").limit(1).execute()
        check("tasks.project_id exists", True)
    except Exception as e:                                  # noqa: BLE001
        check("tasks.project_id exists", False, str(e)[:70])

    projects = supabase_client.get_canonical_projects(status=None)
    active = [p for p in projects if (p.get("status") or "active") != "retired"]
    check("projects seeded", len(active) >= 20, f"{len(active)} active")

    grouped = supabase_client.get_open_tasks_by_project()
    attached = sum(len(v) for k, v in grouped.items() if k)
    orphan = sum(len(v) for k, v in grouped.items() if not k)
    check("open tasks attached to a project", orphan == 0,
          f"{attached} attached, {orphan} unattached")

    # Readable workbook. Proves auth + id BEFORE the backup and the clear.
    if sid:
        try:
            from services.google_sheets import sheets_service
            meta = sheets_service._execute_with_retry(
                lambda: sheets_service.service.spreadsheets().get(
                    spreadsheetId=sid, fields="sheets.properties.title"))
            titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
            check("workbook readable", True, f"{len(titles)} tabs")
        except Exception as e:                              # noqa: BLE001
            check("workbook readable", False, str(e)[:70])
    return ok, lines


def backup(sid: str) -> str:
    """files.copy the workbook to a dated name. Returns the new file id."""
    from services.google_drive import drive_service
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = f"CropSight — Projects Status (pre-v2 backup {stamp})"
    created = drive_service._execute_with_retry(
        lambda: drive_service.service.files().copy(
            fileId=sid, body={"name": name},
            supportsAllDrives=True, fields="id,name"))
    return created["id"]


def seed_snapshots(sid: str) -> dict:
    """Read every tab back and write the ps_project / ps_action merge bases.

    Reads through the same parser the reconcile engine uses, so the snapshot is
    keyed off exactly what that engine will see on its first pass.
    """
    from core.dates import parse_human_date
    from services.google_sheets import sheets_service
    # SYSTEM_ACTION, not KIND_ACTION: Row.kind is the CLASSIFICATION
    # ('system_action'), while KIND_ACTION is the raw _kind cell value ('A').
    # Comparing against the raw value skipped every action row on the first
    # cutover run — caught by the written-vs-read-back check.
    from services.project_status_rows import (
        COL_ACTION, COL_COMMENTS, COL_DATE, COL_PRIORITY, COL_RESP, COL_TOPIC,
        PRIORITY_TO_DB, SYSTEM_ACTION, parse_tab, strip_provenance,
    )

    meta = sheets_service._execute_with_retry(
        lambda: sheets_service.service.spreadsheets().get(
            spreadsheetId=sid, fields="sheets.properties.title"))
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])
            if s["properties"]["title"] != "How to use"]

    # `failed` exists because upsert_sheet_snapshot LOGS and returns False
    # rather than raising. Counting calls instead of successes reported 64
    # seeded while the table held 36, and the written-vs-read-back check
    # compared that inflated number against itself and passed.
    out = {"projects": 0, "actions": 0, "skipped": 0, "failed": 0,
           "stale_cleared": 0, "tabs": len(tabs)}
    rendered: set = set()
    for tab in tabs:
        grid = sheets_service._execute_with_retry(
            lambda t=tab: sheets_service.service.spreadsheets().values().get(
                spreadsheetId=sid, range=f"'{t}'!A1:L2000")).get("values", [])
        blocks, orphans, _ = parse_tab(grid)

        for block in blocks:
            proj = block.project
            if not proj or not proj.uid:
                out["skipped"] += 1
                continue
            ok = supabase_client.upsert_ps_project_snapshot(
                project_id=proj.uid,
                sheet_row=proj.row_number,
                sheet_tab=tab,
                objective=proj.values.get("To do"),
                # target_date is a DATE column: the DD/MM/YYYY cell must be
                # normalised, exactly as sheets_sync does for the task surface.
                target_date=parse_human_date(proj.values.get("Date")),
                owner=proj.values.get("Resp."),
                notes=proj.values.get("Comments"),
            )
            out["projects" if ok else "failed"] += 1

            for row in block.actions:
                if row.kind != SYSTEM_ACTION or not row.uid:
                    out["skipped"] += 1
                    continue
                ok = supabase_client.upsert_sheet_snapshot(
                    task_id=row.uid,
                    sheet_row=row.row_number,
                    # The checkbox, not tasks.status: the snapshot records what
                    # the CELL said, so a later tick reads as a change.
                    status="done" if row.checked else "pending",
                    deadline=parse_human_date(row.values.get(COL_DATE)),
                    # The SHEET spelling maps back to the stored letter, so a
                    # seeded snapshot compares equal on the next cycle instead
                    # of reading as a divergence on every single row.
                    priority=PRIORITY_TO_DB.get(
                        str(row.values.get(COL_PRIORITY) or "").strip().lower()),
                    assignee=row.values.get(COL_RESP),
                    title=strip_provenance(row.values.get(COL_ACTION)),
                    label=row.values.get(COL_TOPIC),
                    entity_type="ps_action",
                    notes=row.values.get(COL_COMMENTS),
                    sheet_tab=tab,
                )
                out["actions" if ok else "failed"] += 1
                rendered.add(row.uid)
        out["skipped"] += len(orphans)

    # A snapshot for a row the rebuild no longer renders is stale — the task
    # closed, so it left the view. Leaving it behind makes the written-vs-table
    # check disagree for a reason that is not a fault, which would train us to
    # ignore the one guard that catches real ones.
    try:
        existing = (supabase_client.client.table("sheet_snapshots")
                    .select("id,task_id").eq("entity_type", "ps_action")
                    .execute().data or [])
        for row in existing:
            if row.get("task_id") and row["task_id"] not in rendered:
                supabase_client.client.table("sheet_snapshots").delete().eq(
                    "id", row["id"]).execute()
                out["stale_cleared"] += 1
    except Exception as e:                                  # noqa: BLE001
        print(f"      !! could not clear stale snapshots: {e}")
    return out


async def run(apply_it: bool, skip_backup: bool = False) -> int:
    from processors.project_status import build_status_blocks, title_block
    from services.project_status_sheet import write_project_status_blocks

    print("APPLY" if apply_it else "DRY RUN (nothing written) — re-run with --apply")
    print("=" * 72)

    print("  1. PREFLIGHT")
    ok, lines = preflight()
    for line in lines:
        print(line)
    if not ok:
        print("\n  ABORTED — fix the failures above. Nothing was written.")
        return 1

    pack = build_status_blocks()
    projects = sum(1 for rows in pack.values() for r in rows if r[7] == "P")
    actions = sum(1 for rows in pack.values() for r in rows if r[7] == "A")
    print(f"\n  WOULD WRITE  {projects} project blocks, {actions} actions, "
          f"{len(pack)} tabs")
    for area, rows in pack.items():
        p = sum(1 for r in rows if r[7] == "P")
        a = sum(1 for r in rows if r[7] == "A")
        print(f"      {p:3} projects  {a:3} actions   {area}")

    if not apply_it:
        print("\n  Nothing was written.")
        return 0

    sid = settings.PROJECT_STATUS_SHEET_ID
    print("\n  2. BACKUP")
    if skip_backup:
        # Re-run after a fixed fault: the pre-v2 copy already exists and the
        # file is now rebuilt, so a second "pre-v2 backup" would be a copy of
        # the NEW state under a name claiming to be the old one.
        backup_id = "(skipped — pre-v2 copy already taken)"
        print(f"      {backup_id}")
    else:
        backup_id = backup(sid)
        print(f"      copied to {backup_id}")

    print("\n  3. REBUILD")
    result = await write_project_status_blocks(
        pack,
        {area: title_block(area) for area in pack},
        spreadsheet_id=sid,
        due_soon_days=settings.PROJECT_STATUS_DUE_SOON_DAYS,
    )
    if result.get("error"):
        print(f"      FAILED: {result['error']}")
        print(f"      the pre-cutover copy is {backup_id} — restore from it.")
        return 1
    print(f"      {result['projects']} projects, {result['actions']} actions, "
          f"tabs: {', '.join(result['tabs'])}")
    if result.get("removed_tabs"):
        print(f"      removed stale v1 tab(s): {', '.join(result['removed_tabs'])}")

    print("\n  4. SEED SNAPSHOTS (read-back)")
    seeded = seed_snapshots(sid)
    print(f"      {seeded['projects']} ps_project, {seeded['actions']} ps_action "
          f"across {seeded['tabs']} tabs   "
          f"(skipped {seeded['skipped']}, failed {seeded['failed']}, "
          f"stale cleared {seeded['stale_cleared']})")

    # Counted from the TABLE, not from the loop above. A helper that logs and
    # returns False on error makes an in-process counter a statement of intent;
    # only the row count is evidence.
    def _count(entity: str) -> int:
        return len(supabase_client.client.table("sheet_snapshots")
                   .select("id").eq("entity_type", entity).execute().data or [])

    in_db = {"ps_project": _count("ps_project"), "ps_action": _count("ps_action")}
    print(f"      in the table: {in_db['ps_project']} ps_project, "
          f"{in_db['ps_action']} ps_action")

    if (in_db["ps_project"] != result["projects"]
            or in_db["ps_action"] != result["actions"]
            or seeded["failed"]):
        print(f"      ** MISMATCH — wrote {result['projects']}/{result['actions']}, "
              f"table holds {in_db['ps_project']}/{in_db['ps_action']}. "
              "Investigate BEFORE enabling reconcile.")
        return 1

    print(f"\n  DONE  {result['url']}")
    print("  Next: PROJECT_STATUS_V2_LAYOUT=true, then P3 (reconcile in shadow).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="back up, rebuild and seed for real")
    ap.add_argument("--skip-backup", action="store_true",
                    help="re-run only: the pre-v2 copy already exists")
    args = ap.parse_args()
    return asyncio.run(run(args.apply, args.skip_backup))


if __name__ == "__main__":
    sys.exit(main())
