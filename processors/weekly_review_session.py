"""
Interactive weekly review session processor.

3-part Telegram conversation following the debrief pattern:
- Part 1: "Here's your week" (stats + alerts + horizon)
- Part 2: "Decisions needed" (Gantt proposals + next week)
- Part 3: "Outputs" (generate + correct + approve)

Data model keeps all 5 sections (MCP-ready for Phase 7).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings
from core.llm import call_llm
from core.weekly_review_prompt import (
    get_weekly_review_system_prompt,
    get_part_prompt,
    get_correction_prompt,
)
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


# =========================================================================
# Session Lifecycle
# =========================================================================

async def start_weekly_review(
    user_id: str,
    trigger: str = "manual",
    calendar_event_id: str | None = None,
    force_fresh: bool = False,
) -> dict:
    """
    Start a new weekly review session or resume an existing one.

    Resume same-week, cancel stale. force_fresh cancels existing and recompiles.

    Args:
        user_id: User identifier.
        trigger: "manual" or "calendar".
        calendar_event_id: Calendar event ID if triggered by scheduler.

    Returns:
        Dict with response, session_id, action.
    """
    now = datetime.now()
    week_number = now.isocalendar()[1]
    year = now.isocalendar()[0]

    # Check for existing active session
    existing = supabase_client.get_active_weekly_review_session()
    if existing and force_fresh:
        # Cancel existing and start fresh with recompiled data
        supabase_client.update_weekly_review_session(
            existing["id"], status="cancelled"
        )
        logger.info(f"Force-fresh: cancelled existing session {existing['id']}")
        existing = None

    if existing:
        created_at = existing.get("created_at", "")
        expired = False
        age_hours = 0.0

        if created_at:
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
                if age_hours > settings.WEEKLY_REVIEW_SESSION_EXPIRY_HOURS:
                    expired = True
            except (ValueError, TypeError):
                pass

        if not expired:
            # Resume session (within expiry window)
            part = existing.get("current_part", 0)
            response = (
                f"Resuming your weekly review (W{week_number}). "
                f"You're on Part {part if part > 0 else 1}."
            )

            # Stale data warning with delta counts
            if age_hours > 4:
                delta_info = ""
                try:
                    new_tasks = supabase_client.count_items_since("tasks", created_at)
                    new_decisions = supabase_client.count_items_since("decisions", created_at)
                    parts = []
                    if new_tasks:
                        parts.append(f"{new_tasks} new task(s)")
                    if new_decisions:
                        parts.append(f"{new_decisions} new decision(s)")
                    if parts:
                        delta_info = f" ({', '.join(parts)} since compilation)"
                except Exception:
                    pass
                stale_note = (
                    f"\n\nNote: review data was compiled {age_hours:.0f}h ago{delta_info}. "
                    "Use /review --fresh to recompile."
                )
                response += stale_note

            if part == 0:
                # First time — show Part 1
                part1_response = await advance_to_part(existing["id"], 1)
                response = part1_response.get("response", response)
                return {
                    "response": response,
                    "session_id": existing["id"],
                    "current_part": 1,
                    "action": "review_resumed",
                }
            return {
                "response": response,
                "session_id": existing["id"],
                "current_part": part,
                "action": "review_resumed",
            }
        else:
            # Expired session — cancel and start fresh
            supabase_client.update_weekly_review_session(
                existing["id"], status="expired"
            )
            logger.info(
                f"Expired review session {existing['id']} ({age_hours:.0f}h old)"
            )

    # Compile data if not pre-compiled (manual /review)
    agenda_data = {}
    if trigger == "manual":
        try:
            from processors.weekly_review import compile_weekly_review_data
            agenda_data = await compile_weekly_review_data(week_number, year)
        except Exception as e:
            logger.error(f"Failed to compile review data: {e}")
            return {
                "response": "Sorry, I couldn't compile the weekly review data. Please try again.",
                "action": "error",
            }

    # Create new session
    session = supabase_client.create_weekly_review_session(
        week_number=week_number,
        year=year,
        status="in_progress",
        trigger_type=trigger,
        calendar_event_id=calendar_event_id or "",
        agenda_data=agenda_data,
    )
    session_id = session["id"]

    # Show Part 1
    part1_result = await advance_to_part(session_id, 1)
    response = part1_result.get("response", f"Starting weekly review for W{week_number}.")

    return {
        "response": response,
        "session_id": session_id,
        "current_part": 1,
        "action": "review_started",
    }


async def process_review_message(
    session_id: str,
    user_message: str,
    user_id: str,
) -> dict:
    """
    Route message within an active review session.

    Handles navigation, questions, Gantt proposals, corrections.

    Args:
        session_id: Weekly review session UUID.
        user_message: The user's message.
        user_id: User identifier.

    Returns:
        Dict with response, session_id, current_part, action.
    """
    session = supabase_client.get_weekly_review_session(session_id)
    if not session:
        return {
            "response": "Weekly review session not found. Start a new one with /review.",
            "action": "error",
        }

    # Check TTL
    created_at = session.get("created_at", "")
    if _is_session_expired(created_at):
        supabase_client.update_weekly_review_session(session_id, status="cancelled")
        return {
            "response": "This weekly review session has expired. Start a new one with /review.",
            "action": "session_expired",
        }

    current_part = session.get("current_part", 1)
    raw_messages = session.get("raw_messages", []) or []

    # Append user message
    raw_messages.append({
        "role": "user",
        "text": user_message,
        "ts": datetime.now().isoformat(),
    })

    # Check for navigation signals
    nav = _detect_navigation(user_message)
    if nav == "next":
        next_part = min(current_part + 1, 3)
        if next_part > current_part:
            supabase_client.update_weekly_review_session(
                session_id, raw_messages=raw_messages
            )
            return await advance_to_part(session_id, next_part)
    elif nav == "back":
        prev_part = max(current_part - 1, 1)
        if prev_part < current_part:
            supabase_client.update_weekly_review_session(
                session_id, raw_messages=raw_messages
            )
            return await advance_to_part(session_id, prev_part)
    elif nav == "end":
        supabase_client.update_weekly_review_session(
            session_id, status="cancelled", raw_messages=raw_messages
        )
        return {
            "response": "Weekly review ended.",
            "session_id": session_id,
            "current_part": current_part,
            "action": "review_ended",
        }

    # Regular message — process with LLM
    agenda_data = session.get("agenda_data", {}) or {}
    context = _build_review_context(agenda_data, current_part)

    prompt = f"{context}\n\nEyal's message:\n{user_message}"
    system_prompt = get_weekly_review_system_prompt()

    try:
        text, _usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,
            max_tokens=2048,
            system=system_prompt,
            call_site="weekly_review_message",
        )

        parsed = _parse_llm_json(text)
        response_text = parsed.get("response_text", text)
        action = parsed.get("action", "none")

        if action == "advance":
            supabase_client.update_weekly_review_session(
                session_id, raw_messages=raw_messages
            )
            return await advance_to_part(session_id, min(current_part + 1, 3))

        # Save messages
        supabase_client.update_weekly_review_session(
            session_id, raw_messages=raw_messages
        )

        return {
            "response": response_text,
            "session_id": session_id,
            "current_part": current_part,
            "action": "review_message",
        }

    except Exception as e:
        logger.error(f"Review message processing failed: {e}")
        supabase_client.update_weekly_review_session(
            session_id, raw_messages=raw_messages
        )
        return {
            "response": "I had trouble processing that. Could you rephrase?",
            "session_id": session_id,
            "current_part": current_part,
            "action": "review_message",
        }


async def advance_to_part(session_id: str, part_number: int) -> dict:
    """
    Format and present a specific part's data for Telegram.

    Args:
        session_id: Weekly review session UUID.
        part_number: Part to show (1, 2, or 3).

    Returns:
        Dict with response, session_id, current_part, action.
    """
    session = supabase_client.get_weekly_review_session(session_id)
    if not session:
        return {"response": "Session not found.", "action": "error"}

    agenda_data = session.get("agenda_data", {}) or {}
    week_number = session.get("week_number", 0)

    supabase_client.update_weekly_review_session(
        session_id, current_part=part_number
    )

    if part_number == 1:
        response = _format_part1(agenda_data, week_number)
    elif part_number == 2:
        response = _format_part2(agenda_data, week_number)
    elif part_number == 3:
        response = _format_part3(agenda_data, week_number)
    else:
        response = "Invalid part number."

    return {
        "response": response,
        "session_id": session_id,
        "current_part": part_number,
        "action": f"review_part{part_number}",
    }


async def finalize_review(session_id: str) -> dict:
    """
    Generate outputs (PPTX, HTML, digest).

    Stores in pending — does NOT upload to Drive yet (post-approval only).

    Returns:
        Dict with response, session_id, outputs, action.
    """
    session = supabase_client.get_weekly_review_session(session_id)
    if not session:
        return {"response": "Session not found.", "action": "error"}

    # Guard against double-finalize
    status = session.get("status", "")
    if status in ("confirming", "approved", "cancelled"):
        return {
            "response": "This review has already been finalized.",
            "action": "error",
        }

    agenda_data = session.get("agenda_data", {}) or {}
    week_number = session.get("week_number", 0)
    year = session.get("year", 0)

    outputs = {}

    # Generate HTML report
    try:
        from processors.weekly_report import generate_html_report
        report_result = await generate_html_report(
            session_id, agenda_data, week_number, year
        )
        outputs["html_report"] = report_result
    except Exception as e:
        logger.error(f"HTML report generation failed: {e}")
        outputs["html_report"] = {"error": str(e)}

    # Generate PPTX slide
    try:
        from processors.gantt_slide import generate_gantt_slide
        pptx_bytes = await generate_gantt_slide(week_number, year)
        outputs["pptx"] = {"generated": True, "size_bytes": len(pptx_bytes)}
        # Store bytes in session for later upload
        # (Can't store raw bytes in JSON — store flag, regenerate on confirm)
    except Exception as e:
        logger.error(f"PPTX generation failed: {e}")
        outputs["pptx"] = {"error": str(e)}

    # Transition to confirming
    supabase_client.update_weekly_review_session(
        session_id, status="confirming"
    )

    response_parts = ["<b>Weekly Review Outputs Ready</b>\n"]

    if outputs.get("html_report", {}).get("report_url"):
        response_parts.append(
            f"HTML Report: {outputs['html_report']['report_url']}"
        )

    if outputs.get("pptx", {}).get("generated"):
        response_parts.append("PPTX Gantt Slide: Ready")

    if any(o.get("error") for o in outputs.values()):
        errors = [f"- {k}: {v['error']}" for k, v in outputs.items() if v.get("error")]
        response_parts.append(f"\nSome outputs had issues:\n" + "\n".join(errors))

    response_parts.append("\nReview the outputs and approve when ready.")

    return {
        "response": "\n".join(response_parts),
        "session_id": session_id,
        "outputs": outputs,
        "action": "review_finalize",
    }


async def process_correction(
    session_id: str,
    correction_text: str,
    user_id: str,
) -> dict:
    """
    Parse correction and regenerate affected output.

    Safety cap: WEEKLY_REVIEW_MAX_CORRECTIONS.

    Returns:
        Dict with response, session_id, action.
    """
    session = supabase_client.get_weekly_review_session(session_id)
    if not session:
        return {"response": "Session not found.", "action": "error"}

    corrections = session.get("corrections", []) or []

    if len(corrections) >= settings.WEEKLY_REVIEW_MAX_CORRECTIONS:
        return {
            "response": (
                f"Maximum corrections ({settings.WEEKLY_REVIEW_MAX_CORRECTIONS}) "
                f"reached. Please approve or cancel."
            ),
            "session_id": session_id,
            "action": "max_corrections",
        }

    # Parse correction: try Haiku first (cheap, fast), fallback to Sonnet
    try:
        try:
            text, _usage = call_llm(
                prompt=correction_text,
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=get_correction_prompt(),
                call_site="weekly_review_correction_parse",
            )
            parsed = _parse_llm_json(text)
            new_corrections = parsed.get("corrections", [])
            response_text = parsed.get("response_text", "Correction noted.")
            if not new_corrections:
                raise ValueError("Haiku returned no corrections")
        except Exception:
            # Fallback to Sonnet for complex corrections
            text, _usage = call_llm(
                prompt=correction_text,
                model=settings.model_agent,
                max_tokens=1024,
                system=get_correction_prompt(),
                call_site="weekly_review_correction_fallback",
            )
            parsed = _parse_llm_json(text)
            new_corrections = parsed.get("corrections", [])
            response_text = parsed.get("response_text", "Correction noted.")

        corrections.extend(new_corrections)
        supabase_client.update_weekly_review_session(
            session_id, corrections=corrections
        )

        return {
            "response": response_text,
            "session_id": session_id,
            "action": "correction_applied",
        }

    except Exception as e:
        logger.error(f"Correction parsing failed: {e}")
        return {
            "response": "I couldn't parse that correction. Please try again.",
            "session_id": session_id,
            "action": "review_message",
        }


async def confirm_review(session_id: str, approved: bool, execute_gantt: bool = True) -> dict:
    """
    Confirm or cancel a finalized review.

    If approved: execute Gantt proposals FIRST (unless execute_gantt=False),
    then upload + distribute. Uses atomic claim to prevent double-approval.

    Args:
        session_id: Weekly review session ID.
        approved: True to approve, False to cancel.
        execute_gantt: If False, skip Gantt proposal execution (still distribute outputs).

    Returns:
        Dict with response, action, distribution details.
    """
    session = supabase_client.get_weekly_review_session(session_id)
    if not session:
        return {"response": "Session not found.", "action": "error"}

    # Guard against double-approve
    current_status = session.get("status", "")
    if current_status in ("approved", "cancelled"):
        return {
            "response": f"This review has already been {current_status}.",
            "action": f"review_{current_status}",
        }

    if not approved:
        supabase_client.update_weekly_review_session(session_id, status="cancelled")
        return {
            "response": "Weekly review cancelled.",
            "action": "review_cancelled",
        }

    # Atomic claim: update only if still confirming
    claim = (
        supabase_client.client.table("weekly_review_sessions")
        .update({"status": "approving"})
        .eq("id", session_id)
        .eq("status", "confirming")
        .execute()
    )
    if not claim.data:
        return {
            "response": "This review has already been approved.",
            "action": "review_approved",
        }

    agenda_data = session.get("agenda_data", {}) or {}
    week_number = session.get("week_number", 0)
    year = session.get("year", 0)
    gantt_proposals = session.get("gantt_proposals", []) or []

    distribution = {"gantt_executed": False, "drive_uploaded": False, "distributed": False}

    # 0. Backup Gantt BEFORE execution (pre-write snapshot)
    if execute_gantt and gantt_proposals and any(p.get("approved") for p in gantt_proposals):
        try:
            from services.gantt_manager import gantt_manager
            await gantt_manager.backup_full_gantt()
        except Exception as e:
            logger.warning(f"Pre-review Gantt backup failed: {e}")

    # 1. Execute approved Gantt proposals (skip if execute_gantt=False)
    gantt_failed = False
    if execute_gantt and gantt_proposals:
        try:
            from services.gantt_manager import gantt_manager
            for proposal in gantt_proposals:
                if proposal.get("approved"):
                    await gantt_manager.execute_approved_proposal(proposal["id"])
            distribution["gantt_executed"] = True
        except Exception as e:
            logger.error(f"Gantt execution failed during review confirm: {e}")
            gantt_failed = True

    if gantt_failed:
        # Revert status to confirming so Eyal can choose
        supabase_client.update_weekly_review_session(
            session_id, status="confirming"
        )
        return {
            "response": (
                "Gantt update failed. Distribute anyway or fix first?\n"
                "Use the buttons below to decide."
            ),
            "session_id": session_id,
            "action": "gantt_failed",
            "backup_available": True,
        }

    # 3-8. Upload + distribute via approval flow
    try:
        from guardrails.approval_flow import distribute_approved_review
        dist_result = await distribute_approved_review(
            session_id=session_id,
            agenda_data=agenda_data,
            week_number=week_number,
            year=year,
        )
        distribution.update(dist_result)
    except Exception as e:
        logger.error(f"Review distribution failed: {e}")

    # Update session status
    supabase_client.update_weekly_review_session(session_id, status="approved")

    # Build response with distribution details
    response_parts = ["Weekly review approved."]
    successes = []
    failures = []

    if distribution.get("gantt_executed"):
        successes.append("Gantt updated")
    if distribution.get("pptx_uploaded"):
        successes.append("PPTX uploaded to Drive")
    elif distribution.get("gantt_executed") is not None:
        failures.append("PPTX upload")
    if distribution.get("digest_uploaded"):
        successes.append("Digest uploaded to Drive")
    if distribution.get("email_sent"):
        successes.append(f"Email sent to {len(distribution.get('emails_to', []))} recipient(s)")
    elif distribution.get("pptx_uploaded") is not None:
        failures.append("Email")
    if distribution.get("telegram_sent"):
        successes.append("Telegram group notified")

    if successes:
        response_parts.append("\n<b>Distributed:</b> " + ", ".join(successes))
    if failures:
        response_parts.append("\n<b>Failed:</b> " + ", ".join(failures))
    if not successes and not failures:
        response_parts.append("\nNo distribution actions completed.")

    return {
        "response": "\n".join(response_parts),
        "session_id": session_id,
        "action": "review_approved",
        "distribution": distribution,
    }


async def resume_after_debrief(session_id: str) -> dict:
    """
    Resume weekly review after a debrief interruption.

    Refreshes agenda_data (debrief may have added tasks/decisions/proposals).

    Returns:
        Dict with response, session_id, current_part, action.
    """
    session = supabase_client.get_weekly_review_session(session_id)
    if not session:
        return {"response": "Session not found.", "action": "error"}

    week_number = session.get("week_number", 0)
    year = session.get("year", 0)
    current_part = session.get("current_part", 1)

    # Refresh data
    try:
        from processors.weekly_review import compile_weekly_review_data
        agenda_data = await compile_weekly_review_data(week_number, year)
        supabase_client.update_weekly_review_session(
            session_id, agenda_data=agenda_data
        )
    except Exception as e:
        logger.warning(f"Data refresh after debrief failed: {e}")

    result = await advance_to_part(session_id, current_part)
    result["action"] = "review_resumed_after_debrief"
    return result


# =========================================================================
# Formatting Helpers
# =========================================================================

def _format_part1(agenda_data: dict, week_number: int) -> str:
    """Format Part 1: Here's your week (stats + alerts + horizon)."""
    lines = [f"<b>Weekly Review W{week_number} — Part 1: Here's your week</b>\n"]

    # Week stats
    wir = agenda_data.get("week_in_review", {})
    meetings_count = wir.get("meetings_count", 0)
    decisions_count = wir.get("decisions_count", 0)
    task_summary = wir.get("task_summary", {})
    completed = len(task_summary.get("completed_this_week", []))
    overdue = len(task_summary.get("overdue", []))
    debrief_count = wir.get("debrief_count", 0)
    email_count = wir.get("email_scan_count", 0)

    lines.append("<b>Week Stats:</b>")
    lines.append(f"  • Meetings: {meetings_count}")
    lines.append(f"  • Decisions: {decisions_count}")
    lines.append(f"  • Tasks completed: {completed} | Overdue: {overdue}")
    if debrief_count:
        lines.append(f"  • Debriefs: {debrief_count}")
    if email_count:
        lines.append(f"  • Email scans: {email_count}")
    lines.append("")

    # Attention needed
    attention = agenda_data.get("attention_needed", {})
    stale_tasks = attention.get("stale_tasks", [])
    alerts = attention.get("alerts", [])

    if stale_tasks or alerts:
        lines.append("<b>Attention Needed:</b>")
        for task in stale_tasks[:5]:
            title = _escape_html(task.get("title", "")[:60])
            assignee = task.get("assignee", "")
            lines.append(f"  • [Stale] {title} ({assignee})")
        for alert in alerts[:5]:
            msg = _escape_html(alert.get("title", alert.get("message", ""))[:80])
            lines.append(f"  • {msg}")
        lines.append("")

    # Horizon check
    horizon = agenda_data.get("horizon_check", {})
    milestones = horizon.get("milestones", [])
    if milestones:
        lines.append("<b>Strategic Horizon:</b>")
        for ms in milestones[:5]:
            name = _escape_html(str(ms.get("name", ms) if isinstance(ms, dict) else ms)[:60])
            lines.append(f"  • {name}")
        lines.append("")

    result = "\n".join(lines)
    return result[:4000]  # Telegram limit


def _format_part2(agenda_data: dict, week_number: int) -> str:
    """Format Part 2: Next week + decisions (Gantt proposals + preview)."""
    lines = [f"<b>Weekly Review W{week_number} — Part 2: Next week + decisions</b>\n"]

    # Next week preview FIRST (always relevant)
    preview = agenda_data.get("next_week_preview", {})
    upcoming = preview.get("upcoming_meetings", [])
    deadlines = preview.get("deadlines", [])

    if upcoming:
        lines.append(f"<b>Next Week Meetings ({len(upcoming)}):</b>")
        for event in upcoming[:10]:
            title = _escape_html(event.get("title", "Untitled")[:60])
            start = event.get("start", "")
            if isinstance(start, str) and "T" in start:
                try:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    time_str = dt.strftime("%a %H:%M")
                except (ValueError, TypeError):
                    time_str = start
            else:
                time_str = str(start)[:16]
            lines.append(f"  • {time_str} — {title}")
        lines.append("")

    if deadlines:
        lines.append(f"<b>Upcoming Deadlines ({len(deadlines)}):</b>")
        for task in deadlines[:10]:
            title = _escape_html(task.get("title", "")[:60])
            deadline = task.get("deadline", "")
            lines.append(f"  • {title} (due {deadline})")
        lines.append("")

    # Gantt proposals (if any)
    gantt = agenda_data.get("gantt_proposals", {})
    proposals = gantt.get("proposals", [])
    if proposals:
        lines.append(f"<b>Gantt Proposals ({len(proposals)}):</b>")
        for i, p in enumerate(proposals[:10], 1):
            changes = p.get("changes", [])
            desc = changes[0].get("description", "Update") if changes else "Update"
            desc = _escape_html(desc[:80])
            source = p.get("source_type", "")
            lines.append(f"  {i}. {desc} (from {source})")
        lines.append("")

    result = "\n".join(lines)
    return result[:4000]


def _format_part3(agenda_data: dict, week_number: int) -> str:
    """Format Part 3: Outputs (generate + correct + approve)."""
    lines = [f"<b>Weekly Review W{week_number} — Part 3: Outputs</b>\n"]

    lines.append("Ready to generate your weekly outputs:")
    lines.append("  • PPTX Gantt slide")
    lines.append("  • HTML report (shareable link)")
    lines.append("  • Weekly digest\n")
    lines.append("Press <b>Generate Outputs</b> when ready, or ask questions first.")

    return "\n".join(lines)


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# =========================================================================
# Navigation & Helpers
# =========================================================================

def _detect_navigation(message: str) -> str | None:
    """Detect navigation intent from user message."""
    msg_lower = message.strip().lower()
    words = msg_lower.split()
    if len(words) >= 5:
        return None  # Long messages are probably questions, not navigation

    next_signals = ["next", "continue", "go on", "move on", ">>", "part 2", "part 3"]
    back_signals = ["back", "go back", "previous", "<<", "part 1"]
    end_signals = ["end", "end review", "stop", "cancel", "done"]

    for signal in next_signals:
        if msg_lower == signal or msg_lower.startswith(signal):
            return "next"

    for signal in back_signals:
        if msg_lower == signal or msg_lower.startswith(signal):
            return "back"

    for signal in end_signals:
        if msg_lower == signal or msg_lower.startswith(signal):
            return "end"

    return None


def _build_review_context(agenda_data: dict, current_part: int) -> str:
    """Build context string for the review LLM call."""
    parts = [f"CURRENT PART: {current_part}/3"]
    parts.append(f"PART INSTRUCTIONS: {get_part_prompt(current_part)}")
    parts.append("")

    if current_part == 1:
        wir = agenda_data.get("week_in_review", {})
        parts.append(f"WEEK DATA: {json.dumps(wir, default=str)[:2000]}")
        attention = agenda_data.get("attention_needed", {})
        parts.append(f"ATTENTION: {json.dumps(attention, default=str)[:1000]}")
    elif current_part == 2:
        gantt = agenda_data.get("gantt_proposals", {})
        parts.append(f"GANTT PROPOSALS: {json.dumps(gantt, default=str)[:1500]}")
        preview = agenda_data.get("next_week_preview", {})
        parts.append(f"NEXT WEEK: {json.dumps(preview, default=str)[:1000]}")
    elif current_part == 3:
        parts.append("OUTPUT GENERATION PHASE — help user with corrections or approve.")

    return "\n".join(parts)


def _is_session_expired(created_at: str) -> bool:
    """Check if a review session has exceeded its TTL."""
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        ).replace(tzinfo=None)
        elapsed = datetime.utcnow() - created
        return elapsed > timedelta(hours=settings.WEEKLY_REVIEW_SESSION_EXPIRY_HOURS)
    except (ValueError, TypeError):
        return False


def _parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM response, handling markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        logger.warning(f"Could not parse review LLM JSON: {text[:200]}")
        return {"response_text": text}
