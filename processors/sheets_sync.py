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
    # limit must comfortably exceed the live task count (incl. archived) — a
    # truncated DB list drops real tasks from db_by_id, so their sheet rows match
    # nothing and apply would re-CREATE them as duplicates. [audit P1-11]
    db_tasks = supabase_client.get_tasks(status=None, limit=2000, include_archived=True)

    # UUID-FIRST matching. A sheet row carrying its col-J UUID is matched to the
    # DB task by id (exact). The old title+assignee-only key collapsed two tasks
    # that share a title+assignee into ONE key (dict overwrite), so an edit to one
    # row could be applied to the WRONG task, or one side's edit silently dropped.
    # Only sheet rows WITHOUT a usable col-J id fall back to the title+assignee key. [audit P1-03]
    db_by_id = {dt["id"]: dt for dt in db_tasks if dt.get("id")}
    matched_db_ids: set = set()
    sheets_by_key = {}            # only sheet rows lacking a resolvable col-J id

    for st in sheets_tasks:
        sid = (st.get("id") or "").strip()
        if sid and sid in db_by_id:
            dt = db_by_id[sid]
            matched_db_ids.add(sid)
            changes = _compare_task(st, dt)
            if changes:
                result["tasks"]["modified"].append({
                    "sheets": st, "db": dt, "changes": changes, "db_id": dt.get("id"),
                })
            else:
                result["tasks"]["in_sync"] += 1
        else:
            key = _task_key(st)
            if key and key != "|":
                sheets_by_key[key] = st

    # DB tasks already matched by UUID are excluded from the key-based fallback so
    # they can't also surface as db_only.
    db_by_key = {}
    for dt in db_tasks:
        if dt.get("id") in matched_db_ids:
            continue
        key = _task_key(dt)
        if key and key != "|":
            db_by_key[key] = dt

    # Fallback: title+assignee matching for rows without a col-J id (newly added).
    all_keys = set(sheets_by_key.keys()) | set(db_by_key.keys())
    for key in all_keys:
        in_sheets = key in sheets_by_key
        in_db = key in db_by_key

        if in_sheets and in_db:
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
#   - CONTENT columns (title/label) are reconciled like the action fields as of
#     Phase 1 (2026-07): a manual edit wins & sticks via the per-task SNAPSHOT;
#     an untouched cell is refreshed from the DB. (source/created/id stay one-way
#     DB->Sheet and are protected in the Sheet so they can't be hand-edited.)
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
# content db-field -> (TASK_COLUMNS key, get_all_tasks dict key). Reconciled
# snapshot-style (manual-wins-and-sticky) since Phase 1, not one-way DB->Sheet.
_CONTENT_MAP = {"title": ("task", "task"), "label": ("label", "label")}
# DB-only tasks in these statuses get re-added to the Sheet (done/archived are not resurrected)
_READD_STATUSES = ("pending", "in_progress", "overdue")

# Decisions (Phase 2, editable Decisions sheet). Content db-field -> (DECISION_COLUMNS
# key, get_all_decisions dict key). Reconciled snapshot-style (manual-wins-and-sticky).
# Status is handled separately (the monotonic-supersede rule), not here.
_DECISION_CONTENT_MAP = {
    "description": ("decision", "decision"),
    "label": ("label", "label"),
    "rationale": ("rationale", "rationale"),
    "confidence": ("confidence", "confidence"),
}
# A retired decision (superseded/reversed) can never be resurrected to 'active' by
# a stale Sheet cell — the supersession layer stays authoritative for that direction.
_DECISION_RETIRED = ("superseded", "reversed")


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

    # GUARD [2026-07-10 incident]: a transient Google Sheets read can return an
    # EMPTY sheet WITHOUT raising. Reconcile would then see every DB task as
    # "missing" and re-add them all — DUPLICATING the whole sheet (the 293-row /
    # 100-duplicate mess on 2026-07-10). If the sheet reads empty BUT we hold
    # snapshots (proof tasks were synced to this sheet before), the read is bad —
    # ABORT before any processing. (No snapshots = plausibly a fresh/empty sheet,
    # so we don't block genuine first population.)
    if not sheet_tasks and len(snapshots) > 0:
        logger.error(
            f"[reconcile] ABORTED — sheet read returned 0 rows but {len(snapshots)} "
            f"snapshots exist (tasks were synced before). Refusing to reconcile: a "
            f"bad/empty read would mass re-add and duplicate the sheet (transient "
            f"Sheets API read)."
        )
        try:
            supabase_client.log_action(
                "reconcile_aborted_bad_read",
                details={"sheet_rows": 0, "db_tasks": len(db_tasks), "snapshots": len(snapshots)},
                triggered_by="auto",
            )
        except Exception:
            pass
        return {"error": "sheet_read_empty", "sheet_rows": 0, "snapshots": len(snapshots)}

    db_by_id = {t["id"]: t for t in db_tasks if t.get("id")}
    sheet_by_id, creates = {}, []
    for st in sheet_tasks:
        sid = str(st.get("id") or "").strip()
        if sid:
            sheet_by_id[sid] = st
        elif str(st.get("task") or "").strip():
            creates.append(st)

    summary = {"matched": 0, "pulled": 0, "pushed": 0, "created": 0, "readded": 0,
               "archived": 0, "bad_dates": 0, "manual_held": 0,
               "shadow": shadow, "dry_run": dry_run}
    db_updates: dict[str, dict] = {}   # task_id -> {field: value}
    manual_marks: list[tuple] = []     # (task_id, field)
    manual_held: list[tuple] = []      # (task_id, field, db_val, sheet_val) — Rule 4 suppressed
    cell_writes: list[dict] = []       # {"range": ..., "values": [[v]]}
    snapshot_writes: list[tuple] = []  # (task_id, row, status, deadline, priority, assignee, title, label)
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
                if dt.get(f"manual_{field}"):
                    # Rule 2 rail: never clobber a manually-set field. Until
                    # 2026-07-22 the manual_* flags were write-only (one reader in
                    # the whole codebase) and Rule 4 pushed straight over Eyal's
                    # sticky value. The authoritative HUMAN paths (Telegram, MCP)
                    # write the cell as well as the DB, so a DB-only divergence on
                    # a sticky field means a system/inference path wrote it — hold
                    # the human's cell and surface it instead of reverting.
                    summary["manual_held"] += 1
                    manual_held.append((sid, field, db_val, sheet_val))
                    final[field] = sheet_val
                else:
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
        # Content columns (Task text col C, Label col B): reconcile like the
        # action fields. A manual edit — Sheet-now differs from BOTH the snapshot
        # AND the DB — is pulled to the DB and marked sticky (Rule 1); an
        # untouched cell is refreshed from the DB (Rule 4). NEVER pull a blanked
        # cell (would null a task's text/label) — refresh it from the DB instead.
        # The extra "!= DB" guard means a missing/stale snapshot can't be mistaken
        # for an edit (no phantom-pull, audit P1-04). Closes the silent
        # content-revert trap (Eyal's 2026-07-06 /sync incident).
        for db_key, (col_key, sheet_key) in _CONTENT_MAP.items():
            c_sheet, c_snap, c_db = st.get(sheet_key), snap.get(db_key), dt.get(db_key)
            if (str(c_sheet or "").strip()
                    and _normalize(c_sheet) != _normalize(c_snap)
                    and _normalize(c_sheet) != _normalize(c_db)):
                upd[db_key] = c_sheet                      # Eyal edited (Rule 1)
                manual_marks.append((sid, db_key))
                summary["pulled"] += 1
                final[db_key] = c_sheet
            elif _normalize(c_db) != _normalize(c_sheet):
                if dt.get(f"manual_{db_key}"):
                    # Same Rule 2 rail as the action fields above. [2026-07-22]
                    summary["manual_held"] += 1
                    manual_held.append((sid, db_key, c_db, c_sheet))
                    final[db_key] = c_sheet
                else:
                    _cell(col_key, row, c_db)              # DB advanced -> refresh (Rule 4)
                    summary["pushed"] += 1
                    final[db_key] = c_db
            else:
                final[db_key] = c_sheet
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
        # Last Update (col L): one-way DB -> Sheet, system-owned. It mirrors
        # `updated_at`, which is what makes staleness sortable in-sheet — the
        # pressure signal that replaces deadlines for the 75% of tasks that
        # legitimately have none. Never pulled: a human editing this cell is
        # editing a system field, not stating a fact. [2026-07-22]
        if getattr(settings, "TASK_SHEET_LAST_UPDATE_ENABLED", False):
            from services.google_sheets import _fmt_day
            want = _fmt_day(dt.get("updated_at"))
            if want and want != (st.get("last_update") or "").strip():
                _cell("last_update", row, want)
        if upd:
            db_updates[sid] = upd
        # 'archived' (typed by Eyal or already set in the DB) -> move the row to
        # the Archive tab; no snapshot (the row is leaving the working view).
        if _normalize(final.get("status")) == "archived":
            # prior_status must come from the DB, not the sheet row: `st` is
            # about to be stamped 'archived', and the sheet cell already says
            # 'archived' (that IS the removal signal), so the pre-archive value
            # only survives in the DB. Without it Archive can't tell finished
            # work from abandoned work. [2026-07-22]
            archive_moves.append({
                **st, "status": "archived", "prior_status": dt.get("status"),
            })
            summary["archived"] += 1
        else:
            snapshot_writes.append((sid, row, final["status"], final["deadline"],
                                    final["priority"], final["assignee"],
                                    final.get("title"), final.get("label")))

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
                # ATOMICITY: write the col-J UUID back to the sheet row NOW,
                # synchronously, per-create — NOT via the deferred cell_writes
                # batch (flushed at step 3, after an `await add_tasks_batch`).
                # If the process cycles in that window the DB has the task but
                # the sheet row has no UUID, so next reconcile treats the row as
                # new and creates a DUPLICATE. Writing it here means create +
                # writeback land together; on writeback failure we roll the DB
                # create back so the row is retried cleanly instead. [audit P1-02]
                try:
                    await sheets_service._update_cell(
                        settings.TASK_TRACKER_SHEET_ID,
                        f"'{tab}'!{TASK_COLUMNS['id']}{st['row_number']}",
                        new_id,
                    )
                except Exception as we:
                    logger.error(
                        f"[reconcile] col-J UUID writeback failed for new task "
                        f"{new_id} (row {st['row_number']}) — rolling back the DB "
                        f"create so the row retries cleanly: {we}"
                    )
                    try:
                        # Just-created task has no FK children — a plain delete is safe.
                        supabase_client.client.table("tasks").delete().eq(
                            "id", new_id
                        ).execute()
                    except Exception as de:
                        logger.error(
                            f"[reconcile] rollback delete failed for {new_id} — a "
                            f"UUID-less DB task may duplicate next cycle: {de}"
                        )
                    summary["created"] -= 1
                    continue
                snapshot_writes.append((new_id, st["row_number"], st.get("status"),
                                        st.get("deadline"), st.get("priority"), st.get("assignee"),
                                        st.get("task"), st.get("label")))
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
    # SANITY CAP [2026-07-10 incident]: a truncated (non-empty) sheet read would
    # make many matched tasks look "missing" and drive an abnormally large re-add.
    # You never legitimately re-add MORE tasks than the sheet already matched (plus
    # a small floor for genuine first-population). If the re-add count blows past
    # that, the read is suspect — SKIP the append (never duplicate the sheet) and
    # flag it loudly. The safe pulls/pushes on the rows that DID read still apply.
    _readd_cap = max(30, len(sheet_by_id))
    if len(readd_rows) > _readd_cap:
        logger.error(
            f"[reconcile] SKIPPED re-add of {len(readd_rows)} rows — exceeds the "
            f"sanity cap ({_readd_cap}) vs {len(sheet_by_id)} matched. Suspected "
            f"truncated Sheets read; refusing to append (would duplicate the sheet)."
        )
        try:
            supabase_client.log_action(
                "reconcile_readd_capped",
                details={"readd": len(readd_rows), "matched": len(sheet_by_id), "cap": _readd_cap},
                triggered_by="auto",
            )
        except Exception:
            pass
        readd_rows = []
        summary["readded"] = 0
    if readd_rows:
        try:
            await sheets_service.add_tasks_batch(readd_rows)
            # Seed a snapshot per re-added row from the values we just wrote.
            # Without it next cycle reads snap={} → every action field compares
            # unequal to None → pulled as a phantom "Eyal edit" + marked manual,
            # freezing the field against future DB→Sheet refresh. [audit P1-04]
            for rr in readd_rows:
                rid = rr.get("id")
                if rid:
                    supabase_client.upsert_sheet_snapshot(
                        rid, None, rr.get("status"), rr.get("deadline"),
                        rr.get("priority"), rr.get("assignee"),
                        rr.get("task"), rr.get("label"),
                    )
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
            await sheets_service.archive_task_rows(archive_moves, reason="manual")
        except Exception as e:
            # archive_task_rows fires its own CRITICAL with the exact rows/UUIDs
            # when the append-then-delete move only half-completes; it self-heals
            # next cycle (idempotent append + delete retry). Nothing to roll back
            # here — archived rows are not in snapshot_writes.
            logger.error(f"[reconcile] archive move incomplete (see CRITICAL above): {e}")
    # 4. Rewrite snapshots LAST (with a light retry so a transient miss doesn't
    #    leave a stale snapshot that re-attributes the change next cycle, #5).
    for (tid, row, sstatus, sdeadline, spriority, sassignee, stitle, slabel) in snapshot_writes:
        if tid in db_update_failed:
            # The DB write for this row failed — do NOT advance its snapshot to the
            # edited value. If we did, next cycle would see sheet==snapshot, treat
            # the (stale) DB as authoritative, and overwrite Eyal's edit back out of
            # the sheet. Leaving the snapshot stale re-detects the edit and retries
            # the pull next cycle instead of silently reverting it (audit AD-01).
            logger.warning(
                f"[reconcile] NOT advancing snapshot for {tid} — its DB update "
                "failed; edit will be retried next cycle."
            )
            continue
        ok = supabase_client.upsert_sheet_snapshot(
            tid, row, sstatus, sdeadline, spriority, sassignee, stitle, slabel)
        if not ok:
            supabase_client.upsert_sheet_snapshot(
                tid, row, sstatus, sdeadline, spriority, sassignee, stitle, slabel)

    # Surface any Rule 4 pushes we suppressed to protect a sticky field. Silent
    # divergence is how the old clobber went unnoticed for months — name it.
    if manual_held:
        summary["manual_held_fields"] = [
            {"task_id": t, "field": f, "db": str(d or ""), "sheet": str(s or "")}
            for (t, f, d, s) in manual_held[:20]
        ]
        logger.warning(
            f"[reconcile] held {len(manual_held)} manually-set field(s) against a "
            f"DB-side change (Sheet value kept): "
            + ", ".join(f"{t[:8]}.{f}" for (t, f, _, _) in manual_held[:10])
        )

    try:
        supabase_client.log_action("reconcile_applied", details=summary, triggered_by="auto")
    except Exception:
        pass
    logger.info(f"[reconcile] applied: {summary}")
    return summary


_MEETING_CONTENT_MAP = {
    "title": "title",
    "label": "label",
    "led_by": "led_by",
    "participants": "participants",
}


def _meeting_participants_to_list(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


async def reconcile_meetings(dry_run: bool = False, shadow: bool | None = None) -> dict:
    """Reconcile the Meetings tab against follow_up_meetings.

    Fourth reconcile entity, UUID-keyed on col J. Same three rules as tasks:
      - Rule 1: Sheet-now != snapshot -> Eyal/Nechama edited -> pull + mark sticky
      - Rule 2: a manually-set field is never reverted by a DB-side change
      - Rule 4: otherwise refresh the cell from the DB
      - snapshot rewritten LAST, and skipped when the DB write failed

    Two deliberate differences:
      - Blank-id rows ARE created (unlike decisions, which need a source
        meeting): source_meeting_id is nullable, so a meeting typed straight
        into the Sheet is legitimate. The UUID is written back SYNCHRONOUSLY and
        the create rolled back if that writeback fails — the same guard the task
        create path uses, and the reason the old "Schedule: X" rows duplicated
        forever (they never got a UUID at all).
      - Status is MONOTONIC: a stale cell can never move a meeting backwards
        (held -> scheduled), because the meeting already happened.
    """
    from services.google_sheets import (
        sheets_service, MEETING_COLUMNS, MEETING_TAB_NAME,
        MEETING_STATUSES, MEETING_STATUS_ORDER,
    )

    if not getattr(settings, "MEETING_RECONCILE_ENABLED", False):
        return {"skipped": "MEETING_RECONCILE_ENABLED off"}
    if shadow is None:
        shadow = getattr(settings, "MEETING_RECONCILE_SHADOW_MODE", True)
    write_allowed = not (dry_run or shadow)

    try:
        sheet_rows = await sheets_service.get_all_meetings()
    except Exception as e:
        logger.error(f"[meeting-reconcile] could not read Sheet: {e}")
        return {"error": str(e)}

    db_rows = supabase_client.list_follow_up_meetings(limit=2000, include_pending=True)
    snapshots = supabase_client.get_meeting_snapshots()

    # Same bad-read guard as tasks/decisions: an empty read WITH snapshots means
    # the read failed, not that the tab is empty. Re-adding everything would
    # duplicate the tab (the 2026-07-10 incident class).
    if not sheet_rows and len(snapshots) > 0:
        logger.error(
            f"[meeting-reconcile] ABORTED — sheet read returned 0 rows but "
            f"{len(snapshots)} snapshots exist."
        )
        return {"error": "sheet_read_empty", "snapshots": len(snapshots)}

    db_by_id = {m["id"]: m for m in db_rows if m.get("id")}
    sheet_by_id, creates = {}, []
    for sm in sheet_rows:
        sid = str(sm.get("id") or "").strip()
        if sid:
            sheet_by_id[sid] = sm
        elif str(sm.get("title") or "").strip():
            creates.append(sm)

    summary = {"matched": 0, "pulled": 0, "pushed": 0, "created": 0, "readded": 0,
               "manual_held": 0, "status_guarded": 0, "bad_dates": 0,
               "shadow": shadow, "dry_run": dry_run}
    manual_held: list[tuple] = []
    cell_writes: list[dict] = []
    snapshot_writes: list[tuple] = []
    db_updates: dict[str, dict] = {}
    manual_marks: list[tuple] = []

    def _cell(col_key, row, value):
        if row:
            cell_writes.append({
                "range": f"'{MEETING_TAB_NAME}'!{MEETING_COLUMNS[col_key]}{row}",
                "values": [[value if value is not None else ""]],
            })

    for mid, sm in sheet_by_id.items():
        dm = db_by_id.get(mid)
        if not dm:
            continue
        summary["matched"] += 1
        row = sm.get("row_number")
        snap = snapshots.get(mid) or {}
        upd, final = {}, {}

        for field, sheet_key in _MEETING_CONTENT_MAP.items():
            s_val = sm.get(sheet_key)
            snap_val = snap.get(field)
            d_val = dm.get(field)
            if field == "participants":
                # Stored as TEXT[] in the DB, rendered comma-separated in the cell.
                d_val = ", ".join(d_val) if isinstance(d_val, list) else (d_val or "")
            if (str(s_val or "").strip()
                    and _normalize(s_val) != _normalize(snap_val)
                    and _normalize(s_val) != _normalize(d_val)):
                upd[field] = (_meeting_participants_to_list(s_val)
                              if field == "participants" else s_val)
                manual_marks.append((mid, field))
                summary["pulled"] += 1
                final[field] = s_val
            elif _normalize(d_val) != _normalize(s_val):
                if dm.get(f"manual_{field}"):
                    summary["manual_held"] += 1
                    manual_held.append((mid, field, d_val, s_val))
                    final[field] = s_val
                else:
                    _cell(sheet_key, row, d_val)
                    summary["pushed"] += 1
                    final[field] = d_val
            else:
                final[field] = s_val

        # --- proposed date (unparseable cells are never pulled) ---
        raw_date = sm.get("proposed_date_raw")
        s_date = sm.get("proposed_date")
        if raw_date and parse_human_date(raw_date) is None:
            logger.warning(
                f"[meeting-reconcile] unparseable date {raw_date!r} (row {row}) — ignored"
            )
            summary["bad_dates"] += 1
            final["proposed_date"] = dm.get("proposed_date")
        else:
            d_date = str(dm.get("proposed_date") or "")[:10]
            snap_date = str(snap.get("proposed_date") or "")[:10]
            if _normalize(s_date) != _normalize(snap_date):
                upd["proposed_date"] = s_date or None
                manual_marks.append((mid, "proposed_date"))
                summary["pulled"] += 1
                final["proposed_date"] = s_date
            elif _normalize(d_date) != _normalize(s_date):
                if dm.get("manual_proposed_date"):
                    summary["manual_held"] += 1
                    manual_held.append((mid, "proposed_date", d_date, s_date))
                    final["proposed_date"] = s_date
                else:
                    _cell("proposed_date", row, d_date)
                    summary["pushed"] += 1
                    final["proposed_date"] = d_date
            else:
                final["proposed_date"] = s_date

        # --- status: MONOTONIC. A meeting that was held cannot become merely
        #     scheduled again because a stale cell says so. Forward moves pull. ---
        s_status = (sm.get("status") or "").strip().lower()
        d_status = (dm.get("status") or "not_scheduled").strip().lower()
        snap_status = (snap.get("status") or "").strip().lower()
        if s_status and s_status not in MEETING_STATUSES:
            logger.warning(f"[meeting-reconcile] unknown status {s_status!r} (row {row})")
            s_status = ""
        if s_status and s_status != snap_status and s_status != d_status:
            if MEETING_STATUS_ORDER.get(s_status, 0) >= MEETING_STATUS_ORDER.get(d_status, 0):
                upd["status"] = s_status
                manual_marks.append((mid, "status"))
                summary["pulled"] += 1
                final["status"] = s_status
            else:
                summary["status_guarded"] += 1
                _cell("status", row, d_status)
                final["status"] = d_status
        elif d_status != s_status:
            _cell("status", row, d_status)
            summary["pushed"] += 1
            final["status"] = d_status
        else:
            final["status"] = s_status

        if upd:
            db_updates[mid] = upd
        snapshot_writes.append((
            mid, row, final.get("title"), final.get("label"), final.get("led_by"),
            final.get("proposed_date"), final.get("participants"), final.get("status"),
        ))

    # --- hand-added rows (no UUID) -> create in DB + write the UUID back ---
    if write_allowed:
        for sm in creates:
            try:
                created = supabase_client.create_follow_up_meeting_manual(
                    title=sm.get("title") or "",
                    led_by=sm.get("led_by") or "",
                    proposed_date=(sm.get("proposed_date") or None),
                    participants=_meeting_participants_to_list(sm.get("participants")),
                    label=sm.get("label") or "",
                    status=(sm.get("status") or "not_scheduled").strip().lower(),
                )
                if not created:
                    continue
                new_id = created["id"]
                row = sm.get("row_number")
                try:
                    # SYNCHRONOUS writeback, then roll back on failure — a row
                    # left without its UUID is re-created on every subsequent
                    # run, which is precisely how "Schedule: X" rows multiplied.
                    await sheets_service._update_cell(
                        sheet_id=settings.TASK_TRACKER_SHEET_ID,
                        range_name=f"'{MEETING_TAB_NAME}'!{MEETING_COLUMNS['id']}{row}",
                        value=new_id,
                    )
                except Exception as we:
                    logger.error(
                        f"[meeting-reconcile] UUID writeback failed for new row {row} "
                        f"— rolling back the DB create: {we}"
                    )
                    supabase_client.client.table("follow_up_meetings").delete().eq(
                        "id", new_id).execute()
                    continue
                summary["created"] += 1
                supabase_client.upsert_meeting_snapshot(
                    new_id, row, created.get("title"), created.get("label"),
                    created.get("led_by"), str(created.get("proposed_date") or "")[:10],
                    ", ".join(created.get("participants") or []), created.get("status"),
                )
            except Exception as e:
                logger.error(f"[meeting-reconcile] create failed for row {sm.get('row_number')}: {e}")

    # --- DB-only meetings -> re-add to the Sheet (never treated as deletes) ---
    missing = [m for m in db_rows
               if m.get("id") and m["id"] not in sheet_by_id
               and (m.get("status") or "not_scheduled") != "dropped"]
    _readd_cap = max(30, len(sheet_by_id))
    if len(missing) > _readd_cap:
        logger.error(
            f"[meeting-reconcile] re-add of {len(missing)} rows exceeds cap "
            f"{_readd_cap} — skipping (bad-read safety)."
        )
        missing = []
    if missing and write_allowed:
        await sheets_service.add_meetings_batch_to_sheet(missing)
        summary["readded"] = len(missing)

    if not write_allowed:
        logger.info(f"[meeting-reconcile][{'shadow' if shadow else 'dry-run'}] {summary}")
        return summary

    # --- apply DB updates ---
    failed: set[str] = set()
    for mid, upd in db_updates.items():
        try:
            supabase_client.update_follow_up_meeting(mid, **upd)
        except Exception as e:
            failed.add(mid)
            logger.error(f"[meeting-reconcile] DB update failed for {mid}: {e}")
    for mid, field in manual_marks:
        if mid not in failed:
            supabase_client.mark_meeting_field_manual(mid, field, "sheet_edit")

    if cell_writes:
        try:
            sheets_service.service.spreadsheets().values().batchUpdate(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                body={"valueInputOption": "RAW", "data": cell_writes},
            ).execute()
        except Exception as e:
            logger.error(f"[meeting-reconcile] batched Sheet write failed: {e}")
            return {**summary, "error": "sheet_write_failed"}  # do NOT advance snapshots

    for (mid, row, title, label, led_by, pdate, parts, status) in snapshot_writes:
        if mid in failed:
            logger.warning(
                f"[meeting-reconcile] NOT advancing snapshot for {mid} — its DB "
                "update failed; the edit retries next cycle."
            )
            continue
        supabase_client.upsert_meeting_snapshot(
            mid, row, title, label, led_by, pdate, parts, status)

    if manual_held:
        summary["manual_held_fields"] = [
            {"meeting_id": m, "field": f, "db": str(d or ""), "sheet": str(s or "")}
            for (m, f, d, s) in manual_held[:20]
        ]
    try:
        supabase_client.log_action("meeting_reconcile_applied", details=summary,
                                   triggered_by="auto")
    except Exception:
        pass
    logger.info(f"[meeting-reconcile] applied: {summary}")
    return summary


async def reconcile_decisions(dry_run: bool = False, shadow: bool | None = None) -> dict:
    """Reconcile the Decisions sheet against the DB (Phase 2 engine).

    Mirrors reconcile_tasks, UUID-keyed on col H:
    - Pull Eyal's content edits (Sheet-now != snapshot AND != DB) to the DB + mark
      sticky (Rule 1); refresh untouched cells from the DB (Rule 4); rewrite the
      per-decision snapshot LAST on success (Rule 3).
    - Status has the MONOTONIC-SUPERSEDE guard: a stale Sheet 'active' cell can
      never un-retire a DB superseded/reversed decision (the supersession layer
      owns that direction). A deliberate forward hand-retire (active -> superseded)
      still pulls.
    - DB-only decisions -> re-added to the Sheet (never treated as deletes).

    FIRST CUT: edits/refreshes EXISTING decisions only. Blank-id (hand-authored)
    rows are counted + LEFT, not created (create needs a source meeting). Gated on
    DECISION_RECONCILE_ENABLED — a no-op until cutover.
    """
    from services.google_sheets import (
        sheets_service, DECISION_COLUMNS, DECISION_ID_COLUMN,
    )

    if not getattr(settings, "DECISION_RECONCILE_ENABLED", False):
        return {"skipped": "DECISION_RECONCILE_ENABLED off"}
    if shadow is None:
        shadow = False  # the enable flag IS the go-live switch; no separate shadow
    write_allowed = not (dry_run or shadow)

    try:
        sheet_decisions = await sheets_service.get_all_decisions()
    except Exception as e:
        logger.error(f"[decision-reconcile] could not read Sheet: {e}")
        return {"error": str(e)}
    db_decisions = supabase_client.list_decisions(
        limit=2000, include_pending=True, include_superseded=True
    )
    snapshots = supabase_client.get_decision_snapshots()

    # GUARD [mirror 2026-07-10 task incident]: a transient empty read would make
    # every DB decision look "missing" and re-add them all -> duplicate the sheet.
    if not sheet_decisions and len(snapshots) > 0:
        logger.error(
            f"[decision-reconcile] ABORTED — sheet read 0 rows but {len(snapshots)} "
            f"snapshots exist. Refusing (a bad read would mass re-add + duplicate)."
        )
        try:
            supabase_client.log_action(
                "decision_reconcile_aborted_bad_read",
                details={"sheet_rows": 0, "snapshots": len(snapshots)},
                triggered_by="auto",
            )
        except Exception:
            pass
        return {"error": "sheet_read_empty", "snapshots": len(snapshots)}

    db_by_id = {d["id"]: d for d in db_decisions if d.get("id")}
    sheet_by_id, blank_id = {}, 0
    blank_rows: list[dict] = []
    for sd in sheet_decisions:
        sid = str(sd.get("id") or "").strip()
        if sid:
            sheet_by_id[sid] = sd
        elif str(sd.get("decision") or "").strip():
            blank_id += 1
            blank_rows.append(sd)

    summary = {"matched": 0, "pulled": 0, "pushed": 0, "readded": 0,
               "blank_id": blank_id, "status_guarded": 0, "manual_held": 0,
               "shadow": shadow, "dry_run": dry_run}
    manual_held: list[tuple] = []      # (decision_id, field, db_val, sheet_val)

    # CUTOVER BOOTSTRAP: pre-cutover state — the sheet still holds the historical
    # A:G rows (decision text present) but NONE carry an id, and no snapshots exist
    # yet (the flag was just flipped). Re-adding would DUPLICATE every decision.
    # Instead do ONE full rebuild to write the col-H ids from the DB + seed
    # snapshots, then return — the next reconcile keys on the ids normally. This
    # replaces the fragile "manually trigger a prod rebuild" cutover step.
    if blank_id > 0 and not sheet_by_id and not snapshots:
        approved = [d for d in db_decisions
                    if (d.get("approval_status") or "approved") == "approved"]
        summary["bootstrapped"] = len(approved)
        if dry_run or shadow:
            logger.info(f"[decision-reconcile][{'shadow' if shadow else 'dry-run'}] would bootstrap {summary}")
            return summary
        try:
            await sheets_service.rebuild_decisions_sheet(approved)
            for d in approved:
                if d.get("id"):
                    supabase_client.upsert_decision_snapshot(
                        d["id"], None, d.get("description"), d.get("label"),
                        d.get("rationale"), d.get("confidence"), d.get("decision_status"))
        except Exception as e:
            logger.error(f"[decision-reconcile] bootstrap rebuild failed: {e}")
            return {**summary, "error": "bootstrap_failed"}
        try:
            supabase_client.log_action("decision_reconcile_bootstrapped",
                                       details=summary, triggered_by="auto")
        except Exception:
            pass
        logger.info(f"[decision-reconcile] bootstrapped col-H ids + snapshots: {summary}")
        return summary

    db_updates: dict[str, dict] = {}
    manual_marks: list[tuple] = []
    cell_writes: list[dict] = []
    snapshot_writes: list[tuple] = []

    def _cell(col_key, row, value):
        if row:
            cell_writes.append({
                "range": f"Decisions!{DECISION_COLUMNS[col_key]}{row}",
                "values": [[value if value is not None else ""]],
            })

    for sid, sd in sheet_by_id.items():
        dd = db_by_id.get(sid)
        if not dd:
            continue  # Sheet id the DB doesn't know — leave it
        summary["matched"] += 1
        row = sd.get("row_number")
        snap = snapshots.get(sid) or {}
        upd, final = {}, {}

        # --- content fields (description / label / rationale / confidence) ---
        # NOTE: use _normalize DIRECTLY (it maps None -> ""); wrapping in str()
        # first turns None into "None" and makes a null DB field never match a
        # blank sheet cell -> a permanent push-churn loop (2026-07-11 cutover bug).
        for db_key, (col_key, sheet_key) in _DECISION_CONTENT_MAP.items():
            c_sheet, c_snap, c_db = sd.get(sheet_key), snap.get(db_key), dd.get(db_key)
            if (_normalize(c_sheet)
                    and _normalize(c_sheet) != _normalize(c_snap)
                    and _normalize(c_sheet) != _normalize(c_db)):
                val = c_sheet
                if db_key == "confidence":
                    try:
                        val = int(c_sheet)
                    except (TypeError, ValueError):
                        # Junk confidence cell (e.g. a stale "None" the old rebuild
                        # wrote) — don't pull garbage; refresh it from the DB so the
                        # cell self-heals to a number or blank.
                        _cell(col_key, row, c_db)
                        summary["pushed"] += 1
                        final[db_key] = c_db
                        continue
                upd[db_key] = val                      # Eyal edited (Rule 1)
                manual_marks.append((sid, db_key))
                summary["pulled"] += 1
                final[db_key] = val
            elif _normalize(c_db) != _normalize(c_sheet):
                if dd.get(f"manual_{db_key}"):
                    # Same Rule 2 rail as reconcile_tasks: a sticky field is never
                    # reverted by a DB-side change. [2026-07-22]
                    summary["manual_held"] += 1
                    manual_held.append((sid, db_key, c_db, c_sheet))
                    final[db_key] = c_sheet
                else:
                    _cell(col_key, row, c_db)          # DB advanced -> refresh (Rule 4)
                    summary["pushed"] += 1
                    final[db_key] = c_db
            else:
                final[db_key] = c_sheet

        # --- status (monotonic-supersede rule) ---
        s_status = _normalize(sd.get("status"))
        snap_status = _normalize(snap.get("decision_status"))
        db_status = _normalize(dd.get("decision_status"))
        if db_status in _DECISION_RETIRED and s_status == "active":
            # stale/careless cell — NEVER resurrect. Refresh Sheet <- DB.
            _cell("status", row, dd.get("decision_status"))
            summary["status_guarded"] += 1
            final["decision_status"] = dd.get("decision_status")
        elif s_status and s_status != snap_status and s_status != db_status:
            upd["decision_status"] = s_status          # forward hand-retire (Rule 1)
            manual_marks.append((sid, "status"))
            summary["pulled"] += 1
            final["decision_status"] = s_status
        elif db_status != s_status:
            _cell("status", row, dd.get("decision_status"))  # DB advanced -> refresh
            summary["pushed"] += 1
            final["decision_status"] = dd.get("decision_status")
        else:
            final["decision_status"] = dd.get("decision_status") or sd.get("status")

        if upd:
            db_updates[sid] = upd
        snapshot_writes.append((sid, row, final.get("description"), final.get("label"),
                                final.get("rationale"), final.get("confidence"),
                                final.get("decision_status")))

    # --- DB-only approved decisions -> re-add to the Sheet (never delete) ---
    readd_rows = []
    for did, dd in db_by_id.items():
        if did in sheet_by_id:
            continue
        if (dd.get("approval_status") or "approved") != "approved":
            continue
        summary["readded"] += 1
        readd_rows.append(dd)

    if shadow or dry_run:
        logger.info(f"[decision-reconcile][{'shadow' if shadow else 'dry-run'}] {summary}")
        try:
            supabase_client.log_action(
                "decision_shadow_reconcile" if shadow else "decision_reconcile_dryrun",
                details=summary, triggered_by="auto")
        except Exception:
            pass
        return summary

    # --- APPLY ---
    # Hand-added decision rows: create them, instead of counting them forever.
    #
    # The first cut deliberately left blank-id rows alone because "a decision
    # needs a source meeting". That was defensible when the tab was read-mostly,
    # but the tab is advertised as editable, so those rows just accumulated —
    # `blank_id` sat at 10 with nothing ever consuming it. source_meeting_id is
    # nullable, so a decision typed straight into the Sheet is legitimate; it is
    # approved on arrival for the same reason debrief items are (a human typing
    # it IS the approval). The UUID is written back synchronously and the create
    # rolled back if that fails, so a row can never be created twice.
    # [2026-07-22]
    for sd in blank_rows:
        row_no = sd.get("row_number")
        if not row_no:
            continue
        try:
            created = supabase_client.create_manual_decision(
                description=sd.get("decision") or "",
                label=sd.get("label") or "",
                rationale=sd.get("rationale") or "",
                confidence=sd.get("confidence"),
                decision_status=(sd.get("status") or "active").strip().lower(),
            )
            if not created:
                continue
            new_id = created["id"]
            try:
                await sheets_service._update_cell(
                    sheet_id=settings.TASK_TRACKER_SHEET_ID,
                    range_name=f"Decisions!{DECISION_ID_COLUMN}{row_no}",
                    value=new_id,
                )
            except Exception as we:
                logger.error(
                    f"[decision-reconcile] UUID writeback failed for new row "
                    f"{row_no} — rolling back the DB create: {we}"
                )
                supabase_client.client.table("decisions").delete().eq(
                    "id", new_id).execute()
                continue
            summary["created"] = summary.get("created", 0) + 1
            summary["blank_id"] -= 1
            supabase_client.upsert_decision_snapshot(
                new_id, row_no, created.get("description"), created.get("label"),
                created.get("rationale"), created.get("confidence"),
                created.get("decision_status"),
            )
        except Exception as e:
            logger.error(f"[decision-reconcile] create failed for row {row_no}: {e}")

    decision_update_failed: set[str] = set()
    for did, upd in db_updates.items():
        try:
            supabase_client.update_decision(did, **upd)
            for (mid, mfield) in manual_marks:
                if mid == did:
                    supabase_client.mark_decision_field_manual(did, mfield, "sheet_edit")
            # Keep the semantic index in sync with sheet edits pulled to the DB —
            # the reconcile path was the one decision-edit path not yet hooked.
            # [semantic-index dual-side gap closed, 2026-07-14]
            from processors.semantic_index import schedule_reindex_decision
            schedule_reindex_decision(did)
        except Exception as e:
            decision_update_failed.add(did)
            logger.warning(f"[decision-reconcile] DB update failed for {did}: {e}")

    # Re-add DB-only rows. SANITY CAP [mirror 2026-07-10]: a truncated read makes
    # matched decisions look missing; never re-add more than the sheet matched.
    _readd_cap = max(30, len(sheet_by_id))
    if len(readd_rows) > _readd_cap:
        logger.error(
            f"[decision-reconcile] SKIPPED re-add of {len(readd_rows)} rows — exceeds "
            f"cap ({_readd_cap}) vs {len(sheet_by_id)} matched (suspected truncated read)."
        )
        try:
            supabase_client.log_action(
                "decision_reconcile_readd_capped",
                details={"readd": len(readd_rows), "matched": len(sheet_by_id), "cap": _readd_cap},
                triggered_by="auto")
        except Exception:
            pass
        readd_rows = []
        summary["readded"] = 0
    for dd in readd_rows:
        try:
            meeting_info = dd.get("meetings") if isinstance(dd.get("meetings"), dict) else {}
            src = dd.get("source_meeting") or (meeting_info or {}).get("title", "")
            await sheets_service.add_decisions_batch_to_sheet(
                [dd], src, str(dd.get("created_at", ""))[:10])
            if dd.get("id"):
                supabase_client.upsert_decision_snapshot(
                    dd["id"], None, dd.get("description"), dd.get("label"),
                    dd.get("rationale"), dd.get("confidence"), dd.get("decision_status"))
        except Exception as e:
            logger.warning(f"[decision-reconcile] re-add failed for {dd.get('id')}: {e}")

    if cell_writes:
        try:
            sheets_service.service.spreadsheets().values().batchUpdate(
                spreadsheetId=settings.TASK_TRACKER_SHEET_ID,
                body={"valueInputOption": "RAW", "data": cell_writes},
            ).execute()
        except Exception as e:
            logger.error(f"[decision-reconcile] batched Sheet write failed: {e}")
            return {**summary, "error": "sheet_write_failed"}  # do NOT rewrite snapshot

    # Rewrite snapshots LAST (one light retry, mirror reconcile_tasks).
    for (did, row, sdesc, slabel, srat, sconf, sstatus) in snapshot_writes:
        if did in decision_update_failed:
            # DB write failed — leave the snapshot stale so the edit is re-detected
            # and retried next cycle, not silently reverted (audit AD-01).
            logger.warning(
                f"[decision-reconcile] NOT advancing snapshot for {did} — DB update "
                "failed; edit will be retried next cycle."
            )
            continue
        ok = supabase_client.upsert_decision_snapshot(did, row, sdesc, slabel, srat, sconf, sstatus)
        if not ok:
            supabase_client.upsert_decision_snapshot(did, row, sdesc, slabel, srat, sconf, sstatus)

    if manual_held:
        summary["manual_held_fields"] = [
            {"decision_id": d, "field": f, "db": str(v or ""), "sheet": str(s or "")}
            for (d, f, v, s) in manual_held[:20]
        ]
        logger.warning(
            f"[decision-reconcile] held {len(manual_held)} manually-set field(s) "
            f"against a DB-side change (Sheet value kept)"
        )

    try:
        supabase_client.log_action("decision_reconcile_applied", details=summary, triggered_by="auto")
    except Exception:
        pass
    logger.info(f"[decision-reconcile] applied: {summary}")
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
