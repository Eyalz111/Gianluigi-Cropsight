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
from datetime import datetime, timedelta, timezone

from config.settings import settings
from core.dates import edit_is_newer_than_sync, parse_human_date
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Fields compared for tasks (ignore formatting, created dates, source)
TASK_COMPARE_FIELDS = ("status", "assignee", "deadline", "priority", "label", "category")

# Fields compared for decisions
DECISION_COMPARE_FIELDS = ("decision_status",)


def _same_label(value) -> str:
    """Two labels compare equal when they name the same PROJECT.

    A rename keeps the old name as an alias and backfills every reference, so
    the sheet and the database can legitimately hold different spellings of one
    thing. Falls back to the plain normalisation when the vocabulary cannot
    resolve it — an unknown label is still worth comparing as text.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from services.supabase_client import supabase_client
        return _normalize(supabase_client.resolve_label(raw) or raw)
    except Exception:                                        # noqa: BLE001
        return _normalize(raw)


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
    # See the note in the field loop: this tab is a mirror, not an input.
    read_only = getattr(settings, "TASKS_TAB_READ_ONLY", False)
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

    # Assignee shorthand ("Nechama") and its canonical form ("Nechama Tik") name
    # the SAME owner, but comparing raw strings made the reconcile see a phantom
    # divergence every cycle: sheet "Nechama" != db "Nechama Tik" tripped the
    # manual-override rail, so Nechama's tasks were reported held on every 30-min
    # tick forever. Canonicalize both sides before comparing so equivalent
    # spellings are equal. The roster is loaded LAZILY and only when two raw
    # values actually differ — so the common case (sheet==db) never queries, and
    # the whole 145-task pass costs at most one roster read. [2026-07-25 audit]
    _roster_box: dict = {"loaded": False, "roster": None}
    _assignee_canon: dict[str, str] = {}

    def _canon_assignee(v) -> str:
        key = v.strip() if isinstance(v, str) else (str(v).strip() if v else "")
        if key not in _assignee_canon:
            if not _roster_box["loaded"]:
                try:
                    _roster_box["roster"] = supabase_client.list_team_members()
                except Exception as _re:
                    logger.warning(f"[reconcile] roster load failed (assignee compare falls back to raw): {_re}")
                    _roster_box["roster"] = None
                _roster_box["loaded"] = True
            try:
                _assignee_canon[key] = supabase_client.resolve_assignee(key, roster=_roster_box["roster"])
            except Exception:
                _assignee_canon[key] = key
        return _assignee_canon[key]

    def _field_eq(field: str, a, b) -> bool:
        """Field-aware equality. Fast path: a plain normalized compare. Only when
        the assignee field's two raw values DIFFER do we canonicalize both and
        re-compare, so a recognised shorthand ("Nechama") equals its full name
        ("Nechama Tik") without a phantom divergence."""
        if _normalize(a) == _normalize(b):
            return True
        if field == "assignee":
            return _normalize(_canon_assignee(a)) == _normalize(_canon_assignee(b))
        return False

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
    overrides: list[dict] = []         # {field, from, to} the system adjusted on pull

    def _record_override(field: str, typed):
        """If canonicalization changes a pulled value, note it so the human can
        be told 'roye → Roye Tadmor' rather than have it silently corrected."""
        typed = (typed or "").strip() if isinstance(typed, str) else typed
        if not typed:
            return
        canon = None
        if field == "assignee":
            canon = supabase_client.resolve_assignee(typed)
        elif field == "status":
            canon = supabase_client.resolve_status(typed)
        elif field == "label":
            canon = supabase_client.resolve_label(typed)
        if canon and canon != typed:
            overrides.append({"field": field, "from": typed, "to": canon})

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
            # READ-ONLY MIRROR. The Project Status sheet is the single editable
            # surface for tasks as of 2026-08-08; this tab stays as the flat,
            # sortable view but must never WRITE. Two writers on the same rows
            # is what produced every cross-surface defect this week — the
            # rename-revert loop, the manual_set_at recency bug, and three
            # labels left permanently divergent because this tab pulled its own
            # stale cell over a value Project Status had just written.
            #
            # Forcing every field down the Rule 4 path makes the tab a true
            # mirror: the DB is copied out, nothing is ever read back in, and
            # `manual_*` stops being set from here at all.
            if read_only:
                if not _field_eq(field, db_val, sheet_val):
                    _cell(_ACTION_SHEET_KEY[field], row, db_val)
                    summary["pushed"] += 1
                    if field == "deadline":
                        deadline_cell_written = True
                final[field] = db_val
                continue
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
            if not _field_eq(field, sheet_val, snap_val):
                upd[field] = sheet_val or None          # Eyal edited (Rule 1)
                manual_marks.append((sid, field))
                summary["pulled"] += 1
                final[field] = sheet_val
                _record_override(field, sheet_val)
            elif not _field_eq(field, db_val, sheet_val):
                if dt.get(f"manual_{field}") and not edit_is_newer_than_sync(
                        dt.get("manual_set_at"), snap.get("snapshot_at")):
                    # Rule 2 rail: never clobber a manually-set field. Until
                    # 2026-07-22 the manual_* flags were write-only (one reader in
                    # the whole codebase) and Rule 4 pushed straight over Eyal's
                    # sticky value. The authoritative HUMAN paths (Telegram, MCP)
                    # write the cell as well as the DB, so a DB-only divergence on
                    # a sticky field means a system/inference path wrote it — hold
                    # the human's cell and surface it instead of reverting.
                    #
                    # THAT PREMISE STOPPED BEING TRUE on 2026-08-07, when the
                    # Project Status sheet became a second HUMAN surface. It
                    # writes the database and marks the field sticky, but it does
                    # not write this tab's cell — so Nechama editing an owner
                    # there looks exactly like "a machine wrote it" from here, and
                    # was held forever. The two sheets would show different values
                    # for the same task indefinitely, with nobody told.
                    #
                    # Same tie-break as the Project Status engine: if the human
                    # edit recorded in manual_set_at came AFTER this tab was last
                    # in step, it is the newer decision and belongs in this cell
                    # too. Missing timestamps hold. [2026-08-08]
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
            if read_only:                       # mirror — never read text back in
                if _normalize(c_db) != _normalize(c_sheet):
                    _cell(col_key, row, c_db)
                    summary["pushed"] += 1
                final[db_key] = c_db
                continue
            if (str(c_sheet or "").strip()
                    and _normalize(c_sheet) != _normalize(c_snap)
                    and _normalize(c_sheet) != _normalize(c_db)):
                upd[db_key] = c_sheet                      # Eyal edited (Rule 1)
                manual_marks.append((sid, db_key))
                summary["pulled"] += 1
                final[db_key] = c_sheet
                _record_override(db_key, c_sheet)
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
    # A mirror does not accept new work. A row typed here would become a task
    # that the Project Status sheet then has to be told about, which is the
    # second-writer problem wearing a different hat. Reported so a row typed
    # out of habit is not silently ignored.
    if read_only and creates:
        logger.info(
            f"[reconcile] {len(creates)} hand-typed row(s) on the Tasks tab "
            "ignored — it is a read-only mirror; add work on Project Status.")
        summary["ignored_creates"] = len(creates)
        creates = []
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
                # Hand-typed into the Sheet by Eyal/Nechama -> approved on arrival,
                # matching the manual decision/meeting create paths. Without this
                # the row lands DB-default 'pending' and never surfaces to the bot.
                approval_status="approved",
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
    # Canonicalization adjustments the human should see, not have silently
    # applied (the SatYield-revert class). Deduped for the summary.
    if overrides:
        seen, uniq = set(), []
        for o in overrides:
            k = (o["field"], o["from"], o["to"])
            if k not in seen:
                seen.add(k); uniq.append(o)
        summary["overrides"] = uniq[:20]

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
# Merged the same way, but ONLY once the column exists. follow_up_meetings.
# priority arrives with migrate_meeting_priority_2026_08.sql; until then the DB
# row simply has no such key, and merging it would compare every sheet value
# against None and pull the lot in as human edits. Presence is checked per row
# rather than assumed, so the code can ship before the migration runs.
_MEETING_OPTIONAL_MAP = {"priority": "priority"}


# Five meetings vanishing is tidying up; fifty is a bad read.
_MAX_MEETING_DROPS = 5
_MEETING_TERMINAL = frozenset({"held", "dropped"})


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
      - Status regression is blocked only FROM A TERMINAL STATE: a stale cell
        can never un-hold or un-drop a meeting, because it already happened.
        Every other move, including parking something already queued to
        schedule, is a legitimate decision and is pulled.
    """
    from services.google_sheets import (
        sheets_service, MEETING_COLUMNS, MEETING_TAB_NAME,
        MEETING_STATUSES, MEETING_TERMINAL_STATUSES, _fmt_ddmmyyyy,
        _MEETING_PRIORITY_TO_SHEET,
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
               "canonicalized": 0, "shadow": shadow, "dry_run": dry_run}
    manual_held: list[tuple] = []
    cell_writes: list[dict] = []
    snapshot_writes: list[tuple] = []
    db_updates: dict[str, dict] = {}
    manual_marks: list[tuple] = []
    archive_moves: list[dict] = []     # aged-out dropped rows -> Past Meetings tab
    # HELD meetings stay on the tab as visible history (Eyal wants to see the
    # completed ones); DROPPED meetings stay too, then age out to Past Meetings
    # once untouched for the archival window — the "60-day dropped timer".
    # _TERMINAL is still used to keep DB-only terminal meetings from being
    # re-added to the sheet (no resurrection of already-archived history).
    # [2026-07-24]
    _TERMINAL = ("held", "dropped")
    _drop_days = int(getattr(settings, "TASK_ARCHIVAL_DAYS", 60) or 60)
    _drop_cutoff = datetime.now(timezone.utc) - timedelta(days=_drop_days)

    # HELD MEETINGS LEAVE AFTER TWO WEEKS — Eyal's number. Its own timer, not
    # the 60-day dropped one: a meeting that happened is history worth glancing
    # at for a fortnight, whereas an abandoned one is kept mainly so a deletion
    # can be noticed and undone. [2026-08-13]
    _held_days = int(getattr(settings, "MEETING_HELD_ARCHIVE_DAYS", 14) or 14)
    _held_cutoff = datetime.now(timezone.utc) - timedelta(days=_held_days)

    def _older_than(iso, cutoff) -> bool:
        if not iso:
            return False
        try:
            d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        return d < cutoff

    def _aged_out(iso) -> bool:
        return _older_than(iso, _drop_cutoff)

    def _held_aged_out(dm: dict) -> bool:
        """Two weeks since the meeting became HELD — measured from `held_at`.

        Never from `updated_at`. That moves on every edit, and the reconcile
        edits rows: both held meetings on the live tab carry the timestamp of
        last night's sync rather than of the day they happened. Keyed on it,
        renaming a held meeting would silently restart its fortnight and a
        tab-wide write would restart everyone's at once.

        `held_at` is stamped by a database trigger on the transition into held
        (scripts/migrate_meeting_held_at.sql) and is not touched while the
        meeting stays held.

        A MISSING held_at MEANS "DO NOT ARCHIVE". Before the migration runs every
        held row reads None, and `_older_than` returns False for it — the safe
        reading of "nobody knows when this happened" is to leave it where it is.
        The inverse, treating an absent timestamp as infinitely old, would sweep
        every held meeting off the tab the first time this ran against an
        unmigrated database.
        """
        return _older_than(dm.get("held_at"), _held_cutoff)

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

        merged = dict(_MEETING_CONTENT_MAP)
        merged.update({f: k for f, k in _MEETING_OPTIONAL_MAP.items()
                       if f in dm})
        # AN UNTOUCHED DEFAULT IS NOT A DECISION. `ADD COLUMN priority DEFAULT
        # 'M'` backfilled all 122 meetings, so every blank Priority cell
        # differed from the database and the merge stamped "M" across a column
        # nobody had filled in — telling Eyal the whole pool had been triaged
        # when none of it had, and burying the handful he actually marked.
        # _meeting_row already refuses to RENDER an unset priority for exactly
        # this reason; the push path needed to agree. Once a human sets one,
        # manual_priority makes it real and it renders normally.
        # [2026-08-09 code review]
        if (not str(sm.get("priority") or "").strip()
                and str(dm.get("priority") or "").strip().upper() == "M"
                and not dm.get("manual_priority")):
            merged.pop("priority", None)
        for field, sheet_key in merged.items():
            s_val = sm.get(sheet_key)
            snap_val = snap.get(field)
            d_val = dm.get(field)
            if field == "participants":
                # Stored as TEXT[] in the DB, rendered comma-separated in the cell.
                d_val = ", ".join(d_val) if isinstance(d_val, list) else (d_val or "")
            # COMPARE A LABEL BY THE PROJECT IT NAMES, NOT BY ITS TEXT.
            #
            # Renaming a project keeps the old name as an ALIAS and backfills
            # every reference, so after the 2026-08-13 rename the sheet said
            # "Business Plan" and the database said "Business Plan
            # updates/refinements Q3 2026" — the same project, spelled two ways.
            # The raw comparison saw a difference, `manual_label` held the sheet
            # value under Rule 2, and the cell would have stayed divergent
            # forever.
            #
            # Third instance of this shape today, after `not_scheduled` and
            # `timing_text`: a value with a canonical form, compared raw. The
            # canonicalisation is the same one `resolve_label` applies on the
            # way in, so this only teaches the merge what the writer already
            # knew. [2026-08-13]
            if field == "label":
                s_cmp, d_cmp, snap_cmp = (_same_label(s_val),
                                          _same_label(d_val),
                                          _same_label(snap_val))
            else:
                s_cmp, d_cmp, snap_cmp = (_normalize(s_val), _normalize(d_val),
                                          _normalize(snap_val))

            if (str(s_val or "").strip()
                    and s_cmp != snap_cmp
                    and s_cmp != d_cmp):
                upd[field] = (_meeting_participants_to_list(s_val)
                              if field == "participants" else s_val)
                manual_marks.append((mid, field))
                summary["pulled"] += 1
                final[field] = s_val
            elif d_cmp != s_cmp:
                if dm.get(f"manual_{field}"):
                    summary["manual_held"] += 1
                    manual_held.append((mid, field, d_val, s_val))
                    final[field] = s_val
                else:
                    # RENDER IT THE WAY THE SHEET SPELLS IT. The cell stores
                    # 'Urgent' and the column stores 'U'; pushing the raw letter
                    # put a value outside the dropdown into the cell, so the
                    # most important meeting on the tab was the only one with a
                    # red invalid-entry triangle and no colour (the TEXT_EQ
                    # rules match 'Urgent', never 'U'). Reading it back maps
                    # u -> U, so it was stable and never self-corrected.
                    # [2026-08-09 code review]
                    _cell(sheet_key, row,
                          _MEETING_PRIORITY_TO_SHEET.get(
                              str(d_val or "").strip().upper(), d_val)
                          if field == "priority" else d_val)
                    summary["pushed"] += 1
                    final[field] = d_val
            else:
                final[field] = s_val
                # AND SETTLE THE SPELLING. The two sides now AGREE that
                # "Business Plan" and the renamed project are one thing — which
                # is exactly what stops anything from rewriting the cell, so the
                # old name would sit there forever. Same in-cycle
                # canonicalisation the status column gets, and the same reason:
                # a tolerant reader needs a writer that settles on one spelling.
                if field == "label" and str(d_val or "").strip():
                    if str(s_val or "").strip() != str(d_val).strip():
                        _cell(sheet_key, row, d_val)
                        summary["canonicalized"] += 1

        # --- proposed date (unparseable cells are never pulled) ---
        raw_date = sm.get("proposed_date_raw")
        s_date = sm.get("proposed_date")

        # KEEP THE WORDS EVEN WHEN THEY ARE NOT A DATE. Eyal writes "Once a
        # week" or "end of August" here — telling Nechama roughly when he wants
        # it, not booking a slot. Ignoring the cell (below) stops it corrupting
        # a timestamptz, but it also meant his phrasing never left the sheet, so
        # nothing else in the system could see it. The text now lands in
        # timing_text and the parsed reading beside it. [2026-08-12]
        if raw_date and str(raw_date).strip():
            from processors.meeting_timing import parse_timing

            timing = parse_timing(raw_date)
            # COMPARE THE WHOLE READING, NOT JUST THE TEXT.
            #
            # This was keyed on `timing_text` alone, and the text is the one part
            # that never changes — it is the cell, verbatim. So when the PARSER
            # learned a form it had not understood before, every meeting already
            # carrying that text was skipped and its new window was never
            # written: three rows reading `23-29/8/2026` and `16-22/8/2026` kept
            # a null window across the deploy that taught the parser to read
            # them. Found in production the same evening. [2026-08-13]
            #
            # Same shape as the `not_scheduled` cell earlier today: a guard keyed
            # on a field that already agrees suppresses the update of the fields
            # that do not. Compare everything the parse produces.
            _stored = (str(dm.get("timing_text") or ""),
                       dm.get("recurrence") or None,
                       str(dm.get("window_start") or "")[:10] or None,
                       str(dm.get("window_end") or "")[:10] or None)
            _parsed = (timing["text"], timing["recurrence"],
                       timing["window_start"], timing["window_end"])
            if _stored != _parsed:
                upd["timing_text"] = timing["text"]
                upd["recurrence"] = timing["recurrence"]
                upd["window_start"] = timing["window_start"]
                upd["window_end"] = timing["window_end"]

        if raw_date and parse_human_date(raw_date) is None:
            logger.warning(
                f"[meeting-reconcile] unparseable date {raw_date!r} (row {row}) — "
                "kept as timing_text, not pulled as a date"
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
                    # DD/MM/YYYY going out, ISO staying in the database —
                    # pushing the raw ISO put a differently-formatted date in a
                    # column where every other cell reads DD/MM/YYYY.
                    _cell("proposed_date", row, _fmt_ddmmyyyy(d_date))
                    summary["pushed"] += 1
                    final["proposed_date"] = d_date
            else:
                final["proposed_date"] = s_date

        # --- status: TERMINAL-ONLY GUARD. A meeting that has been held or
        #     dropped cannot be walked backwards by a stale cell — it already
        #     happened. Every other move is legitimate and is pulled.
        #
        #     This was a FULL monotonic ordering, which made every backward
        #     transition illegal. That is wrong for a working document: parking
        #     something already marked "to schedule" is a normal decision, and
        #     it was silently refused with the cell snapping back inside 30
        #     minutes — the sheet arguing with the person using it. The thing
        #     actually worth protecting is history, not order. [2026-08-09] ---
        # Canonicalised on the way in, so a cell still reading `not_scheduled`
        # — or a row written before the 2026-08-12 rename — resolves to
        # `to_schedule` instead of being rejected as unknown and silently
        # dropped back to the database value.
        from services.google_sheets import canonical_meeting_status

        raw_s_status = (sm.get("status") or "").strip().lower()
        s_status = canonical_meeting_status(raw_s_status)
        d_status = canonical_meeting_status(dm.get("status")) or "to_schedule"
        snap_status = canonical_meeting_status(snap.get("status"))
        if raw_s_status and not s_status:
            logger.warning(
                f"[meeting-reconcile] unknown status {raw_s_status!r} (row {row})")
        status_written = False
        if s_status and s_status != snap_status and s_status != d_status:
            if d_status not in MEETING_TERMINAL_STATUSES:
                upd["status"] = s_status
                manual_marks.append((mid, "status"))
                summary["pulled"] += 1
                final["status"] = s_status
            else:
                summary["status_guarded"] += 1
                _cell("status", row, d_status)
                status_written = True
                final["status"] = d_status
        elif d_status != s_status:
            _cell("status", row, d_status)
            status_written = True
            summary["pushed"] += 1
            final["status"] = d_status
        else:
            final["status"] = s_status

        # THE CELL'S OWN SPELLING IS NORMALISED TOO — and this is the only step
        # that can do it. Canonicalising on the way in makes the three surfaces
        # AGREE: with `not_scheduled` in the cell, in the database and in the
        # snapshot, all three read as `to_schedule`, every comparison above is
        # equal, so no divergence is found and no branch ever writes the cell.
        # The rename was invisible on the tab for exactly that reason — the
        # value the system stopped using stayed on screen, outside the dropdown
        # (red triangle) and matching no colour rule, until somebody retyped it.
        #
        # Compare the LITERAL cell text, not `raw_s_status`: that one is already
        # lower-cased, so a hand-typed `To_Schedule` would compare equal to the
        # canonical form and keep its invalid-entry triangle forever.
        #
        # This is the same in-cycle canonicalisation the other columns already
        # get on their push paths — `_fmt_ddmmyyyy` for dates, the Urgent/H/M/L
        # map for priority. Cosmetic only: `final` is unchanged, so nothing is
        # pulled, nothing is marked manual, and the snapshot still records the
        # canonical value it already recorded. [2026-08-13]
        literal_status = str(sm.get("status") or "").strip()
        if (not status_written and literal_status
                and final.get("status")
                and literal_status != final["status"]):
            _cell("status", row, final["status"])
            summary["canonicalized"] += 1

        if upd:
            db_updates[mid] = upd
        # Held stays on the tab as history; dropped stays until it ages out of
        # the archival window, then moves to 'Past Meetings'. Everything else
        # (scheduled / to_schedule / held / recent-dropped) snapshots + stays
        # on the working tab. [2026-07-24]
        # Only age-out a meeting that was ALREADY 'dropped' in the DB before this
        # sync: the drop edit is itself a "touch", so a just-dropped meeting gets
        # the full window, not immediate archival on the same reconcile. [review #14]
        # HELD sinks first and leaves second. MEETING_DISPLAY_ORDER already puts
        # held second-from-last, so the sinking needs no code; what was missing
        # was the leaving. Two weeks after it became held, the row moves to Past
        # Meetings — where nothing is lost: the move is append-then-delete,
        # idempotent by UUID, and the follow_up_meetings row keeps its status
        # untouched. [2026-08-13]
        _final_st = _normalize(final.get("status"))
        _db_st = _normalize(dm.get("status"))
        # Both halves of each pair, for the same reason the dropped branch has
        # always required them: the edit that marks a meeting held is itself a
        # touch, so a meeting held in THIS cycle must get its full window rather
        # than be archived out from under the person who just marked it.
        if (_final_st == "dropped" and _db_st == "dropped"
                and _aged_out(dm.get("updated_at"))):
            archive_moves.append({**sm, "status": final.get("status")})
            summary["archived"] = summary.get("archived", 0) + 1
        elif (_final_st == "held" and _db_st == "held"
                and _held_aged_out(dm)):
            archive_moves.append({**sm, "status": final.get("status")})
            summary["archived"] = summary.get("archived", 0) + 1
            summary["archived_held"] = summary.get("archived_held", 0) + 1
        else:
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
                    status=(sm.get("status") or "to_schedule").strip().lower(),
                    # Already the canonical DB letter — the read path maps the
                    # sheet's 'Urgent' to 'U'. Forwarding it at all is the fix:
                    # a priority typed on a NEW meeting row never reached the
                    # insert, so the default 'M' overwrote it within 30 minutes.
                    # [2026-08-11]
                    priority=sm.get("priority") or "",
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
                        sheet_id=sheets_service.meetings_workbook(),
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
                # Push the CANONICALIZED values back into the cells before
                # snapshotting. create_follow_up_meeting_manual canonicalizes
                # label + led_by ("moldova" -> "Moldova Pilot", "roye" ->
                # "Roye Tadmor"), but the cell still holds what was typed.
                # Snapshotting the canonical form against a raw cell makes the
                # NEXT reconcile see sheet != snap != db, fire Rule 1, and mark
                # the field manually-sticky forever — a fake human edit created
                # by our own write. Accepting shorthand is the advertised
                # behaviour, so this hits essentially every hand-added row.
                # [2026-07-23]
                canon_cells = {
                    "title": created.get("title"),
                    "label": created.get("label"),
                    "led_by": created.get("led_by"),
                    "participants": ", ".join(created.get("participants") or []),
                    "status": created.get("status"),
                }
                for _field, _canon in canon_cells.items():
                    if _normalize(_canon) != _normalize(sm.get(_field)):
                        _cell(_field, row, _canon)
                supabase_client.upsert_meeting_snapshot(
                    new_id, row, created.get("title"), created.get("label"),
                    created.get("led_by"), str(created.get("proposed_date") or "")[:10],
                    ", ".join(created.get("participants") or []), created.get("status"),
                )
            except Exception as e:
                logger.error(f"[meeting-reconcile] create failed for row {sm.get('row_number')}: {e}")

    # --- DB-only meetings -> re-add to the Sheet (never treated as deletes) ---
    # A TERMINAL MEETING LIVES ON PAST MEETINGS. Held and dropped ones used to
    # be re-added to the WORKING tab whenever they were absent, from the older
    # design where held stayed there as history. The 2026-08-09 redesign files
    # every terminal meeting into Past Meetings instead — so the two halves
    # disagreed, and the re-add path tried to drag ~46 archived meetings back
    # onto the working tab on EVERY cycle. The cap caught it every time
    # (`re-add of N rows exceeds cap` in the logs is precisely this), which also
    # meant the whole re-add path was permanently jammed: a genuinely missing
    # LIVE meeting could never be restored either.
    #
    # It makes a deliberate deletion permanent too — that sets `dropped`, and
    # the row used to come straight back as "a recent drop, not yet aged out".
    # [2026-08-09 code review, #10]
    archived_ids = await sheets_service.archived_meeting_ids()

    def _readd_ok(m: dict) -> bool:
        if not m.get("id") or m["id"] in sheet_by_id:
            return False
        status = (m.get("status") or "").strip().lower()
        if status in _MEETING_TERMINAL:
            # On Past Meetings already, or the archive could not be read at all
            # — either way, leave it alone. Only a terminal meeting that is on
            # NEITHER tab is genuinely invisible, and that one is still surfaced
            # so it can be seen and archived properly. [review #3's invariant]
            if archived_ids is None or m["id"] in archived_ids:
                return False
            # A HUMAN'S DECISION OUTRANKS THE ARCHIVE. Deleting a row sets
            # `dropped` and marks the status manual; if the archive write has
            # not caught up yet, surfacing it again would undo the deletion in
            # front of them.
            if m.get("manual_status"):
                return False
            # Old history. An aged-out drop is not something to drag back into
            # the working view just because the archive lost its row.
            if status == "dropped" and _aged_out(m.get("updated_at")):
                return False
        return True
    missing = [m for m in db_rows if _readd_ok(m)]

    # DELETING A ROW MEANS "DROP IT". A meeting that HAS a snapshot was on the
    # tab last cycle and is not on it now, so somebody removed it deliberately —
    # and it came straight back on the next re-add, which is the loop Eyal hit:
    # "all that i pressed park and than deleted are either not relevant or
    # duplications - so stick and align with those actions of mine".
    #
    # `dropped` rather than a hidden-suppression flag, because that is what the
    # gesture means here and the status already exists. Nothing is lost: dropped
    # is terminal, so the guard protects it from a stale cell afterwards, and
    # the row moves to Past Meetings where the history stays readable.
    #
    # No snapshot means it was never rendered, so its absence is not a deletion
    # — that one is re-added as before. And the whole thing is capped: five
    # meetings disappearing is tidying up, fifty is a symptom, and the
    # difference has to be visible BEFORE they go.
    deleted = [m for m in missing
               if m["id"] in snapshots
               and (m.get("status") or "").strip().lower() not in _MEETING_TERMINAL]
    if len(deleted) > _MAX_MEETING_DROPS:
        logger.error(
            f"[meeting-reconcile] {len(deleted)} row(s) look deleted — that is "
            f"more than the cap of {_MAX_MEETING_DROPS}. Dropping none of them; "
            "a whole tab going missing is a bad read, not a decision.")
        deleted = []
    dropped_ids = {m["id"] for m in deleted}
    missing = [m for m in missing if m["id"] not in dropped_ids]
    summary["deleted_to_dropped"] = len(deleted)
    if deleted and write_allowed:
        for m in deleted:
            try:
                supabase_client.update_follow_up_meeting(m["id"], status="dropped")
                supabase_client.mark_meeting_field_manual(
                    m["id"], "status", "sheet_delete")
                logger.info("[meeting-reconcile] dropped (row deleted): "
                            f"{(m.get('title') or '')[:60]}")
            except Exception as e:                          # noqa: BLE001
                logger.warning(
                    f"[meeting-reconcile] could not drop {m['id']}: {e}")
    _readd_cap = max(30, len(sheet_by_id))
    if len(missing) > _readd_cap:
        logger.error(
            f"[meeting-reconcile] re-add of {len(missing)} rows exceeds cap "
            f"{_readd_cap} — skipping (bad-read safety)."
        )
        missing = []
    if missing and write_allowed:
        await sheets_service.add_meetings_batch_to_sheet(missing)
        # Seed a snapshot per re-added row from the values we just wrote.
        # Without it the next cycle reads snap={} → every field compares unequal
        # to None → pulled as a phantom human edit + marked manual, freezing the
        # field against future DB→Sheet refresh. reconcile_tasks fixes exactly
        # this at its own re-add ([audit P1-04]); the meetings copy dropped it.
        # `proposed_date` is worse here than on tasks: its Rule 1 has no
        # "!= db" guard, so a missing snapshot freezes it on the very next
        # reconcile with no DB change at all. [2026-07-23]
        for m in missing:
            if m.get("id"):
                supabase_client.upsert_meeting_snapshot(
                    m["id"], None, m.get("title"), m.get("label"), m.get("led_by"),
                    str(m.get("proposed_date") or "")[:10],
                    ", ".join(m.get("participants") or []),
                    m.get("status") or "to_schedule",
                )
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
                spreadsheetId=sheets_service.meetings_workbook(),
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

    # Move held/dropped rows to Past Meetings LAST — the delete leg shifts row
    # numbers, so it must run after every cell write above (same ordering rule
    # as archive_task_rows).
    if archive_moves:
        try:
            await sheets_service.archive_meeting_rows(archive_moves)
        except Exception as e:
            logger.error(f"[meeting-reconcile] Past-Meetings move incomplete: {e}")

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


def _parse_aliases(text: str) -> list[str]:
    return [a.strip() for a in (text or "").split(",") if a.strip()]


async def reconcile_projects(dry_run: bool = False, shadow: bool | None = None) -> dict:
    """Reconcile the Projects tab against canonical_projects.

    The editable face of the vocabulary. Keyed on the id column (E):
      - a row whose NAME changed vs the DB -> rename_canonical_project, which
        backfills every `label` reference and keeps the old name as an alias.
        This is the whole point: a rename is a one-cell edit, not a script.
      - aliases / area / description changes -> a plain update.
      - a row with no id -> a new canonical project (create + write id back).
      - a DB project missing from the sheet -> re-added (never deleted from the
        vocabulary by a blank row; deletion is out of scope on purpose).

    Renames are consequential (they mutate labels across 5 tables), so this runs
    only when the sheet value is unambiguously a deliberate edit — id present,
    name non-blank, and different from the DB.
    """
    from services.google_sheets import sheets_service, PROJECTS_COLUMNS, PROJECTS_TAB_NAME

    if not getattr(settings, "PROJECTS_RECONCILE_ENABLED", False):
        return {"skipped": "PROJECTS_RECONCILE_ENABLED off"}
    if shadow is None:
        shadow = getattr(settings, "PROJECTS_RECONCILE_SHADOW_MODE", True)
    write_allowed = not (dry_run or shadow)

    try:
        sheet_rows = await sheets_service.get_all_projects()
    except Exception as e:
        logger.error(f"[project-reconcile] could not read Sheet: {e}")
        return {"error": str(e)}

    db = supabase_client.get_canonical_projects(status="active")
    db_by_id = {p["id"]: p for p in db if p.get("id")}
    proj_snapshots = supabase_client.get_project_snapshots()
    areas = supabase_client.get_areas()
    area_id_by_name = {(a.get("name") or "").strip().lower(): a["id"] for a in areas}

    summary = {"matched": 0, "renamed": 0, "updated": 0, "created": 0,
               "readded": 0, "shadow": shadow, "dry_run": dry_run, "renames": []}

    if not sheet_rows and db:
        # A populated vocabulary reading as an empty sheet = bad read. Abort.
        logger.error("[project-reconcile] sheet empty but DB has projects — aborting bad read.")
        return {"error": "sheet_read_empty"}

    seen_ids = set()
    for sr in sheet_rows:
        pid = sr.get("id")
        name = sr.get("name")
        if not pid:
            # New project typed into a blank row.
            if not name:
                continue
            summary["created"] += 1
            if write_allowed:
                area_id = area_id_by_name.get(sr.get("area", "").lower())
                created = supabase_client.add_canonical_project(
                    name=name, description=sr.get("description", ""),
                    aliases=_parse_aliases(sr.get("aliases", "")), area_id=area_id,
                )
                if created and created.get("id"):
                    # add_canonical_project is idempotent-by-name: a blank row that
                    # names an EXISTING project resolves to that project's id. Mark
                    # it seen so the missing-re-add pass below doesn't append a
                    # SECOND row for the same project. [review #11]
                    seen_ids.add(created["id"])
                    if sr.get("row_number"):
                        try:
                            await sheets_service._update_cell(
                                sheet_id=settings.TASK_TRACKER_SHEET_ID,
                                range_name=f"'{PROJECTS_TAB_NAME}'!{PROJECTS_COLUMNS['id']}{sr['row_number']}",
                                value=created["id"],
                            )
                        except Exception as we:
                            logger.warning(f"[project-reconcile] id writeback failed row {sr['row_number']}: {we}")
            continue

        seen_ids.add(pid)
        dbp = db_by_id.get(pid)
        if not dbp:
            continue  # sheet id the DB doesn't know — leave it
        summary["matched"] += 1

        # RENAME — the load-bearing case, and until 2026-08-07 a live
        # data-corrupting loop. It compared the sheet cell straight against the
        # DB, so it could not tell "a human edited the SHEET" from "the DB moved
        # on and this cell is stale". Every rename made anywhere else (MCP,
        # Telegram, a migration script) was REVERTED on the next 30-minute tick
        # — and rename_canonical_project backfills `label` across five tables, so
        # the revert propagated through all of them.
        #
        # Now a three-way merge like every other editable surface:
        #   sheet != snapshot AND sheet != db  -> a human renamed it here (Rule 1)
        #   sheet == snapshot AND db != sheet  -> the DB advanced (Rule 4): push
        #                                         the new name into the cell
        # NO SNAPSHOT means no evidence. A rename mutates five tables, so it is
        # never performed on incomplete evidence — the snapshot is seeded from
        # the current cell and the next pass decides. At worst one cycle late.
        snap_name = (proj_snapshots.get(pid) or {}).get("title")
        has_snap = pid in proj_snapshots
        sheet_edited = (bool(name) and has_snap
                        and _normalize(name) != _normalize(snap_name)
                        and _normalize(name) != _normalize(dbp.get("name")))
        db_advanced = (bool(dbp.get("name")) and has_snap and not sheet_edited
                       and _normalize(dbp.get("name")) != _normalize(name))

        renamed_this_pass = sheet_edited
        if sheet_edited:
            summary["renamed"] += 1
            summary["renames"].append({"from": dbp.get("name"), "to": name})
            if write_allowed:
                try:
                    supabase_client.rename_canonical_project(pid, name)
                except Exception as e:
                    logger.error(f"[project-reconcile] rename failed for {pid}: {e}")
        elif db_advanced:
            # The cell is stale. Refresh it instead of reverting the database.
            summary["name_refreshed"] = summary.get("name_refreshed", 0) + 1
            if write_allowed:
                try:
                    await sheets_service._update_cell(
                        sheet_id=settings.TASK_TRACKER_SHEET_ID,
                        range_name=(f"'{PROJECTS_TAB_NAME}'!"
                                    f"{PROJECTS_COLUMNS['name']}{sr['row_number']}"),
                        value=dbp.get("name"),
                    )
                except Exception as e:
                    logger.error(f"[project-reconcile] name refresh failed for {pid}: {e}")
        elif not has_snap and bool(name) and _normalize(name) != _normalize(dbp.get("name")):
            # Divergence with no merge base — report it, change nothing.
            summary["unbased_divergence"] = summary.get("unbased_divergence", 0) + 1
            logger.warning(
                f"[project-reconcile] {pid}: sheet {name!r} != db "
                f"{dbp.get('name')!r} but no snapshot exists — refusing to "
                "rename on incomplete evidence. Seeding the base; next pass decides.")

        # Snapshot the name LAST, and only for a row we did not just fail on.
        if write_allowed:
            # On the UNBASED path the settled value must be the DB name, not
            # the typed cell. Seeding from the cell made the next pass read
            # sheet == snapshot, classify it as db_advanced, and push the OLD
            # name back over the rename — discarding it rather than applying it
            # "one cycle late" as intended. Seeding from the DB leaves the cell
            # differing from both base and DB, which is exactly the Rule 1 shape
            # the next pass needs to perform the rename. [2026-08-08 code review]
            settled = name if sheet_edited else dbp.get("name")
            try:
                supabase_client.upsert_project_snapshot(
                    pid, settled, sr.get("row_number"))
            except Exception as e:
                logger.warning(f"[project-reconcile] snapshot failed for {pid}: {e}")

        # Aliases / area / description — plain updates.
        updates = {}
        sheet_aliases = sorted(set(_parse_aliases(sr.get("aliases", ""))))
        # rename_canonical_project just folded the OLD name into aliases; this
        # same-pass update compares against the STALE pre-rename aliases, so
        # without merging the old name back in it would OVERWRITE the alias the
        # rename added — breaking canonicalization of the old name. [review #5]
        if renamed_this_pass and dbp.get("name"):
            sheet_aliases = sorted(set(sheet_aliases) | {dbp.get("name")})
        db_aliases = sorted(set(dbp.get("aliases") or []))
        if sheet_aliases and sheet_aliases != db_aliases:
            updates["aliases"] = sheet_aliases
        area_id = area_id_by_name.get(sr.get("area", "").lower())
        if area_id and area_id != dbp.get("area_id"):
            updates["area_id"] = area_id
        desc = sr.get("description", "")
        if desc and desc != (dbp.get("description") or ""):
            updates["description"] = desc
        if updates:
            summary["updated"] += 1
            if write_allowed:
                try:
                    supabase_client.client.table("canonical_projects").update(
                        updates).eq("id", pid).execute()
                except Exception as e:
                    logger.error(f"[project-reconcile] update failed for {pid}: {e}")

    # DB projects missing from the sheet -> re-add (never delete the vocabulary).
    missing = [p for p in db if p.get("id") and p["id"] not in seen_ids]
    if missing and write_allowed:
        area_names = {a["id"]: a.get("name", "") for a in areas}
        await sheets_service.add_projects_batch(missing, area_names)
        summary["readded"] = len(missing)

    try:
        supabase_client.log_action("project_reconcile_applied", details=summary, triggered_by="auto")
    except Exception:
        pass
    logger.info(f"[project-reconcile] {'(preview) ' if not write_allowed else ''}{summary}")
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
            # Push the CANONICALIZED label back into the cell before
            # snapshotting. create_manual_decision canonicalizes the label, but
            # the cell still holds the shorthand that was typed — so the next
            # reconcile sees sheet != snap != db, fires Rule 1, and pulls the
            # RAW value back into the DB. update_decision does NOT canonicalize
            # (bare .update()), so that pull silently OVERWRITES 'Moldova Pilot'
            # with 'moldova' and marks the field sticky, undoing the very
            # canonicalization the vocabulary work exists for. [2026-07-23]
            if _normalize(created.get("label")) != _normalize(sd.get("label")):
                _cell("label", row_no, created.get("label"))
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
