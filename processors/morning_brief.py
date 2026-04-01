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
