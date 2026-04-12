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
        from guardrails.calendar_filter import is_cropsight_meeting
        events = await calendar_service.get_todays_events()
        cropsight_events = [e for e in events if is_cropsight_meeting(e) is not False]
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
        overdue_high = [
            t for t in all_tasks
            if t.get("deadline") and t["deadline"] < today_str_urgency
            and t.get("priority") == "H"
        ][:3]
        if overdue_high:
            sections.append({
                "type": "task_urgency",
                "title": "Task Urgency",
                "items": [
                    {"title": t.get("title", "")[:80], "assignee": t.get("assignee", ""), "deadline": t.get("deadline", "")}
                    for t in overdue_high
                ],
            })
    except Exception as e:
        logger.debug(f"Task urgency for morning brief failed: {e}")

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
                assignee = f" ({item['assignee']})" if item.get("assignee") else ""
                attention_items.append(f"  🟡 {item['title']}{assignee} — due {item.get('deadline', '?')}")

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
        from services.telegram_bot import telegram_bot

        formatted = format_morning_brief(brief)
        await telegram_bot.send_to_eyal(formatted, parse_mode="HTML")

        # Audit log for traceability (no approval needed, but keep record)
        brief_id = f"brief-{date.today().isoformat()}"
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
