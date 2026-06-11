"""
Sheets on-demand sync processor (Phase 11 C7).

Computes diffs between Google Sheets and Supabase DB, formats previews,
and applies approved changes. Sheets wins for conflicting values.

Usage:
    from processors.sheets_sync import compute_sheets_diff, apply_sheets_to_db

    diff = await compute_sheets_diff()
    if diff["has_changes"]:
        preview = format_diff_preview(diff)
        # Show preview to Eyal, then on approval:
        result = apply_sheets_to_db(diff)
"""

import logging
from datetime import datetime

from config.settings import settings
from core.dates import parse_human_date
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Fields compared for tasks (ignore formatting, created dates, source)
TASK_COMPARE_FIELDS = ("status", "assignee", "deadline", "priority", "label", "category")

# Fields compared for decisions
DECISION_COMPARE_FIELDS = ("decision_status",)


def _normalize(value: str | None) -> str:
    """Normalize a value for comparison (lowercase, strip whitespace)."""
    if value is None:
        return ""
    return str(value).strip().lower()


def _task_key(task: dict) -> str:
    """Generate a matching key for a task: title + assignee."""
    title = _normalize(task.get("title") or task.get("task", ""))
    assignee = _normalize(task.get("assignee") or task.get("owner", ""))
    return f"{title}|{assignee}"


def _decision_key(decision: dict) -> str:
    """Generate a matching key for a decision: first 100 chars of description."""
    desc = _normalize(decision.get("description") or decision.get("decision", ""))
    return desc[:100]


async def compute_sheets_diff() -> dict:
    """
    Compare Sheets state against DB and compute the diff.

    Returns:
        {
            "has_changes": bool,
            "tasks": {
                "modified": [{"sheets": {...}, "db": {...}, "changes": {...}}],
                "sheets_only": [sheet_task_dict],
                "db_only": [db_task_dict],
                "in_sync": int,
            },
            "decisions": { same structure },
        }
    """
    result = {
        "has_changes": False,
        "tasks": {"modified": [], "sheets_only": [], "db_only": [], "in_sync": 0},
        "decisions": {"modified": [], "sheets_only": [], "db_only": [], "in_sync": 0},
    }

    # --- Tasks ---
    try:
        from services.google_sheets import sheets_service
        sheets_tasks = await sheets_service.get_all_tasks()
    except Exception as e:
        logger.error(f"Failed to read tasks from Sheets: {e}")
        sheets_tasks = []

    # include_archived: an archived DB task whose sheet row hasn't been moved
    # to the Archive tab yet must still MATCH its row — otherwise the row
    # classifies as "new in Sheets" and apply re-creates the task (resurrecting
    # a sanctioned removal under a fresh UUID).
    db_tasks = supabase_client.get_tasks(status=None, limit=500, include_archived=True)

    # Build lookup dicts by matching key
    sheets_by_key = {}
    for st in sheets_tasks:
        key = _task_key(st)
        if key and key != "|":
            sheets_by_key[key] = st

    db_by_key = {}
    for dt in db_tasks:
        key = _task_key(dt)
        if key and key != "|":
            db_by_key[key] = dt

    # Compare
    all_keys = set(sheets_by_key.keys()) | set(db_by_key.keys())
    for key in all_keys:
        in_sheets = key in sheets_by_key
        in_db = key in db_by_key

        if in_sheets and in_db:
            # Both exist — check for differences
            st = sheets_by_key[key]
            dt = db_by_key[key]
            changes = _compare_task(st, dt)
            if changes:
                result["tasks"]["modified"].append({
                    "sheets": st,
                    "db": dt,
                    "changes": changes,
                    "db_id": dt.get("id"),
                })
            else:
                result["tasks"]["in_sync"] += 1
        elif in_sheets:
            result["tasks"]["sheets_only"].append(sheets_by_key[key])
        else:
            result["tasks"]["db_only"].append(db_by_key[key])

    # --- Decisions ---
    try:
        sheets_decisions = await _read_decisions_from_sheets()
    except Exception as e:
        logger.error(f"Failed to read decisions from Sheets: {e}")
        sheets_decisions = []

    db_decisions = supabase_client.list_decisions(limit=500)

    sheets_dec_by_key = {}
    for sd in sheets_decisions:
        key = _decision_key(sd)
        if key:
            sheets_dec_by_key[key] = sd

    db_dec_by_key = {}
    for dd in db_decisions:
        key = _decision_key(dd)
        if key:
            db_dec_by_key[key] = dd

    all_dec_keys = set(sheets_dec_by_key.keys()) | set(db_dec_by_key.keys())
    for key in all_dec_keys:
        in_sheets = key in sheets_dec_by_key
        in_db = key in db_dec_by_key

        if in_sheets and in_db:
            sd = sheets_dec_by_key[key]
            dd = db_dec_by_key[key]
            changes = _compare_decision(sd, dd)
            if changes:
                result["decisions"]["modified"].append({
                    "sheets": sd,
                    "db": dd,
                    "changes": changes,
                    "db_id": dd.get("id"),
                })
            else:
                result["decisions"]["in_sync"] += 1
        elif in_sheets:
            result["decisions"]["sheets_only"].append(sheets_dec_by_key[key])
        else:
            result["decisions"]["db_only"].append(db_dec_by_key[key])

    # --- Duplicate detection (Phase 13) ---
    result["tasks"]["potential_duplicates"] = _detect_duplicate_tasks(db_tasks)

    # Check if any changes exist
    for table in ("tasks", "decisions"):
        if result[table]["modified"] or result[table]["sheets_only"] or result[table]["db_only"]:
            result["has_changes"] = True
            break

    if result["tasks"]["potential_duplicates"]:
        result["has_changes"] = True

    return result


def _detect_duplicate_tasks(tasks: list[dict]) -> list[dict]:
    """
    Detect potential duplicate tasks by fuzzy title matching.

    Compares all open tasks against each other. Two tasks are flagged as
    potential duplicates if they share 60%+ of significant words.

    Returns:
        List of duplicate pairs: [{"task_a": {...}, "task_b": {...}, "overlap": [...]}]
    """
    open_tasks = [t for t in tasks if t.get("status") in ("pending", "in_progress", "overdue")]
    if len(open_tasks) < 2:
        return []

    # Stop words tuned after the 2026-04-11 live audit: generic English +
    # scheduling filler. Without "schedule:", "meeting", "session" most
    # false-positive pairs were two unrelated "Schedule: X" tasks sharing
    # those three tokens as their entire common vocabulary.
    stop_words = {
        "the", "a", "an", "to", "for", "and", "or", "of", "in", "on", "is",
        "it", "we", "with", "from", "by", "at",
        # scheduling filler
        "schedule", "schedule:", "meeting", "meetings", "session", "sessions",
        "call", "sync",
    }
    import re

    def _words(title: str) -> set[str]:
        lowered = (title or "").lower()
        # Strip punctuation so "schedule:" and "schedule" collapse to one token
        cleaned = re.sub(r"[^a-z0-9 ]", " ", lowered)
        return set(cleaned.split()) - stop_words

    duplicates = []
    seen_pairs = set()

    for i, a in enumerate(open_tasks):
        words_a = _words(a.get("title", ""))
        if len(words_a) < 3:
            continue

        for b in open_tasks[i + 1:]:
            words_b = _words(b.get("title", ""))
            if len(words_b) < 3:
                continue

            overlap = words_a & words_b
            min_len = min(len(words_a), len(words_b))
            if min_len > 0 and len(overlap) / min_len >= 0.6:
                pair_key = tuple(sorted([a.get("id", ""), b.get("id", "")]))
                if pair_key not in seen_pairs:
                    seen_pairs.add(pair_key)
                    duplicates.append({
                        "task_a": {
                            "id": a.get("id"),
                            "title": a.get("title", "")[:80],
                            "assignee": a.get("assignee", ""),
                            "status": a.get("status", ""),
                        },
                        "task_b": {
                            "id": b.get("id"),
                            "title": b.get("title", "")[:80],
                            "assignee": b.get("assignee", ""),
                            "status": b.get("status", ""),
                        },
                        "overlap": list(overlap)[:5],
                    })

    return duplicates[:10]  # Cap at 10 pairs


def _compare_task(sheets_task: dict, db_task: dict) -> dict:
    """Compare a Sheets task against its DB counterpart. Returns changed fields."""
    changes = {}

    field_mapping = {
        "status": ("status", "status"),
        "assignee": ("assignee", "assignee"),
        "deadline": ("deadline", "deadline"),
        "priority": ("priority", "priority"),
        "label": ("label", "label"),
        "category": ("category", "category"),
    }
    # PR9: the urgency cell only exists when the sheet flag is on.
    if getattr(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
        field_mapping["urgency"] = ("urgency", "urgency")

    for field, (sheets_key, db_key) in field_mapping.items():
        sheets_val = _normalize(sheets_task.get(sheets_key, ""))
        db_val = _normalize(db_task.get(db_key, ""))
        if sheets_val != db_val and sheets_val:  # Only flag if Sheets has a value
            changes[field] = {"from": db_task.get(db_key, ""), "to": sheets_task.get(sheets_key, "")}

    return changes


def _compare_decision(sheets_dec: dict, db_dec: dict) -> dict:
    """Compare a Sheets decision against its DB counterpart."""
    changes = {}

    sheets_status = _normalize(sheets_dec.get("status", ""))
    db_status = _normalize(db_dec.get("decision_status", ""))
    if sheets_status and sheets_status != db_status:
        changes["decision_status"] = {"from": db_dec.get("decision_status", ""), "to": sheets_dec.get("status", "")}

    return changes


async def _read_decisions_from_sheets() -> list[dict]:
    """Read decisions from the Decisions tab in Google Sheets."""
    from services.google_sheets import sheets_service, DECISION_COLUMNS, DECISION_COL_INDEX

    rows = await sheets_service._read_sheet_range(
        sheet_id=settings.TASK_TRACKER_SHEET_ID,
        range_name="Decisions!A:G",
    )

    if not rows or len(rows) < 2:
        return []

    num_cols = len(DECISION_COLUMNS)
    decisions = []
    for row in rows[1:]:
        while len(row) < num_cols:
            row.append("")

        decisions.append({
            "label": row[DECISION_COL_INDEX["label"]],
            "decision": row[DECISION_COL_INDEX["decision"]],
            "rationale": row[DECISION_COL_INDEX["rationale"]],
            "confidence": row[DECISION_COL_INDEX["confidence"]],
            "source_meeting": row[DECISION_COL_INDEX["source_meeting"]],
            "date": row[DECISION_COL_INDEX["date"]],
            "status": row[DECISION_COL_INDEX["status"]],
        })

    return decisions


def format_diff_preview(diff: dict) -> str:
    """Format the diff as a Telegram-friendly message."""
    if not diff.get("has_changes"):
        return "Sheets and DB are in sync. No changes needed."

    lines = ["<b>Sheets Sync Preview</b>\n"]

    # Tasks
    t = diff["tasks"]
    if t["modified"]:
        lines.append(f"<b>Tasks — Modified ({len(t['modified'])}):</b>")
        for item in t["modified"][:10]:
            title = (item["sheets"].get("task") or item["db"].get("title", "?"))[:50]
            change_parts = []
            for field, vals in item["changes"].items():
                change_parts.append(f"{field}: {vals['from']} → {vals['to']}")
            lines.append(f"  • {title}")
            lines.append(f"    {', '.join(change_parts)}")
        lines.append("")

    if t["sheets_only"]:
        lines.append(f"<b>Tasks — New in Sheets ({len(t['sheets_only'])}):</b>")
        for item in t["sheets_only"][:5]:
            title = item.get("task", "?")[:50]
            assignee = item.get("assignee", "?")
            lines.append(f"  • {title} ({assignee})")
        lines.append("")

    if t["db_only"]:
        lines.append(f"<b>Tasks — In DB only ({len(t['db_only'])}):</b>")
        for item in t["db_only"][:5]:
            title = item.get("title", "?")[:50]
            lines.append(f"  ⚠️ {title} — not in Sheets")
        if len(t["db_only"]) > 5:
            lines.append(f"  ... and {len(t['db_only']) - 5} more")
        lines.append("")

    # Decisions
    d = diff["decisions"]
    if d["modified"]:
        lines.append(f"<b>Decisions — Modified ({len(d['modified'])}):</b>")
        for item in d["modified"][:5]:
            desc = (item["sheets"].get("decision") or item["db"].get("description", "?"))[:50]
            change_parts = [f"{f}: {v['from']} → {v['to']}" for f, v in item["changes"].items()]
            lines.append(f"  • {desc}")
            lines.append(f"    {', '.join(change_parts)}")
        lines.append("")

    if d["sheets_only"]:
        lines.append(f"<b>Decisions — New in Sheets ({len(d['sheets_only'])}):</b>")
        for item in d["sheets_only"][:3]:
            desc = item.get("decision", "?")[:50]
            lines.append(f"  • {desc}")
        lines.append("")

    # Potential duplicates
    dupes = t.get("potential_duplicates", [])
    if dupes:
        lines.append(f"<b>Potential Duplicate Tasks ({len(dupes)}):</b>")
        for dup in dupes[:5]:
            a = dup["task_a"]
            b = dup["task_b"]
            lines.append(f"  • {a['title'][:40]} ({a['assignee']})")
            lines.append(f"    ↔ {b['title'][:40]} ({b['assignee']})")
        lines.append("")

    # Summary
    total_changes = (
        len(t["modified"]) + len(t["sheets_only"]) + len(t["db_only"])
        + len(d["modified"]) + len(d["sheets_only"]) + len(d["db_only"])
    )
    lines.append(f"<i>{total_changes} changes total · {t['in_sync']} tasks in sync · {d['in_sync']} decisions in sync</i>")

    result = "\n".join(lines)
    if len(result) > 4000:
        result = result[:4000] + "\n\n... (truncated)"
    return result


def apply_sheets_to_db(diff: dict) -> dict:
    """
    Apply Sheets changes to the DB. Sheets wins for conflicts.

    Args:
        diff: The diff dict from compute_sheets_diff().

    Returns:
        Summary of applied changes.
    """
    applied = {"tasks_updated": 0, "tasks_created": 0, "decisions_updated": 0, "decisions_created": 0}

    # Cache areas once (resolve_category is called per modified/created task).
    _areas = supabase_client.get_areas()

    # Apply task modifications (Sheets wins)
    for item in diff["tasks"]["modified"]:
        db_id = item.get("db_id")
        if not db_id:
            continue
        update_data = {}
        for field, vals in item["changes"].items():
            update_data[field] = vals["to"]
        # Category carries the Gantt-area taxonomy — canonicalize the edit.
        if "category" in update_data:
            update_data["category"] = supabase_client.resolve_category(
                update_data["category"], areas=_areas
            )
        if "urgency" in update_data:
            u = str(update_data["urgency"]).strip().upper()
            update_data["urgency"] = u if u in ("H", "M", "L") else "M"
        # NEVER let an unparseable date string null out a deadline (2026-06-11
        # incident): drop the deadline change instead of writing garbage/NULL.
        if "deadline" in update_data and update_data["deadline"]:
            parsed = parse_human_date(update_data["deadline"])
            if parsed:
                update_data["deadline"] = parsed
            else:
                logger.warning(
                    f"sync: unparseable deadline {update_data['deadline']!r} "
                    f"for task {db_id} — skipping deadline change"
                )
                del update_data["deadline"]
        if not update_data:
            continue  # only change was an unparseable deadline — nothing to write
        try:
            supabase_client.client.table("tasks").update(update_data).eq("id", db_id).execute()
            applied["tasks_updated"] += 1
        except Exception as e:
            logger.error(f"Failed to update task {db_id}: {e}")

    # Add Sheets-only tasks to DB
    for st in diff["tasks"]["sheets_only"]:
        title = st.get("task", "")
        if not title:
            continue
        if _normalize(st.get("status")) == "archived":
            continue  # a row mid-archive is not a new task
        insert_row = {
            "title": title,
            "assignee": st.get("assignee", ""),
            "status": st.get("status", "pending"),
            "priority": st.get("priority", "M"),
            "deadline": parse_human_date(st.get("deadline")) or None,
            "category": supabase_client.resolve_category(st.get("category"), areas=_areas),
            "label": st.get("label", ""),
        }
        if getattr(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
            u = (st.get("urgency") or "M").strip().upper()
            insert_row["urgency"] = u if u in ("H", "M", "L") else "M"
        try:
            supabase_client.client.table("tasks").insert(insert_row).execute()
            applied["tasks_created"] += 1
        except Exception as e:
            logger.error(f"Failed to create task from Sheets: {e}")

    # Apply decision modifications
    for item in diff["decisions"]["modified"]:
        db_id = item.get("db_id")
        if not db_id:
            continue
        update_data = {}
        for field, vals in item["changes"].items():
            update_data[field] = vals["to"]
        try:
            supabase_client.client.table("decisions").update(update_data).eq("id", db_id).execute()
            applied["decisions_updated"] += 1
        except Exception as e:
            logger.error(f"Failed to update decision {db_id}: {e}")

    # Log the sync action
    total = sum(applied.values())
    if total > 0:
        supabase_client.log_action(
            action="sheets_sync_applied",
            details=applied,
            triggered_by="eyal",
        )
        logger.info(f"Sheets sync applied: {applied}")

        # v2.3 PR 3: observation log — every sync-apply is an approval decision
        # (Eyal explicitly chose to commit the diff). Total > 0 gate avoids
        # logging no-op syncs.
        try:
            supabase_client.log_approval_observation(
                content_type="sheets_sync",
                action="approved",
                final_content={"applied": applied, "total": total},
                context={
                    "task_changes": {
                        "modified": len(diff.get("tasks", {}).get("modified", [])),
                        "created": len(diff.get("tasks", {}).get("sheets_only", [])),
                    },
                    "decision_changes": {
                        "modified": len(diff.get("decisions", {}).get("modified", [])),
                    },
                },
            )
        except Exception as e:
            logger.warning(f"[observation] sheets_sync log failed (non-fatal): {e}")

    return applied


def format_sync_summary(diff: dict) -> str:
    """
    Format a brief sync status for the morning brief.

    Returns empty string if everything is in sync (no noise).
    """
    if not diff.get("has_changes"):
        return ""

    t = diff["tasks"]
    d = diff["decisions"]

    parts = []
    task_changes = len(t["modified"]) + len(t["sheets_only"]) + len(t["db_only"])
    dec_changes = len(d["modified"]) + len(d["sheets_only"]) + len(d["db_only"])

    if task_changes:
        details = []
        if t["modified"]:
            details.append(f"{len(t['modified'])} modified")
        if t["sheets_only"]:
            details.append(f"{len(t['sheets_only'])} new in Sheets")
        if t["db_only"]:
            details.append(f"{len(t['db_only'])} DB-only")
        parts.append(f"Tasks: {', '.join(details)}")

    if dec_changes:
        details = []
        if d["modified"]:
            details.append(f"{len(d['modified'])} modified")
        if d["sheets_only"]:
            details.append(f"{len(d['sheets_only'])} new")
        parts.append(f"Decisions: {', '.join(details)}")

    # Duplicate detection — surface an actionable list, not just a count.
    # 2026-04-11: prior version only showed a count, which was easy to
    # dismiss and did not tell Eyal which rows to act on.
    dupes = t.get("potential_duplicates", [])
    if dupes:
        dup_lines = [f"Potential duplicates ({len(dupes)} pair{'s' if len(dupes) != 1 else ''}):"]
        for dup in dupes[:5]:
            a = dup["task_a"]
            b = dup["task_b"]
            dup_lines.append(
                f"   ↳ {a['title'][:55]} ({a['assignee']})"
            )
            dup_lines.append(
                f"      ↔ {b['title'][:55]} ({b['assignee']})"
            )
        if len(dupes) > 5:
            dup_lines.append(f"   ... and {len(dupes) - 5} more")
        parts.append("\n  ".join(dup_lines))

    if not parts:
        return ""

    return "  • " + "\n  • ".join(parts) + "\n  Reply /sync to review and apply"


# =============================================================================
# Reconcile engine (v3 outputs re-architecture)
# =============================================================================
# DB is the source of truth; the Sheet is an editable downstream view.
#   - CONTENT columns (title/label/source/created) are one-way DB->Sheet.
#   - ACTION fields (status/deadline/priority/assignee) are reconciled with
#     "manual wins & sticks" via a per-task SNAPSHOT (Sheet-now vs snapshot
#     attributes an edit to Eyal). Identity is the task UUID in column J,
#     resolved live at write time. Rule 2 (inference proposes, never clobbers a
#     sticky field) lives in the inference callers (cross_reference), not here.
#   - CATEGORY (2026-06 realignment) carries the Gantt-area taxonomy: a
#     non-blank cell is Eyal's call (canonicalized + pulled); a blank cell is
#     refreshed from the DB. Cells with legacy/sloppy values are rewritten to
#     the canonical area name.
#   - status 'archived' = sanctioned removal: the row moves to the Archive tab
#     and is never resurrected (Eyal sets the status, or asks Gianluigi).
# =============================================================================

_ACTION_FIELDS = ("status", "deadline", "priority", "assignee")
# action field -> google_sheets TASK_COLUMNS key
_ACTION_SHEET_KEY = {
    "status": "status", "deadline": "deadline", "priority": "priority", "assignee": "owner",
}
# content db-field -> (TASK_COLUMNS key, get_all_tasks dict key)
_CONTENT_MAP = {"title": ("task", "task"), "label": ("label", "label")}
# DB-only tasks in these statuses get re-added to the Sheet (done/archived are not resurrected)
_READD_STATUSES = ("pending", "in_progress", "overdue")


async def reconcile_tasks(dry_run: bool = False, shadow: bool | None = None) -> dict:
    """
    Reconcile the Tasks sheet against the DB (v3 engine).

    - Pull Eyal's action-field edits (Sheet-now != snapshot) to the DB + mark
      them sticky (Rule 1); a deadline he types becomes EXPLICIT.
    - Refresh the Sheet from the DB for content + non-edited action fields
      (Rule 4), preserving cells he just changed.
    - Rewrite the per-task snapshot LAST, on success (Rule 3).
    - Sheet rows with no UUID -> create in DB + write the UUID back to col J.
    - DB-only open tasks -> re-added to the Sheet (never treated as deletes, #2).

    shadow / dry_run -> compute + log, no Sheet/DB/snapshot writes. Returns a summary.
    """
    from services.google_sheets import sheets_service, TASK_COLUMNS

    if shadow is None:
        shadow = getattr(settings, "RECONCILE_SHADOW_MODE", True)
    write_allowed = not (dry_run or shadow)
    tab = settings.TASK_TRACKER_TAB_NAME or "Tasks"

    try:
        sheet_tasks = await sheets_service.get_all_tasks()
    except Exception as e:
        logger.error(f"[reconcile] could not read Sheet: {e}")
        return {"error": str(e)}
    db_tasks = supabase_client.get_tasks(
        status=None, limit=1000, include_pending=True, include_archived=True
    )
    snapshots = supabase_client.get_sheet_snapshots()
    # Cache the area list once per cycle — resolve_category would otherwise
    # re-query for every task that carries a Category edit/create.
    _areas_cache = supabase_client.get_areas()

    db_by_id = {t["id"]: t for t in db_tasks if t.get("id")}
    sheet_by_id, creates = {}, []
    for st in sheet_tasks:
        sid = str(st.get("id") or "").strip()
        if sid:
            sheet_by_id[sid] = st
        elif str(st.get("task") or "").strip():
            creates.append(st)

    summary = {"matched": 0, "pulled": 0, "pushed": 0, "created": 0, "readded": 0,
               "archived": 0, "bad_dates": 0, "shadow": shadow, "dry_run": dry_run}
    db_updates: dict[str, dict] = {}   # task_id -> {field: value}
    manual_marks: list[tuple] = []     # (task_id, field)
    cell_writes: list[dict] = []       # {"range": ..., "values": [[v]]}
    snapshot_writes: list[tuple] = []  # (task_id, row, status, deadline, priority, assignee)
    archive_moves: list[dict] = []     # sheet-row dicts to move to the Archive tab

    def _cell(col_key, row, value):
        if row:
            cell_writes.append({
                "range": f"'{tab}'!{TASK_COLUMNS[col_key]}{row}",
                "values": [[value if value is not None else ""]],
            })

    # --- matched tasks (UUID in both) ---
    for sid, st in sheet_by_id.items():
        dt = db_by_id.get(sid)
        if not dt:
            continue  # Sheet UUID the DB doesn't know (superseded/removed) — leave it
        summary["matched"] += 1
        row = st.get("row_number")
        snap = snapshots.get(sid) or {}
        upd, final = {}, {}
        deadline_cell_written = False
        deadline_unparseable = False
        for field in _ACTION_FIELDS:
            sheet_val, snap_val, db_val = st.get(field), snap.get(field), dt.get(field)
            # A non-empty deadline cell that didn't parse to ISO is raw text
            # (get_all_tasks convention). NEVER pull it — that's how the
            # 2026-06-11 NULL-deadline data loss happened. Keep the DB value,
            # leave the cell for Eyal, and flag it in the summary.
            if (field == "deadline" and sheet_val
                    and parse_human_date(sheet_val) is None):
                logger.warning(
                    f"[reconcile] unparseable deadline cell {sheet_val!r} "
                    f"(row {row}, task {sid}) — ignored, fix the cell"
                )
                summary["bad_dates"] += 1
                deadline_unparseable = True
                final[field] = db_val
                continue
            if _normalize(sheet_val) != _normalize(snap_val):
                upd[field] = sheet_val or None          # Eyal edited (Rule 1)
                manual_marks.append((sid, field))
                summary["pulled"] += 1
                final[field] = sheet_val
            elif _normalize(db_val) != _normalize(sheet_val):
                _cell(_ACTION_SHEET_KEY[field], row, db_val)  # DB advanced -> refresh (Rule 4)
                summary["pushed"] += 1
                final[field] = db_val
                if field == "deadline":
                    deadline_cell_written = True
            else:
                final[field] = sheet_val
        # Normalize sloppy-but-valid date cells ("20.6.26" -> "2026-06-20") so
        # every future compare is ISO-vs-ISO. NEVER when the cell was
        # unparseable — that would overwrite Eyal's text with the DB date and
        # destroy the very edit the bad_dates guard just preserved.
        if (not deadline_cell_written and not deadline_unparseable
                and final.get("deadline")
                and st.get("deadline_raw")
                and str(final["deadline"]) != str(st["deadline_raw"])):
            _cell("deadline", row, str(final["deadline"]))
        for db_key, (col_key, sheet_key) in _CONTENT_MAP.items():
            if _normalize(st.get(sheet_key)) != _normalize(dt.get(db_key)):
                _cell(col_key, row, dt.get(db_key))       # content: one-way DB -> Sheet
                summary["pushed"] += 1
        # Urgency is a simple Sheet->DB pull (no snapshot needed — nothing
        # auto-advances it post-extraction, so a Sheet/DB mismatch on a matched
        # task is always Eyal's cell edit). Gated on the K column existing.
        if getattr(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
            s_urg = (st.get("urgency") or "").strip().upper()
            if s_urg in ("H", "M", "L") and s_urg != (dt.get("urgency") or "").strip().upper():
                upd["urgency"] = s_urg
                summary["pulled"] += 1
        # Category (Gantt-area taxonomy): non-blank cell is Eyal's call —
        # canonicalize + pull on mismatch; rewrite the cell when his text
        # resolves to a different canonical name. Blank cell refreshes from DB.
        s_cat = (st.get("category") or "").strip()
        if s_cat:
            canon = supabase_client.resolve_category(s_cat, areas=_areas_cache)
            if _normalize(canon) != _normalize(dt.get("category")):
                upd["category"] = canon
                summary["pulled"] += 1
            if canon != s_cat:
                _cell("category", row, canon)
        elif dt.get("category"):
            _cell("category", row, dt.get("category"))
            summary["pushed"] += 1
        if upd:
            db_updates[sid] = upd
        # 'archived' (typed by Eyal or already set in the DB) -> move the row to
        # the Archive tab; no snapshot (the row is leaving the working view).
        if _normalize(final.get("status")) == "archived":
            archive_moves.append({**st, "status": "archived"})
            summary["archived"] += 1
        else:
            snapshot_writes.append((sid, row, final["status"], final["deadline"],
                                    final["priority"], final["assignee"]))

    # --- Sheet rows with no UUID -> create in DB + write UUID back ---
    for st in creates:
        summary["created"] += 1
        if not write_allowed:
            continue
        try:
            extra = {}
            if getattr(settings, "TASK_SHEET_URGENCY_AREA_ENABLED", False):
                u = (st.get("urgency") or "M").strip().upper()
                extra["urgency"] = u if u in ("H", "M", "L") else "M"
            _deadline = parse_human_date(st.get("deadline"))
            if st.get("deadline") and not _deadline:
                summary["bad_dates"] += 1
                logger.warning(
                    f"[reconcile] unparseable deadline {st.get('deadline')!r} on new "
                    f"Sheet row {st.get('row_number')} — created without deadline"
                )
            created = supabase_client.create_task(
                title=st.get("task", ""), assignee=st.get("assignee", ""),
                priority=st.get("priority") or "M", deadline=_deadline,
                status=st.get("status") or "pending",
                category=supabase_client.resolve_category(st.get("category"), areas=_areas_cache),
                deadline_confidence="EXPLICIT" if _deadline else "NONE",
                **extra,
            )
            new_id = created.get("id")
            if new_id and st.get("row_number"):
                _cell("id", st["row_number"], new_id)
                snapshot_writes.append((new_id, st["row_number"], st.get("status"),
                                        st.get("deadline"), st.get("priority"), st.get("assignee")))
        except Exception as e:
            logger.warning(f"[reconcile] create from Sheet row failed: {e}")

    # --- DB-only open tasks -> re-add to Sheet (never delete, #2) ---
    readd_rows = []
    for tid, dt in db_by_id.items():
        if tid in sheet_by_id:
            continue
        if (dt.get("status") or "pending") not in _READD_STATUSES:
            continue  # don't resurrect done/archived tasks
        if (dt.get("approval_status") or "approved") != "approved":
            # Approval gate: pending-approval tasks surface only when their
            # meeting is approved (the distribution flow adds them then).
            # Re-adding them here was the phantom "readded 5/6/11" loop —
            # every rebuild removed them, every reconcile re-added them.
            continue
        summary["readded"] += 1
        meeting_info = dt.get("meetings") if isinstance(dt.get("meetings"), dict) else {}
        readd_rows.append({
            "priority": dt.get("priority", "M"), "label": dt.get("label", ""),
            "task": dt.get("title", ""), "assignee": dt.get("assignee", ""),
            "deadline": str(dt.get("deadline") or ""), "status": dt.get("status", "pending"),
            "category": dt.get("category", ""),
            "source_meeting": dt.get("source_meeting") or (meeting_info or {}).get("title", ""),
            "created_date": str(dt.get("created_at", ""))[:10], "id": tid,
            # carried through; add_tasks_batch only writes K when the flag is on
            "urgency": dt.get("urgency", "M"),
        })

    if shadow or dry_run:
        logger.info(f"[reconcile][{'shadow' if shadow else 'dry-run'}] {summary}")
        try:
            supabase_client.log_action("shadow_reconcile" if shadow else "reconcile_dryrun",
                                       details=summary, triggered_by="auto")
        except Exception:
            pass
        return summary

    # --- APPLY (write_allowed) ---
    # 1. DB action-field pulls + sticky marks.
    db_update_failed: set[str] = set()
    for tid, upd in db_updates.items():
        try:
            if "deadline" in upd and upd["deadline"]:
                upd["deadline_confidence"] = "EXPLICIT"
            if "deadline" in upd and upd["deadline"] is None:
                # Eyal CLEARED the cell. update_task's deadline kwarg treats
                # None as "not provided", so write the NULL explicitly here —
                # otherwise the clear never lands and Rule 4 refills the cell
                # with the old date next cycle.
                upd.pop("deadline")
                supabase_client.client.table("tasks").update(
                    {"deadline": None, "deadline_confidence": "NONE"}
                ).eq("id", tid).execute()
            if upd:
                supabase_client.update_task(tid, **upd)
            for (mtid, mfield) in manual_marks:
                if mtid == tid:
                    supabase_client.mark_task_field_manual(tid, mfield, "sheet_edit")
        except Exception as e:
            db_update_failed.add(tid)
            logger.warning(f"[reconcile] DB update failed for {tid}: {e}")
    # An archive move is only safe once the DB row actually says 'archived' —
    # otherwise the row gets deleted from the sheet while the task stays open,
    # and the next cycle's re-add resurrects it (archive oscillation).
    if db_update_failed:
        archive_moves = [
            st for st in archive_moves
            if str(st.get("id") or "") not in db_update_failed
        ]
    # 2. Re-add DB-only rows (batched; carries the UUID into col J).
    if readd_rows:
        try:
            await sheets_service.add_tasks_batch(readd_rows)
        except Exception as e:
            logger.warning(f"[reconcile] re-add batch failed: {e}")
    # 3. Single batched Sheet write for all cell refreshes + create-id write-backs.
    if cell_writes:
        try:
            sheets_service.service.spreadsheets().values().batchUpdate(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                body={"valueInputOption": "RAW", "data": cell_writes},
            ).execute()
        except Exception as e:
            logger.error(f"[reconcile] batched Sheet write failed: {e}")
            return {**summary, "error": "sheet_write_failed"}  # do NOT rewrite snapshot
    # 3.5. Move archived rows to the Archive tab. MUST come after the batched
    #      cell writes — deleting rows shifts the row numbers cell_writes used.
    if archive_moves:
        try:
            await sheets_service.archive_task_rows(archive_moves)
        except Exception as e:
            logger.warning(f"[reconcile] archive move failed (rows stay put): {e}")
    # 4. Rewrite snapshots LAST (with a light retry so a transient miss doesn't
    #    leave a stale snapshot that re-attributes the change next cycle, #5).
    for (tid, row, sstatus, sdeadline, spriority, sassignee) in snapshot_writes:
        ok = supabase_client.upsert_sheet_snapshot(tid, row, sstatus, sdeadline, spriority, sassignee)
        if not ok:
            supabase_client.upsert_sheet_snapshot(tid, row, sstatus, sdeadline, spriority, sassignee)

    try:
        supabase_client.log_action("reconcile_applied", details=summary, triggered_by="auto")
    except Exception:
        pass
    logger.info(f"[reconcile] applied: {summary}")
    return summary


# =============================================================================
# Gantt timeframe reconcile (v3 chunk 2) — read-back of Eyal's bar edits.
# =============================================================================
# Reads each tagged topic-lane's active span off the grid ("filled" = non-empty
# text AND a known status color) and pulls Eyal's timeframe edits into gantt_rows
# (manual-wins). Multi-segment (gapped) lanes are FLAGGED for splitting, not
# guessed into one span. The system never repaints his bars — read-back only.

async def reconcile_gantt(dry_run: bool = False, shadow: bool | None = None) -> dict:
    from guardrails.gantt_guard import _load_schema, _load_schema_metadata
    from services.gantt_manager import _get_color_map, _sheets_color_to_hex
    from services.gantt_rows import read_row_tags
    from services.gantt_weeks import week_to_column
    from services.google_sheets import sheets_service

    if shadow is None:
        shadow = getattr(settings, "GANTT_SHADOW_MODE", True)
    write_allowed = not (dry_run or shadow)

    color_vals = {_normalize(v) for v in _get_color_map().values() if v}
    meta = _load_schema_metadata()
    week_offset = meta.get("week_offset", 9)
    first_col = meta.get("first_week_col", "E")
    max_week = meta.get("max_week", 104)
    last_col = week_to_column(max_week, week_offset, first_col)

    sheet_names = sorted({
        r["sheet_name"] for r in _load_schema()
        if r.get("sheet_name") and not r["sheet_name"].startswith("_")
    })
    db_rows = {(r["sheet_name"], r["topic_id"]): r
               for r in supabase_client.get_gantt_rows() if r.get("topic_id")}
    snaps = supabase_client.get_gantt_row_snapshots()

    summary = {"sheets": [], "pulled": 0, "flagged_multigap": 0, "untagged_in_db": 0,
               "shadow": shadow, "dry_run": dry_run}

    for sheet in sheet_names:
        try:
            tags = await read_row_tags(sheet)
        except Exception as e:
            logger.warning(f"[reconcile_gantt] read tags {sheet} failed: {e}")
            continue
        if not tags:
            continue
        try:
            resp = sheets_service.service.spreadsheets().get(
                spreadsheetId=settings.GANTT_SHEET_ID,
                ranges=[f"'{sheet}'!{first_col}1:{last_col}"],
                includeGridData=True,
            ).execute()
            rowdata = resp["sheets"][0]["data"][0].get("rowData", [])
        except Exception as e:
            logger.warning(f"[reconcile_gantt] read grid {sheet} failed: {e}")
            continue

        for row_num, topic_id in tags.items():
            cells = rowdata[row_num - 1].get("values", []) if (row_num - 1) < len(rowdata) else []
            filled = []
            for ci, c in enumerate(cells):
                txt = (c.get("formattedValue", "") or "").strip()
                bg = c.get("effectiveFormat", {}).get("backgroundColor")
                hexv = _sheets_color_to_hex(bg) if bg else ""
                if txt and hexv and _normalize(hexv) in color_vals:
                    filled.append(week_offset + ci)
            if not filled:
                continue
            gr = db_rows.get((sheet, topic_id))
            if not gr:
                summary["untagged_in_db"] += 1
                continue
            ws, we = min(filled), max(filled)
            if set(range(ws, we + 1)) - set(filled):
                summary["flagged_multigap"] += 1  # multi-segment lane — don't guess; flag to split
                continue
            gid = gr["id"]
            snap = snaps.get(gid) or {}
            if snap.get("week_start") == ws and snap.get("week_end") == we:
                continue  # unchanged
            summary["pulled"] += 1
            if write_allowed:
                try:
                    supabase_client.client.table("gantt_rows").update(
                        {"week_start": ws, "week_end": we}
                    ).eq("id", gid).execute()
                    supabase_client.mark_gantt_field_manual(gid, "timeframe", "sheet_edit")
                    supabase_client.upsert_gantt_snapshot(gid, row_num, ws, we)
                except Exception as e:
                    logger.warning(f"[reconcile_gantt] pull {gid} failed: {e}")
        summary["sheets"].append(sheet)

    action = ("shadow_gantt_reconcile" if shadow
              else "gantt_reconcile_dryrun" if dry_run else "gantt_reconcile_applied")
    try:
        supabase_client.log_action(action, details=summary, triggered_by="auto")
    except Exception:
        pass
    logger.info(f"[reconcile_gantt][{action}] {summary}")
    return summary
