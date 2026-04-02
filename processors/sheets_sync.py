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

    db_tasks = supabase_client.get_tasks(status=None, limit=500)

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

    stop_words = {"the", "a", "an", "to", "for", "and", "or", "of", "in", "on", "is", "it", "we", "with", "from"}
    duplicates = []
    seen_pairs = set()

    for i, a in enumerate(open_tasks):
        title_a = (a.get("title") or "").lower()
        words_a = set(title_a.split()) - stop_words
        if len(words_a) < 3:
            continue

        for b in open_tasks[i + 1:]:
            title_b = (b.get("title") or "").lower()
            words_b = set(title_b.split()) - stop_words
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

    # Apply task modifications (Sheets wins)
    for item in diff["tasks"]["modified"]:
        db_id = item.get("db_id")
        if not db_id:
            continue
        update_data = {}
        for field, vals in item["changes"].items():
            update_data[field] = vals["to"]
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
        try:
            supabase_client.client.table("tasks").insert({
                "title": title,
                "assignee": st.get("assignee", ""),
                "status": st.get("status", "pending"),
                "priority": st.get("priority", "M"),
                "deadline": st.get("deadline") or None,
                "category": st.get("category", ""),
                "label": st.get("label", ""),
            }).execute()
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

    # Duplicate detection
    dupes = t.get("potential_duplicates", [])
    if dupes:
        parts.append(f"Potential duplicates: {len(dupes)} task pairs")

    if not parts:
        return ""

    return "  • " + "\n  • ".join(parts) + "\n  Reply /sync to review and apply"
