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
    Format brief for Telegram display.

    Shows extracted intelligence with abstract source attribution.
    Source described by CATEGORY (team/investor/client/legal),
    NOT by sender address or subject line.
    """
    sections = brief.get("sections", [])
    if not sections:
        return ""

    lines = ["<b>Good morning — Daily Brief</b>\n"]

    for section in sections:
        section_type = section.get("type", "")
        title = section.get("title", "")

        if section_type in ("email_scan", "constant_layer"):
            items = section.get("items", [])
            if not items:
                continue

            # Group by source category
            by_category: dict[str, list[dict]] = {}
            for item in items:
                cat = item.get("_source_category", "other")
                by_category.setdefault(cat, []).append(item)

            category_labels = {
                "team": "team correspondence",
                "investor": "investor correspondence",
                "client": "client correspondence",
                "legal": "legal correspondence",
                "partner": "partner correspondence",
                "other": "external correspondence",
            }

            for cat, cat_items in by_category.items():
                label = category_labels.get(cat, "correspondence")
                sensitive = any(i.get("_sensitive") for i in cat_items)
                sensitive_tag = " [SENSITIVE]" if sensitive else ""

                lines.append(
                    f"<b>From {label} ({date.today().strftime('%b %d')}):</b>{sensitive_tag}"
                )
                for item in cat_items[:10]:
                    item_type = item.get("type", "info")
                    text = item.get("text", item.get("description", ""))[:120]
                    lines.append(f"  • [{item_type}] {text}")
                if len(cat_items) > 10:
                    lines.append(f"  ... and {len(cat_items) - 10} more items")
                lines.append("")

        elif section_type == "calendar":
            events = section.get("events", [])
            if events:
                lines.append(f"<b>{title}:</b>")
                for event in events:
                    lines.append(f"  • {event.get('time', '')} — {event.get('title', '')}")
                lines.append("")

        elif section_type == "alerts":
            alerts = section.get("alerts", [])
            if alerts:
                lines.append(f"<b>{title}:</b>")
                for alert in alerts[:5]:
                    severity = alert.get("severity", "")
                    msg = alert.get("message", alert.get("description", ""))[:100]
                    icon = "🔴" if severity == "high" else "🟡" if severity == "medium" else "🔵"
                    lines.append(f"  {icon} {msg}")
                lines.append("")

        elif section_type == "pending_prep_outlines":
            items = section.get("items", [])
            if items:
                lines.append(f"<b>{title} ({len(items)}):</b>")
                for item in items:
                    time_str = f" at {item['time']}" if item.get("time") else ""
                    lines.append(f"  • {item.get('title', 'Unknown')}{time_str}")
                lines.append("")

        elif section_type == "upcoming_review":
            time_str = section.get("time", "")
            time_part = f" at {time_str}" if time_str else " today"
            lines.append(f"<b>Weekly Review{time_part}:</b> prep starts 3h before")
            lines.append("")

        elif section_type == "weekly_review":
            week_num = section.get("week_number", 0)
            status = section.get("status", "unknown")
            status_label = {
                "preparing": "being prepared",
                "ready": "ready — use /review to start",
                "in_progress": "in progress",
                "confirming": "awaiting final confirmation",
            }.get(status, status)
            lines.append(f"<b>Weekly Review W{week_num}:</b> {status_label}")
            lines.append("")

        elif section_type == "continuity":
            summary = section.get("summary", "")
            if summary:
                lines.append(f"<b>{section.get('title', 'Operations Snapshot')}:</b>")
                lines.append(summary)
                lines.append("")

        elif section_type == "qa_health":
            score = section.get("score", "unknown")
            issue_count = section.get("issue_count", 0)
            score_label = {"healthy": "All systems OK", "warning": "Issues detected", "critical": "Action needed"}.get(score, score)
            lines.append(f"<b>System Health:</b> {score_label}")
            for issue in section.get("issues", [])[:3]:
                lines.append(f"  - {issue[:80]}")
            lines.append("")

        elif section_type == "system_state":
            # Always-included heartbeat section (T2.2)
            watcher = section.get("watcher_status", "unknown")
            rejected = section.get("rejected_count", 0)
            errors = section.get("errors_24h", 0)
            queue = section.get("pending_queue", 0)

            all_clear = (
                rejected == 0
                and errors == 0
                and watcher in ("ok", "healthy", "unknown")
            )
            if all_clear and queue == 0:
                lines.append("<b>System State:</b> all clear")
            else:
                lines.append("<b>System State:</b>")
                lines.append(f"  Watcher: {watcher}")
                if rejected:
                    lines.append(f"  Rejected meetings with orphan data: {rejected}")
                if errors:
                    lines.append(f"  Errors in 24h: {errors}")
                if queue:
                    lines.append(f"  Pending approvals: {queue}")
            lines.append("")

        elif section_type == "deal_pulse":
            items = section.get("items", [])
            if items:
                lines.append(f"<b>{title}:</b>")
                for item in items:
                    icon = "!" if item.get("type") == "overdue" else "~"
                    lines.append(f"  {icon} {item['name']} ({item['organization']}): {item['detail']}")
                lines.append("")

        elif section_type == "commitments_due":
            items = section.get("items", [])
            if items:
                lines.append(f"<b>{title}:</b>")
                for item in items:
                    to_str = f" to {item['promised_to']}" if item.get("promised_to") else ""
                    lines.append(f"  ! {item['commitment']}{to_str} ({item['days_overdue']}d overdue)")
                lines.append("")

        elif section_type == "task_urgency":
            items = section.get("items", [])
            if items:
                lines.append(f"<b>{title}:</b>")
                for item in items:
                    assignee = f" ({item['assignee']})" if item.get("assignee") else ""
                    lines.append(f"  ! {item['title']}{assignee} — due {item.get('deadline', '?')}")
                lines.append("")

        elif section_type == "gantt_milestones":
            items = section.get("items", [])
            if items:
                lines.append(f"<b>{title}:</b>")
                for item in items:
                    weeks = item.get("weeks_away", "?")
                    lines.append(f"  - {item.get('milestone', '?')} ({item.get('section', '')}) — {weeks}w away")
                lines.append("")

        elif section_type == "drift_alerts":
            items = section.get("items", [])
            if items:
                lines.append(f"<b>{title}:</b>")
                for item in items:
                    lines.append(f"  ! {item.get('drift_description', '')}")
                lines.append("")

        elif section_type == "sheets_sync":
            summary = section.get("summary", "")
            if summary:
                lines.append(f"<b>{section.get('title', 'Sheets Sync')}:</b>")
                lines.append(summary)
                lines.append("")

    # Stats footer
    stats = brief.get("stats", {})
    total_items = stats.get("email_scans", 0) + stats.get("constant_items", 0)
    if total_items:
        lines.append(f"<i>{total_items} email items • {stats.get('calendar_events', 0)} meetings today</i>")

    result = "\n".join(lines)
    # Truncate for Telegram
    if len(result) > 4000:
        result = result[:4000] + "\n\n... (truncated)"
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
