"""
Morning brief processor.

Compiles all overnight items into ONE consolidated Telegram message:
1. Daily email scan results (personal Gmail)
2. Overnight constant layer extractions (team emails to Gianluigi)
3. Today's calendar preview
4. Overnight alerts (overdue tasks, stale commitments)

This is the key Phase 4 UX innovation — replaces approval bombardment
with a single daily touchpoint at 7:00 IST.
"""

import logging
from datetime import date, datetime

from config.settings import settings
from config.team import SENSITIVE_KEYWORDS
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Telegram-message length budget for the brief (matches the historical 3800 cap).
MORNING_BRIEF_BUDGET_CHARS = 3800


# =========================================================================
# Source Categorization
# =========================================================================

def _categorize_source(sender: str, subject: str) -> str:
    """
    Categorize email source for display: team/investor/client/legal/partner/other.

    Based on entity registry + sensitivity keywords.
    Never shows raw email/subject in the brief.
    """
    sender_lower = (sender or "").lower()
    subject_lower = (subject or "").lower()

    # Check sensitivity keywords for investor/legal
    for kw in SENSITIVE_KEYWORDS:
        if kw in subject_lower or kw in sender_lower:
            if kw in ("investor", "investment", "funding", "vc"):
                return "investor"
            if kw in ("lawyer", "legal", "fischer", "fbc", "zohar"):
                return "legal"

    # Check if sender is team
    from config.team import is_team_email
    if is_team_email(sender_lower):
        return "team"

    # Check entity registry for known orgs
    try:
        from services.supabase_client import supabase_client as sc
        entities = sc.list_entities(entity_type="organization", limit=100)
        for entity in entities:
            name = entity.get("canonical_name", "").lower()
            if name and (name in sender_lower or name in subject_lower):
                return "partner"
    except Exception:
        pass

    return "other"


# =========================================================================
# Topic surfacing helpers (v1 legacy + v2 knowledge-layer)
# =========================================================================

def _gather_topic_state_legacy() -> dict | None:
    """Legacy (v1) blocked/stale topic surfacing off state_json (hard cap 3)."""
    from datetime import date as _date, timedelta as _td

    state_rows = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, meeting_count, state_json")
        .not_.is_("state_json", "null")
        .limit(200)
        .execute()
    )
    today = _date.today()
    stale_cutoff = today - _td(days=14)
    blocked, stale = [], []
    for row in (state_rows.data or []):
        state = row.get("state_json") or {}
        status = state.get("current_status")
        if status == "blocked":
            blocked.append({
                "topic_name": row.get("topic_name", ""),
                "summary": state.get("summary", "")[:160],
                "kind": "blocked",
            })
            continue
        if (row.get("meeting_count") or 0) > 3:
            last = state.get("last_activity_date")
            if last:
                try:
                    last_d = _date.fromisoformat(str(last)[:10])
                    if last_d < stale_cutoff:
                        stale.append({
                            "topic_name": row.get("topic_name", ""),
                            "summary": state.get("summary", "")[:160],
                            "days_idle": (today - last_d).days,
                            "last_activity_date": str(last),
                            "kind": "stale",
                        })
                except (ValueError, TypeError):
                    pass
    stale.sort(key=lambda x: x["days_idle"], reverse=True)
    topic_items = (blocked + stale)[:3]
    if topic_items:
        return {"type": "topic_state", "title": "Topic state", "items": topic_items}
    return None


def _gather_knowledge_flags() -> list[dict]:
    """v2 foresight flags off the live topic briefs (brief_json).

    Reads the knowledge layer's authoritative current_status (blocked/stale),
    carrying each topic's source id (citation) and tier (sensitivity). The brief
    goes to Eyal (CEO tier) so nothing is filtered out here, but tier is
    preserved so these lines could be reused safely in a team-facing surface.
    Never raises; returns [] when no briefs exist yet (pre-synthesis).
    """
    rows = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, brief_json")
        .not_.is_("brief_json", "null")
        .limit(300)
        .execute()
        .data
        or []
    )
    flags: list[dict] = []
    for r in rows:
        brief = r.get("brief_json") or {}
        status = brief.get("current_status")
        if status not in ("blocked", "stale"):
            continue
        name = r.get("topic_name", "")
        narrative = (brief.get("narrative") or "").strip()
        if status == "blocked":
            risks = brief.get("risks") or []
            detail = (risks[0] if risks else narrative)[:140]
            flags.append({
                "topic_name": name, "kind": "blocked", "severity": "red",
                "detail": detail, "citation": r.get("id"),
                "sensitivity": brief.get("sensitivity"),
            })
        else:  # stale
            flags.append({
                "topic_name": name, "kind": "idle", "severity": "yellow",
                "detail": narrative[:140], "citation": r.get("id"),
                "sensitivity": brief.get("sensitivity"),
            })
    return flags


def _gather_loose_ends(knowledge_flags: list[dict] | None = None) -> str | None:
    """One aggregated 'loose ends' line from existing signals. None when clean.

    Blocked topics are surfaced individually (Needs attention), so they are NOT
    re-counted here — loose ends is the quiet aggregate of lower-signal items
    (overdue commitments, idle topics).
    """
    parts: list[str] = []
    try:
        from processors.deal_intelligence import generate_commitments_due
        n = len(generate_commitments_due(max_items=50) or [])
        if n:
            parts.append(f"{n} overdue commitment{'s' if n != 1 else ''}")
    except Exception:
        pass
    idle = sum(1 for f in (knowledge_flags or []) if f.get("kind") == "idle")
    if idle:
        parts.append(f"{idle} idle topic{'s' if idle != 1 else ''}")
    if not parts:
        return None
    return ", ".join(parts) + " — ask me to expand"


# =========================================================================
# Compilation
# =========================================================================

async def compile_morning_brief() -> dict:
    """
    Compile the daily morning brief from all sources.

    Returns:
        {
            "sections": [...],
            "stats": {...},
            "scan_ids": [...],  # email_scan IDs to mark approved on approval
        }
    """
    today_str = date.today().isoformat()
    # Get yesterday for scanning window
    from datetime import timedelta
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()

    sections = []
    scan_ids = []
    stats = {"email_scans": 0, "constant_items": 0, "calendar_events": 0, "alerts": 0}

    # v2 (PR2) rollout flags. During shadow both formats are produced (v1 is the
    # authoritative send, v2 a tagged preview), so we gather data for whichever
    # is rendered: v1 needs topic_state; v2 needs knowledge_flags + loose_ends.
    v2_enabled = settings.MORNING_BRIEF_V2_ENABLED
    v2_shadow = settings.MORNING_BRIEF_V2_SHADOW
    render_v1 = (not v2_enabled) or v2_shadow
    render_v2 = v2_enabled

    # 1. Daily email scan results (personal Gmail)
    daily_scans = supabase_client.get_unapproved_email_scans(
        scan_type="daily",
        date_from=yesterday_str,
    )
    if daily_scans:
        items = []
        for scan in daily_scans:
            scan_ids.append(scan.get("id"))
            extracted = scan.get("extracted_items") or []
            category = _categorize_source(
                scan.get("sender", ""),
                scan.get("subject", ""),
            )
            sensitive = category in ("investor", "legal")
            for item in extracted:
                item["_source_category"] = category
                item["_sensitive"] = sensitive or item.get("sensitive", False)
                items.append(item)
        if items:
            sections.append({
                "type": "email_scan",
                "title": "Email Intelligence (Personal Gmail)",
                "items": items,
            })
            stats["email_scans"] = len(items)

    # 2. Overnight constant layer extractions
    constant_scans = supabase_client.get_unapproved_email_scans(
        scan_type="constant",
        date_from=yesterday_str,
    )
    if constant_scans:
        items = []
        for scan in constant_scans:
            scan_ids.append(scan.get("id"))
            extracted = scan.get("extracted_items") or []
            category = _categorize_source(
                scan.get("sender", ""),
                scan.get("subject", ""),
            )
            for item in extracted:
                item["_source_category"] = category
                item["_sensitive"] = item.get("sensitive", False)
                items.append(item)
        if items:
            sections.append({
                "type": "constant_layer",
                "title": "Team Email Intelligence",
                "items": items,
            })
            stats["constant_items"] = len(items)

    # 3. Today's calendar preview
    try:
        from services.google_calendar import calendar_service
        from guardrails.calendar_filter import should_include_meeting
        events = await calendar_service.get_todays_events()
        cropsight_events = [e for e in events if should_include_meeting(e)]
        if cropsight_events:
            event_list = []
            for e in cropsight_events:
                title = e.get("title", "Untitled")
                start = e.get("start", "")
                # Format time if available
                if isinstance(start, str) and "T" in start:
                    try:
                        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                        time_str = dt.strftime("%H:%M")
                    except (ValueError, TypeError):
                        time_str = start
                else:
                    time_str = str(start)
                event_list.append({"title": title, "time": time_str})
            sections.append({
                "type": "calendar",
                "title": "Today's Calendar",
                "events": event_list,
            })
            stats["calendar_events"] = len(event_list)
        elif getattr(calendar_service, "last_fetch_failed", False) is True:
            # The empty list came from an API failure (e.g. idle-wake broken
            # pipe), NOT a genuinely empty calendar. Surface it via the alerts
            # channel (handled by both brief renderers) so Eyal knows the brief
            # couldn't check his meetings rather than reading the silent absence
            # as "nothing today". [audit P3-03]
            sections.append({
                "type": "alerts",
                "alerts": [{
                    "severity": "high",
                    "message": "Calendar unavailable — today's meetings could not be checked.",
                }],
            })
            stats["calendar_unavailable"] = True
    except Exception as e:
        logger.warning(f"Calendar fetch for morning brief failed: {e}")

    # 4. Overnight alerts
    try:
        from processors.proactive_alerts import run_all_detectors
        alerts = await run_all_detectors()
        if alerts:
            sections.append({
                "type": "alerts",
                "title": "Operational Alerts",
                "alerts": alerts[:10],
            })
            stats["alerts"] = len(alerts)
    except Exception as e:
        logger.debug(f"Alert detection for morning brief skipped: {e}")

    # 5. Pending prep outlines
    try:
        pending_preps = supabase_client.get_pending_prep_outlines()
        if pending_preps:
            prep_items = []
            for pp in pending_preps:
                content = pp.get("content", {})
                event = content.get("outline", {}).get("event", content.get("event", {}))
                ptitle = event.get("title", "Unknown meeting")
                pstart = event.get("start", "")
                time_str = ""
                if isinstance(pstart, str) and "T" in pstart:
                    try:
                        dt = datetime.fromisoformat(pstart.replace("Z", "+00:00"))
                        time_str = dt.strftime("%H:%M")
                    except (ValueError, TypeError):
                        time_str = pstart
                prep_items.append({"title": ptitle, "time": time_str})
            sections.append({
                "type": "pending_prep_outlines",
                "title": "Pending Prep Outlines",
                "items": prep_items,
            })
    except Exception as e:
        logger.debug(f"Pending prep outlines check for morning brief failed: {e}")

    # 6. Pending weekly review session (existing)
    try:
        review_session = supabase_client.get_active_weekly_review_session()
        if review_session:
            review_week = review_session.get("week_number", 0)
            review_status = review_session.get("status", "unknown")
            sections.append({
                "type": "weekly_review",
                "title": "Weekly Review",
                "week_number": review_week,
                "status": review_status,
            })
    except Exception as e:
        logger.debug(f"Weekly review check for morning brief failed: {e}")

    # 7. Calendar check for upcoming weekly review today (if no session exists)
    try:
        from services.google_calendar import calendar_service
        from schedulers.weekly_review_scheduler import weekly_review_scheduler
        events = await calendar_service.get_todays_events()
        for event in events:
            if weekly_review_scheduler._is_review_event(event.get("title", "")):
                if not supabase_client.get_active_weekly_review_session():
                    start = event.get("start", "")
                    time_str = ""
                    if isinstance(start, str) and "T" in start:
                        try:
                            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                            time_str = dt.strftime("%H:%M")
                        except (ValueError, TypeError):
                            pass
                    sections.append({
                        "type": "upcoming_review",
                        "title": "Weekly Review",
                        "time": time_str,
                    })
                break
    except Exception as e:
        logger.debug(f"Calendar review check failed: {e}")

    # 8. Sheets sync status — detect if Sheets edits are out of sync with DB
    try:
        from processors.sheets_sync import compute_sheets_diff, format_sync_summary
        sync_diff = await compute_sheets_diff()
        sync_summary = format_sync_summary(sync_diff)
        if sync_summary:
            sections.append({
                "type": "sheets_sync",
                "title": "Sheets Sync Status",
                "summary": sync_summary,
            })
    except Exception as e:
        logger.debug(f"Sheets sync check for morning brief failed: {e}")

    # 8b. Task-update proposals (v3 reconcile) — Gianluigi inferred a change to a
    # field you've manually set; surfaced for your call. Capped to stay tractable.
    try:
        from services.supabase_client import supabase_client as _sc
        _pending = _sc.get_pending_approvals_by_status("pending") or []
        _tprops = [r for r in _pending if r.get("content_type") == "task_update_proposal"]
        if _tprops:
            _shown = _tprops[:5]
            _lines = [
                f"{len(_tprops)} task update(s) need your call"
                + (" (showing 5)" if len(_tprops) > 5 else "") + ":"
            ]
            for r in _shown:
                c = r.get("content") or {}
                label = (c.get("title") or c.get("task_id") or "")[:45]
                _lines.append(
                    f"  • {c.get('field', '?')} → {c.get('proposed', '?')} "
                    f"on '{label}' (from {c.get('source', 'meeting')})"
                )
            _lines.append("  Review via get_task_proposals / approve_task_proposal")
            sections.append({
                "type": "task_proposals",
                "title": "Task Proposals",
                "summary": "\n".join(_lines),
            })
    except Exception as e:
        logger.debug(f"Task proposals check for morning brief failed: {e}")

    # 9. Operational continuity context (Phase 12 A1) — always include baseline
    try:
        from processors.meeting_continuity import (
            build_daily_continuity_context,
            format_daily_continuity_for_brief,
        )
        continuity = build_daily_continuity_context()
        if continuity:
            formatted_continuity = format_daily_continuity_for_brief(continuity)
            if formatted_continuity:
                sections.append({
                    "type": "continuity",
                    "title": "Operations Snapshot",
                    "summary": formatted_continuity,
                })
    except Exception as e:
        logger.debug(f"Continuity context for morning brief failed: {e}")

    # 10. Deal Pulse — overdue follow-ups + stale deals (Phase 4)
    try:
        from processors.deal_intelligence import generate_deal_pulse, generate_commitments_due

        deal_pulse = generate_deal_pulse(max_items=3)
        if deal_pulse:
            sections.append({
                "type": "deal_pulse",
                "title": "Deal Pulse",
                "items": deal_pulse,
            })

        commitments_due = generate_commitments_due(max_items=3)
        if commitments_due:
            sections.append({
                "type": "commitments_due",
                "title": "Commitments Due",
                "items": commitments_due,
            })
    except Exception as e:
        logger.debug(f"Deal pulse for morning brief failed: {e}")

    # 11. Task Urgency — high-priority overdue tasks (Phase 5)
    try:
        today_str_urgency = date.today().isoformat()
        all_tasks = supabase_client.get_tasks(status="pending", limit=100)
        all_tasks += supabase_client.get_tasks(status="in_progress", limit=100)
        items = _gather_task_urgency_items(all_tasks, today_str_urgency)
        if items:
            sections.append({
                "type": "task_urgency",
                "title": "Task Urgency",
                "items": items,
            })
    except Exception as e:
        logger.debug(f"Task urgency for morning brief failed: {e}")

    # 11b. Topic surfacing.
    #  - v1 path: legacy state_json blocked/stale block (hard cap 3).
    #  - v2 path: knowledge-layer foresight flags (off brief_json) + loose-ends line.
    if render_v1:
        try:
            ts = _gather_topic_state_legacy()
            if ts:
                sections.append(ts)
        except Exception as e:
            logger.debug(f"Topic state surfacing failed: {e}")

    if render_v2:
        kflags = []
        try:
            kflags = _gather_knowledge_flags()
            if kflags:
                sections.append({
                    "type": "knowledge_flags",
                    "title": "Knowledge",
                    "items": kflags,
                })
        except Exception as e:
            logger.debug(f"Knowledge flags for morning brief failed: {e}")
        try:
            loose = _gather_loose_ends(kflags)
            if loose:
                sections.append({
                    "type": "loose_ends",
                    "title": "Loose ends",
                    "summary": loose,
                })
        except Exception as e:
            logger.debug(f"Loose ends for morning brief failed: {e}")

    # 12. Gantt Milestones This Week + Drift Alerts (Phase 5)
    try:
        from processors.gantt_intelligence import compute_gantt_metrics, detect_gantt_drift

        metrics = await compute_gantt_metrics()
        milestones = metrics.get("milestone_risks", [])[:3]
        if milestones:
            sections.append({
                "type": "gantt_milestones",
                "title": "Gantt Milestones",
                "items": milestones,
            })

        drift = await detect_gantt_drift()
        if drift:
            sections.append({
                "type": "drift_alerts",
                "title": "Drift Alerts",
                "items": drift[:2],
            })
    except Exception as e:
        logger.debug(f"Gantt milestones/drift for morning brief failed: {e}")

    # 13. QA system health — inline summary from daily QA check (X1)
    try:
        from schedulers.qa_scheduler import qa_scheduler
        qa_report = qa_scheduler.last_report
        if qa_report:
            score = qa_report.get("score", "unknown")
            issue_count = len(qa_report.get("issues", []))
            if score != "healthy" or issue_count > 0:
                sections.append({
                    "type": "qa_health",
                    "title": "System Health",
                    "score": score,
                    "issue_count": issue_count,
                    "issues": qa_report.get("issues", [])[:3],
                })
    except Exception as e:
        logger.debug(f"QA health for morning brief failed: {e}")

    # 14. System state — ALWAYS included so silence is never ambiguous (T2.2)
    try:
        from datetime import datetime as _dt, timedelta as _td
        yesterday = (_dt.now() - _td(days=1)).isoformat()

        # Watcher heartbeat freshness
        watcher_status = "unknown"
        try:
            heartbeats = supabase_client.get_scheduler_heartbeats()
            watcher_hb = next(
                (hb for hb in heartbeats if hb.get("scheduler_name") == "transcript_watcher"),
                None,
            )
            if watcher_hb:
                last_beat = str(watcher_hb.get("last_heartbeat", ""))
                if last_beat >= yesterday:
                    watcher_status = watcher_hb.get("status", "ok")
                else:
                    watcher_status = "stale"
        except Exception:
            pass

        # Rejected meetings count (should be 0 after Tier 1)
        rejected_count = 0
        try:
            rejected_list = supabase_client.list_meetings(
                approval_status="rejected", limit=100
            )
            rejected_count = len(rejected_list)
        except Exception:
            pass

        # Errors in last 24h
        errors_24h = 0
        try:
            error_result = (
                supabase_client.client.table("audit_log")
                .select("id", count="exact")
                .in_("action", ["critical_error", "watcher_error", "reminder_scheduler_error"])
                .gte("created_at", yesterday)
                .execute()
            )
            errors_24h = error_result.count or 0
        except Exception:
            pass

        # Pending approvals queue depth
        pending_queue = 0
        try:
            pending_list = supabase_client.get_pending_approval_summary()
            pending_queue = len(pending_list) if pending_list else 0
        except Exception:
            pass

        sections.append({
            "type": "system_state",
            "title": "System State",
            "watcher_status": watcher_status,
            "rejected_count": rejected_count,
            "errors_24h": errors_24h,
            "pending_queue": pending_queue,
        })
    except Exception as e:
        logger.debug(f"System state for morning brief failed: {e}")

    return {
        "sections": sections,
        "stats": stats,
        "scan_ids": scan_ids,
    }


# =========================================================================
# Formatting
# =========================================================================

def _gather_task_urgency_items(all_tasks: list[dict], today_str: str) -> list[dict]:
    """Pick up to 3 tasks for the morning-brief urgency line.

    Flag ON (OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED): rank by urgency-then-
    priority and INCLUDE the urgency=H ASAP class (no deadline) that today's
    overdue filter silently drops — capturing time-pressure without a date.
    Item dicts carry 'urgency'+'area' (the flag-on render shape).

    Flag OFF: the legacy selection (overdue ∧ priority=H), byte-for-byte —
    item dicts carry no 'urgency' key so the renderer keeps the old line.
    """
    if settings.OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED:
        _rank = {"H": 0, "M": 1, "L": 2}
        candidates = [
            t for t in all_tasks
            if (t.get("deadline") and t["deadline"] < today_str)  # overdue
            or t.get("urgency") == "H"                            # ASAP / time-critical
            or t.get("priority") == "H"                           # important (legacy)
        ]
        candidates.sort(key=lambda t: (
            _rank.get(t.get("urgency") or "M", 1),
            _rank.get(t.get("priority") or "M", 1),
            t.get("deadline") or "9999-12-31",  # dated before undated within a tier
        ))
        return [
            {
                "title": t.get("title", "")[:80],
                "assignee": t.get("assignee", ""),
                "deadline": t.get("deadline", ""),
                "deadline_confidence": t.get("deadline_confidence", "NONE"),
                "urgency": t.get("urgency") or "M",
                # category = Gantt-area taxonomy (2026-06 realignment); the
                # render key stays 'area' (it IS the area chip).
                "area": t.get("category") or "General",
            }
            for t in candidates[:3]
        ]

    overdue_high = [
        t for t in all_tasks
        if t.get("deadline") and t["deadline"] < today_str
        and t.get("priority") == "H"
    ][:3]
    return [
        {
            "title": t.get("title", "")[:80],
            "assignee": t.get("assignee", ""),
            "deadline": t.get("deadline", ""),
            "deadline_confidence": t.get("deadline_confidence", "NONE"),
        }
        for t in overdue_high
    ]


def _task_urgency_line(item: dict, esc) -> tuple[int, str]:
    """Render a task-urgency item the operational way (PR6 flag-on shape only —
    keyed on the item carrying an 'urgency' field).

    Urgency-first: a 🔴 for urgency=H, and for an undated H task it says "ASAP"
    rather than inventing a date (the no-invented-dates guardrail). Appends an
    area chip when the task has a real area. `esc` escapes user text (identity
    for plain-text v1, _esc for HTML v2). Returns (rank, line) — rank 0 floats
    H tasks to the top of the attention list.
    """
    urg = (item.get("urgency") or "M").upper()
    assignee = f" ({esc(item['assignee'])})" if item.get("assignee") else ""
    deadline_str = item.get("deadline") or ""
    if deadline_str:
        # ~ prefix signals an INFERRED (LLM-guessed) date, not a commitment.
        if item.get("deadline_confidence") == "INFERRED":
            deadline_str = f"~{deadline_str}"
        when = f"due {esc(deadline_str)}"
    elif urg == "H":
        when = "ASAP"          # time-critical but no committed date — never faked
    else:
        when = "no date set"
    area = item.get("area") or "General"
    area_str = f" · {esc(area)}" if area and area not in ("non-area", "General") else ""
    icon = "🔴" if urg == "H" else "🟡"
    rank = 0 if urg == "H" else 1
    return rank, f"  {icon} {esc(item['title'])}{assignee} — {when}{area_str}"


def format_morning_brief(brief: dict) -> str:
    """
    Format brief for Telegram display (Option A: tightened scannable).

    Shows extracted intelligence with abstract source attribution.
    Source described by CATEGORY (team/investor/client/legal),
    NOT by sender address or subject line.

    Sections are grouped into: emails, today's schedule, needs attention,
    deals, milestones, system status. Empty sections are omitted entirely.
    """
    sections = brief.get("sections", [])
    if not sections:
        return ""

    # Collect data from sections into logical groups
    email_groups: list[tuple[str, bool, list[dict]]] = []  # (label, sensitive, items)
    calendar_events: list[dict] = []
    prep_outlines: list[dict] = []
    attention_items: list[str] = []  # pre-formatted lines with emoji
    deal_items: list[str] = []
    milestone_items: list[str] = []
    system_parts: list[str] = []
    weekly_review_line: str = ""

    category_labels = {
        "team": "Team emails",
        "investor": "Investor emails",
        "client": "Client emails",
        "legal": "Legal emails",
        "partner": "Partner emails",
        "other": "External emails",
    }

    for section in sections:
        section_type = section.get("type", "")

        if section_type in ("email_scan", "constant_layer"):
            items = section.get("items", [])
            if not items:
                continue
            by_category: dict[str, list[dict]] = {}
            for item in items:
                cat = item.get("_source_category", "other")
                by_category.setdefault(cat, []).append(item)
            for cat, cat_items in by_category.items():
                label = category_labels.get(cat, "Emails")
                sensitive = any(i.get("_sensitive") for i in cat_items)
                email_groups.append((label, sensitive, cat_items))

        elif section_type == "calendar":
            calendar_events = section.get("events", [])

        elif section_type == "pending_prep_outlines":
            prep_outlines = section.get("items", [])

        elif section_type == "alerts":
            for alert in section.get("alerts", [])[:5]:
                severity = alert.get("severity", "")
                msg = alert.get("message", alert.get("description", ""))[:100]
                if severity == "high":
                    attention_items.append(f"  🔴 {msg}")
                elif severity == "medium":
                    attention_items.append(f"  🟡 {msg}")
                # Drop low severity from attention — not actionable at 7am

        elif section_type == "task_urgency":
            for item in section.get("items", []):
                if "urgency" in item:  # PR6 flag-on shape — urgency-first render
                    _, line = _task_urgency_line(item, esc=lambda s: s)
                    attention_items.append(line)
                    continue
                assignee = f" ({item['assignee']})" if item.get("assignee") else ""
                deadline_str = item.get("deadline", "?")
                # v2.3: ~ prefix signals INFERRED deadline (LLM guess, not a
                # verbatim commitment). Keep the task visible but flag the
                # date's reliability.
                if item.get("deadline_confidence") == "INFERRED" and deadline_str != "?":
                    deadline_str = f"~{deadline_str}"
                attention_items.append(f"  🟡 {item['title']}{assignee} — due {deadline_str}")

        elif section_type == "topic_state":
            # v2.3 PR 4: surface blocked / stale topics (hard cap of 3 items
            # enforced at gathering time in morning_brief.py). Blocked uses
            # 🔴, stale uses 🟡 — same severity language as alerts.
            for item in section.get("items", []):
                name = item.get("topic_name", "")
                if item.get("kind") == "blocked":
                    summary = item.get("summary") or "blocked"
                    attention_items.append(f"  🔴 {name}: blocked — {summary}")
                else:  # stale
                    days = item.get("days_idle", "?")
                    attention_items.append(f"  🟡 {name}: no activity in {days}d")

        elif section_type == "drift_alerts":
            for item in section.get("items", []):
                attention_items.append(f"  🔴 {item.get('drift_description', '')}")

        elif section_type == "deal_pulse":
            for item in section.get("items", []):
                icon = "🔴" if item.get("type") == "overdue" else ""
                prefix = f"  {icon} " if icon else "  "
                deal_items.append(f"{prefix}{item['name']} ({item['organization']}): {item['detail']}")

        elif section_type == "commitments_due":
            for item in section.get("items", []):
                to_str = f" to {item['promised_to']}" if item.get("promised_to") else ""
                deal_items.append(f"  🔴 {item['commitment']}{to_str} ({item['days_overdue']}d overdue)")

        elif section_type == "gantt_milestones":
            for item in section.get("items", []):
                weeks = item.get("weeks_away", "?")
                milestone_items.append(f"  {item.get('milestone', '?')} ({item.get('section', '')}) — {weeks}w away")

        elif section_type == "system_state":
            watcher = section.get("watcher_status", "unknown")
            rejected = section.get("rejected_count", 0)
            errors = section.get("errors_24h", 0)
            queue = section.get("pending_queue", 0)
            all_clear = (
                rejected == 0 and errors == 0
                and watcher in ("ok", "healthy", "unknown")
            )
            if all_clear and queue == 0:
                system_parts.append("all clear")
            else:
                problems = []
                if watcher not in ("ok", "healthy", "unknown"):
                    problems.append(f"watcher {watcher}")
                if rejected:
                    problems.append(f"{rejected} rejected meetings with orphan data")
                if errors:
                    problems.append(f"{errors} errors in 24h")
                if queue:
                    problems.append(f"{queue} pending approvals")
                system_parts.append(", ".join(problems))

        elif section_type == "qa_health":
            score = section.get("score", "unknown")
            if score != "healthy":
                issues = section.get("issues", [])
                if issues:
                    system_parts.append(f"QA: {issues[0][:80]}")

        elif section_type == "weekly_review":
            status = section.get("status", "unknown")
            week_num = section.get("week_number", 0)
            status_label = {
                "preparing": "being prepared",
                "ready": "ready — use /review to start",
                "in_progress": "in progress",
                "confirming": "awaiting final confirmation",
            }.get(status, status)
            weekly_review_line = f"Weekly review W{week_num}: {status_label}"

        elif section_type == "upcoming_review":
            time_str = section.get("time", "")
            time_part = f" at {time_str}" if time_str else " today"
            weekly_review_line = f"Weekly review{time_part} — prep starts 3h before"

        # continuity, sheets_sync: intentionally omitted

    # --- Assemble output ---
    lines = ["<b>Good morning</b>\n"]

    # Emails
    for label, sensitive, cat_items in email_groups:
        sensitive_tag = " [SENSITIVE]" if sensitive else ""
        lines.append(f"<b>{label}</b>{sensitive_tag}")
        for item in cat_items[:10]:
            text = item.get("text", item.get("description", ""))[:120]
            lines.append(f"  • {text}")
        if len(cat_items) > 10:
            lines.append(f"  ...and {len(cat_items) - 10} more")
        lines.append("")

    # Today (calendar + prep outlines)
    today_items = []
    for event in calendar_events:
        today_items.append(f"  • {event.get('time', '')} — {event.get('title', '')}")
    for po in prep_outlines:
        time_str = f" at {po['time']}" if po.get("time") else ""
        today_items.append(f"  • {po.get('title', 'Unknown')}{time_str} (prep pending)")
    if today_items:
        lines.append("<b>Today</b>")
        lines.extend(today_items)
        lines.append("")

    # Needs attention (alerts + task urgency + drift)
    if attention_items:
        lines.append("<b>Needs attention</b>")
        lines.extend(attention_items)
        lines.append("")

    # Deals (deal pulse + commitments)
    if deal_items:
        lines.append("<b>Deals</b>")
        lines.extend(deal_items)
        lines.append("")

    # Milestones
    if milestone_items:
        lines.append("<b>Milestones</b>")
        lines.extend(milestone_items)
        lines.append("")

    # Weekly review (one line, only if active)
    if weekly_review_line:
        lines.append(weekly_review_line)
        lines.append("")

    # System status (one line, no bold header)
    if system_parts:
        lines.append(f"System: {'; '.join(system_parts)}")

    result = "\n".join(lines).rstrip()
    # Truncate for Telegram
    if len(result) > 3800:
        result = result[:3800] + "\n\n(...)"
    return result


# =========================================================================
# v2 Formatting (PR2) — decision-first, knowledge-aware, ranked-not-capped
# =========================================================================

from html import escape as _esc


def _strip_line(line: str) -> str:
    """Strip leading bullet/emoji decoration from a formatted line (for the lead input)."""
    return line.replace("🔴", "").replace("🟡", "").replace("•", "").strip()


def _assemble_v2_groups(sections: list[dict]) -> dict:
    """Pure, deterministic regrouping of brief sections for the v2 layout.

    Facts come only from the gathered sections — nothing is invented here.
    """
    category_labels = {
        "team": "Team emails", "investor": "Investor emails", "client": "Client emails",
        "legal": "Legal emails", "partner": "Partner emails", "other": "External emails",
    }
    groups: dict = {
        "today": [], "attention": [], "deals": [], "milestones": [], "emails": [],
        "loose_ends": None, "weekly_review_line": "", "system_line": "",
    }
    attention_ranked: list[tuple[int, str]] = []  # (severity_rank, line); red=0, yellow=1
    system_raw = None
    qa_issue = ""

    for section in sections:
        st = section.get("type", "")

        if st in ("email_scan", "constant_layer"):
            items = section.get("items", [])
            by_cat: dict[str, list[dict]] = {}
            for item in items:
                by_cat.setdefault(item.get("_source_category", "other"), []).append(item)
            for cat, cat_items in by_cat.items():
                label = category_labels.get(cat, "Emails")
                sensitive = any(i.get("_sensitive") for i in cat_items)
                lines = [f"  • {_esc(i.get('text', i.get('description', ''))[:120])}" for i in cat_items]
                groups["emails"].append((label, sensitive, lines))

        elif st == "calendar":
            for e in section.get("events", []):
                groups["today"].append(f"  • {_esc(e.get('time', ''))} — {_esc(e.get('title', ''))}")

        elif st == "pending_prep_outlines":
            for po in section.get("items", []):
                t = f" at {_esc(po['time'])}" if po.get("time") else ""
                groups["today"].append(f"  • {_esc(po.get('title', 'Unknown'))}{t} (prep pending)")

        elif st == "alerts":
            for alert in section.get("alerts", []):
                sev = alert.get("severity", "")
                msg = _esc(alert.get("message", alert.get("description", ""))[:100])
                if sev == "high":
                    attention_ranked.append((0, f"  🔴 {msg}"))
                elif sev == "medium":
                    attention_ranked.append((1, f"  🟡 {msg}"))

        elif st == "task_urgency":
            for item in section.get("items", []):
                if "urgency" in item:  # PR6 flag-on shape — urgency-first render
                    attention_ranked.append(_task_urgency_line(item, esc=_esc))
                    continue
                assignee = f" ({_esc(item['assignee'])})" if item.get("assignee") else ""
                deadline_str = item.get("deadline", "?")
                if item.get("deadline_confidence") == "INFERRED" and deadline_str != "?":
                    deadline_str = f"~{deadline_str}"
                attention_ranked.append(
                    (1, f"  🟡 {_esc(item['title'])}{assignee} — due {_esc(deadline_str)}")
                )

        elif st == "knowledge_flags":
            # Blocked topics surface individually (red). Idle topics are quietly
            # aggregated into the loose-ends line instead.
            for f in section.get("items", []):
                if f.get("kind") == "blocked":
                    detail = _esc(f.get("detail", "") or "blocked")
                    attention_ranked.append((0, f"  🔴 {_esc(f.get('topic_name', ''))}: blocked — {detail}"))

        elif st == "drift_alerts":
            for item in section.get("items", []):
                attention_ranked.append((0, f"  🔴 {_esc(item.get('drift_description', ''))}"))

        elif st == "deal_pulse":
            for item in section.get("items", []):
                icon = "🔴 " if item.get("type") == "overdue" else ""
                groups["deals"].append(
                    f"  {icon}{_esc(item['name'])} ({_esc(item['organization'])}): {_esc(item['detail'])}"
                )

        elif st == "commitments_due":
            for item in section.get("items", []):
                to_str = f" to {_esc(item['promised_to'])}" if item.get("promised_to") else ""
                groups["deals"].append(
                    f"  🔴 {_esc(item['commitment'])}{to_str} ({item['days_overdue']}d overdue)"
                )

        elif st == "gantt_milestones":
            for item in section.get("items", []):
                weeks = item.get("weeks_away", "?")
                groups["milestones"].append(
                    f"  {_esc(item.get('milestone', '?'))} ({_esc(item.get('section', ''))}) — {weeks}w away"
                )

        elif st == "loose_ends":
            groups["loose_ends"] = section.get("summary")

        elif st == "system_state":
            system_raw = section

        elif st == "qa_health":
            if section.get("score") != "healthy":
                issues = section.get("issues", [])
                if issues:
                    qa_issue = _esc(issues[0][:80])

        elif st == "weekly_review":
            status = section.get("status", "unknown")
            label = {
                "preparing": "being prepared", "ready": "ready — use /review to start",
                "in_progress": "in progress", "confirming": "awaiting final confirmation",
            }.get(status, status)
            groups["weekly_review_line"] = f"Weekly review W{section.get('week_number', 0)}: {label}"

        elif st == "upcoming_review":
            time_str = section.get("time", "")
            groups["weekly_review_line"] = (
                f"Weekly review{' at ' + _esc(time_str) if time_str else ' today'} — prep starts 3h before"
            )

    attention_ranked.sort(key=lambda x: x[0])  # red before yellow, stable otherwise
    groups["attention"] = [line for _, line in attention_ranked]
    groups["system_line"] = _v2_system_line(system_raw, qa_issue)
    return groups


def _v2_system_line(system_raw: dict | None, qa_issue: str) -> str:
    """Exception-only System line: empty string when everything is healthy."""
    problems: list[str] = []
    if system_raw:
        watcher = system_raw.get("watcher_status", "unknown")
        if watcher not in ("ok", "healthy", "unknown"):
            problems.append(f"watcher {watcher}")
        if system_raw.get("rejected_count", 0):
            problems.append(f"{system_raw['rejected_count']} rejected meetings with orphan data")
        if system_raw.get("errors_24h", 0) >= settings.BRIEF_ERROR_THRESHOLD:
            problems.append(f"{system_raw['errors_24h']} errors in 24h")
        if system_raw.get("pending_queue", 0) > 5:
            problems.append(f"{system_raw['pending_queue']} pending approvals")
    line = ", ".join(problems)
    if qa_issue:
        line = (line + "; " if line else "") + f"QA: {qa_issue}"
    fb = _headline_fallback_line()
    if fb:
        line = (line + "; " if line else "") + fb
    return line


def _headline_fallback_line() -> str:
    """Surface a warning if the Haiku headline has been falling back a lot (review #2)."""
    try:
        from datetime import datetime as _dt, timedelta as _td
        since = (_dt.now() - _td(days=7)).isoformat()
        rows = (
            supabase_client.client.table("audit_log")
            .select("details")
            .eq("action", "morning_brief_headline_status")
            .gte("created_at", since)
            .execute()
            .data
            or []
        )
        if len(rows) < 4:
            return ""
        fallbacks = sum(
            1 for r in rows if str((r.get("details") or {}).get("status", "")).startswith("fallback")
        )
        rate = fallbacks / len(rows)
        return f"brief headline fell back {int(rate * 100)}% of last 7d" if rate > 0.25 else ""
    except Exception:
        return ""


def _log_headline_status(status: str) -> None:
    """Record each headline-composition outcome for observability (never raises)."""
    try:
        supabase_client.log_action(
            action="morning_brief_headline_status",
            details={"status": status},
            triggered_by="scheduler",
        )
    except Exception:
        logger.debug("headline status log failed", exc_info=True)


def _deterministic_lead(groups: dict) -> str:
    """The always-available lead line (used directly, or as Haiku fallback)."""
    bits = []
    if groups["attention"]:
        n = len(groups["attention"])
        bits.append(f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} attention")
    if groups["today"]:
        bits.append(f"{len(groups['today'])} on your calendar")
    if groups["deals"]:
        n = len(groups["deals"])
        bits.append(f"{n} deal item{'s' if n != 1 else ''}")
    return "; ".join(bits) if bits else "Quiet morning — nothing pressing"


async def _compose_lead(groups: dict) -> str:
    """Thin Haiku headline: one line on what needs Eyal today. Deterministic fallback.

    Facts stay deterministic — the model only headlines the provided items; it
    never sees or alters numbers/dates. Wrapped in to_thread (call_llm is sync).
    """
    fallback = _deterministic_lead(groups)
    source_lines = (
        [_strip_line(x) for x in groups["attention"][:5]]
        + [_strip_line(x) for x in groups["today"][:3]]
        + [_strip_line(x) for x in groups["deals"][:3]]
    )
    if not source_lines:
        _log_headline_status("success:empty")
        return fallback
    try:
        import asyncio
        from core.llm import call_llm

        system = (
            "You write ONE short line (max 18 words) for the top of a CEO's morning "
            "brief: what needs his attention today. Use ONLY the provided items; do "
            "not invent anything. No greeting, no emoji, no trailing period."
        )
        prompt = "Today's items:\n" + "\n".join(f"- {s}" for s in source_lines)

        def _run() -> str:
            text, _usage = call_llm(
                prompt=prompt, model=settings.model_simple, max_tokens=60,
                system=system, call_site="morning_brief_headline",
            )
            return text

        lead = (await asyncio.to_thread(_run)).strip()
        if lead:
            _log_headline_status("success")
            return _esc(lead[:200])
        _log_headline_status("fallback:empty_output")
        return fallback
    except Exception as e:
        _log_headline_status(f"fallback:{type(e).__name__}")
        return fallback


# Per-section soft caps for the v2 render (overflow becomes a "+N more →" button).
_V2_SECTION_CAPS = {"today": 8, "attention": 6, "deals": 4, "milestones": 4, "email": 6}


def _render_v2(groups: dict, lead: str) -> tuple[str, list[dict]]:
    """Render the decision-first brief. Returns (text, overflow) where overflow
    lists {section, label, hidden} for the brief_more pull buttons (PR3)."""
    lines = [f"<b>{lead}</b>\n"]
    overflow: list[dict] = []

    def emit(header: str, items: list[str], cap: int, section_key: str, label: str):
        if not items:
            return
        lines.append(f"<b>{header}</b>")
        lines.extend(items[:cap])
        if len(items) > cap:
            hidden = len(items) - cap
            overflow.append({"section": section_key, "label": label, "hidden": hidden})
            lines.append(f"  +{hidden} more →")
        lines.append("")

    emit("Today", groups["today"], _V2_SECTION_CAPS["today"], "today", "Today")
    emit("Needs attention", groups["attention"], _V2_SECTION_CAPS["attention"], "attention", "attention")
    emit("Deals", groups["deals"], _V2_SECTION_CAPS["deals"], "deals", "deals")
    emit("Milestones", groups["milestones"], _V2_SECTION_CAPS["milestones"], "milestones", "milestones")
    for label, sensitive, item_lines in groups["emails"]:
        tag = " [SENSITIVE]" if sensitive else ""
        emit(f"{label}{tag}", item_lines, _V2_SECTION_CAPS["email"], f"email:{label}", label)

    if groups.get("loose_ends"):
        lines.append(f"Loose ends: {_esc(groups['loose_ends'])}")
        lines.append("")
    if groups.get("weekly_review_line"):
        lines.append(groups["weekly_review_line"])
        lines.append("")
    if groups.get("system_line"):
        lines.append(f"System: {groups['system_line']}")

    text = "\n".join(lines).rstrip()
    if len(text) > MORNING_BRIEF_BUDGET_CHARS:
        text = text[:MORNING_BRIEF_BUDGET_CHARS] + "\n\n(...)"
    return text, overflow


async def format_morning_brief_v2(brief: dict) -> tuple[str, list[dict]]:
    """v2 entry point: assemble groups, compose the lead, render. Returns (text, overflow)."""
    sections = brief.get("sections", [])
    if not sections:
        return "", []
    groups = _assemble_v2_groups(sections)
    lead = await _compose_lead(groups)
    return _render_v2(groups, lead)


def _build_brief_keyboard(brief_id: str, overflow: list[dict] | None):
    """👍/👎 feedback + per-section '+N more' pull buttons (PR3).

    Only the four ranked sections get pull buttons (their keys are colon-free,
    so the callback parses cleanly); email overflow stays text-only.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [[
        InlineKeyboardButton("👍", callback_data=f"brieffb:up:{brief_id}"),
        InlineKeyboardButton("👎", callback_data=f"brieffb:down:{brief_id}"),
    ]]
    for o in (overflow or []):
        section = o.get("section", "")
        if section not in ("today", "attention", "deals", "milestones"):
            continue
        rows.append([
            InlineKeyboardButton(
                f"+{o.get('hidden', 0)} more: {o.get('label', section)}",
                callback_data=f"brief_more:{section}:{brief_id}",
            )
        ])
    return InlineKeyboardMarkup(rows)


# =========================================================================
# Trigger
# =========================================================================

async def trigger_morning_brief() -> dict | None:
    """
    Called by morning scheduler at MORNING_BRIEF_HOUR IST.

    1. Run personal email scan (if enabled)
    2. Compile morning brief (includes scan results)
    3. If any items, send directly to Eyal via Telegram (no approval needed)
    4. If NO items, stay silent (no "Nothing new" message)

    Returns:
        Brief dict if sent, None if nothing to report.
    """
    # 1. Run personal email scan if enabled
    if settings.EMAIL_DAILY_SCAN_ENABLED:
        try:
            from schedulers.personal_email_scanner import personal_email_scanner
            scan_stats = await personal_email_scanner.run_daily_scan()
            logger.info(f"Personal email scan completed: {scan_stats}")
        except Exception as e:
            logger.error(f"Personal email scan failed: {e}")

    # 2. Compile morning brief
    brief = await compile_morning_brief()

    # 3. Check if there's anything to report
    sections = brief.get("sections", [])
    if not sections:
        logger.info("Morning brief: nothing to report")
        return None

    # 4. Send directly to Eyal (internal, no approval gate)
    try:
        from services.orchestrator.spine import comms_spine

        brief_id = f"brief-{date.today().isoformat()}"
        v2_enabled = settings.MORNING_BRIEF_V2_ENABLED
        v2_shadow = settings.MORNING_BRIEF_V2_SHADOW

        # Authoritative send: v2 once cut over (enabled & not shadow); else v1
        # (the default, and during the shadow window where v1 stays authoritative).
        overflow: list[dict] = []
        if v2_enabled and not v2_shadow:
            brief_text, overflow = await format_morning_brief_v2(brief)
        else:
            brief_text = format_morning_brief(brief)

        # PR3: feedback buttons (+ pull buttons) on the authoritative send.
        reply_markup = None
        if settings.BRIEF_FEEDBACK_ENABLED:
            try:
                brief_id = supabase_client.create_brief_feedback_row(
                    brief_id,
                    brief_date=date.today().isoformat(),
                    variant="primary",
                    section_count=len(sections),
                )
                reply_markup = _build_brief_keyboard(brief_id, overflow)
            except Exception as e:
                logger.debug(f"Brief feedback row/keyboard failed: {e}")

        await comms_spine.send_to_eyal(
            brief_text, parse_mode="HTML", reply_markup=reply_markup
        )

        # v2 shadow preview — tagged, button-less; logged for comparison.
        if v2_enabled and v2_shadow:
            try:
                v2_text, _ = await format_morning_brief_v2(brief)
                if v2_text:
                    await comms_spine.send_to_eyal(
                        "<b>[v2 preview]</b>\n\n" + v2_text, parse_mode="HTML"
                    )
                    supabase_client.log_action(
                        action="morning_brief_v2_shadow",
                        details={"brief_id": brief_id, "chars": len(v2_text)},
                        triggered_by="scheduler",
                    )
                    if settings.BRIEF_FEEDBACK_ENABLED:
                        supabase_client.create_brief_feedback_row(
                            f"{brief_id}-preview",
                            brief_date=date.today().isoformat(),
                            variant="preview",
                            section_count=len(sections),
                        )
            except Exception as e:
                logger.debug(f"v2 shadow preview failed: {e}")

        # Audit log for traceability (no approval needed, but keep record)
        supabase_client.log_action(
            action="morning_brief_sent",
            details={
                "brief_id": brief_id,
                "stats": brief.get("stats", {}),
                "scan_ids": brief.get("scan_ids", []),
                "section_count": len(sections),
            },
            triggered_by="scheduler",
        )

        # Mark email scans as processed
        scan_ids = brief.get("scan_ids", [])
        if scan_ids:
            for scan_id in scan_ids:
                try:
                    supabase_client.client.table("email_scans").update(
                        {"approved": True}
                    ).eq("id", scan_id).execute()
                except Exception:
                    pass

        logger.info(f"Morning brief sent directly: {brief.get('stats', {})}")
        return brief

    except Exception as e:
        logger.error(f"Failed to send morning brief: {e}")
        return None
