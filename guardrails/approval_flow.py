"""
Eyal approval flow management.

This module handles the approval workflow from Section 9:
1. Send draft to Eyal (Telegram DM + email)
2. Process Eyal's response (approve/edit/reject)
3. Handle edit requests via conversational editing
4. Distribute approved content to team

Usage:
    from guardrails.approval_flow import submit_for_approval, process_response

    # Submit a draft
    await submit_for_approval(
        content_type="meeting_summary",
        content=summary_dict,
        meeting_id="uuid"
    )

    # Process Eyal's response
    result = await process_response(
        meeting_id="uuid",
        response="Change task 3 deadline to March 5"
    )
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from config.settings import settings
from core.llm import call_llm
from config.team import TEAM_MEMBERS, get_team_member
from services.telegram_bot import telegram_bot
from services.gmail import gmail_service
from services.google_drive import drive_service
from services.google_sheets import sheets_service
from services.supabase_client import supabase_client
from guardrails.sensitivity_classifier import get_distribution_list
from services.conversation_memory import conversation_memory

logger = logging.getLogger(__name__)


class ApprovalStatus(Enum):
    """Status of an approval request."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITING = "editing"  # Eyal requested changes


# =============================================================================
# Auto-Publish Timer (v0.2)
# =============================================================================

# Track pending auto-publish timers: {meeting_id: asyncio.Task}
_pending_auto_publishes: dict[str, asyncio.Task] = {}

# Track pending reminder tasks: {meeting_id: list[asyncio.Task]}
_pending_reminders: dict[str, list[asyncio.Task]] = {}

# NOTE: Pending approvals are now persisted in the `pending_approvals` Supabase
# table (v0.4).  The in-memory dict was removed — all reads/writes go through
# supabase_client.{create,get,update,delete}_pending_approval().


def _row_to_pending_info(row: dict) -> dict:
    """Convert a DB row from pending_approvals into the legacy in-memory format."""
    return {
        "type": row.get("content_type", "meeting_summary"),
        "content": row.get("content", {}),
    }


async def _auto_publish_after_delay(meeting_id: str, delay_minutes: int) -> None:
    """
    Background task that auto-approves after delay if still pending.

    Waits for delay_minutes, then checks if the meeting is still pending.
    If Eyal hasn't acted, auto-approves and notifies him.

    Args:
        meeting_id: UUID of the meeting.
        delay_minutes: Minutes to wait before auto-publishing.
    """
    await asyncio.sleep(delay_minutes * 60)

    # Check if still pending (Eyal may have already acted)
    meeting = supabase_client.get_meeting(meeting_id)
    if not meeting:
        return

    if meeting.get("approval_status") != "pending":
        logger.info(
            f"Auto-publish skipped for {meeting_id}: "
            f"already {meeting.get('approval_status')}"
        )
        return

    logger.info(
        f"Auto-publishing meeting {meeting_id} after {delay_minutes}m review window"
    )

    # Trigger approval
    result = await process_response(
        meeting_id=meeting_id,
        response="approve",
        response_source="auto_review",
    )

    # Notify Eyal that it was auto-published
    title = meeting.get("title", "Unknown meeting")
    await telegram_bot.send_to_eyal(
        f"Auto-published: *{title}*\n\n"
        f"The review window ({delay_minutes}min) expired without action.\n"
        f"Use /retract to undo if needed."
    )

    # Clean up
    _pending_auto_publishes.pop(meeting_id, None)


async def _send_approval_reminder(meeting_id: str, hours: int, content_type: str) -> None:
    """
    Send a gentle Telegram reminder that an approval is waiting.

    Args:
        meeting_id: Approval ID to check.
        hours: How many hours have passed (for the message).
        content_type: Type of content awaiting approval.
    """
    await asyncio.sleep(hours * 3600)

    # Check if still pending
    row = supabase_client.get_pending_approval(meeting_id)
    if not row or row.get("status") != "pending":
        return

    title = ""
    content = row.get("content", {})
    if isinstance(content, dict):
        title = content.get("title", content_type)
    else:
        title = content_type

    await telegram_bot.send_to_eyal(
        f"Reminder: \"{title}\" is awaiting your approval ({hours}h).\n"
        f"Reply approve/edit/reject, or use /status to see all pending items."
    )


def schedule_approval_reminders(meeting_id: str, content_type: str) -> None:
    """
    Schedule gentle reminder DMs for an unreviewed approval.

    Args:
        meeting_id: Approval ID.
        content_type: Type of content for the reminder message.
    """
    if not settings.APPROVAL_REMINDER_ENABLED:
        return

    cancel_approval_reminders(meeting_id)
    tasks = []
    for hours in settings.approval_reminder_hours_list:
        task = asyncio.create_task(
            _send_approval_reminder(meeting_id, hours, content_type),
            name=f"reminder_{meeting_id}_{hours}h",
        )
        tasks.append(task)
    if tasks:
        _pending_reminders[meeting_id] = tasks
        logger.info(f"Scheduled reminders for {meeting_id} at {settings.approval_reminder_hours_list}h")


def cancel_approval_reminders(meeting_id: str) -> None:
    """Cancel any pending reminders for this approval."""
    tasks = _pending_reminders.pop(meeting_id, [])
    for task in tasks:
        if not task.done():
            task.cancel()


def schedule_auto_publish(meeting_id: str) -> None:
    """
    Schedule auto-publish if in auto_review mode.

    Creates a background asyncio task that will auto-approve the meeting
    after AUTO_REVIEW_WINDOW_MINUTES if Eyal hasn't acted.

    Args:
        meeting_id: UUID of the meeting to schedule.
    """
    if settings.APPROVAL_MODE != "auto_review":
        return

    delay = settings.AUTO_REVIEW_WINDOW_MINUTES

    # Cancel any existing timer for this meeting
    existing = _pending_auto_publishes.get(meeting_id)
    if existing and not existing.done():
        existing.cancel()

    task = asyncio.create_task(
        _auto_publish_after_delay(meeting_id, delay),
        name=f"auto_publish_{meeting_id}"
    )
    _pending_auto_publishes[meeting_id] = task
    logger.info(f"Scheduled auto-publish for {meeting_id} in {delay} minutes")


def cancel_auto_publish(meeting_id: str) -> None:
    """
    Cancel pending auto-publish (called when Eyal acts manually).

    Args:
        meeting_id: UUID of the meeting to cancel timer for.
    """
    task = _pending_auto_publishes.pop(meeting_id, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"Cancelled auto-publish for {meeting_id}")


async def expire_stale_approvals() -> list[dict]:
    """
    Expire pending approvals past their expires_at timestamp.

    For prep_outline approvals:
    - If meeting is still in the future → auto-generate with defaults
    - If meeting has passed → expire silently

    Also cleans stale focus_active flags.

    Notifies Eyal about expired items so he's aware.

    Returns:
        List of expired approval records.
    """
    expired = supabase_client.expire_pending_approvals()
    if expired:
        lines = ["Expired approvals (no longer actionable):"]
        auto_generated = []

        for row in expired:
            ct = row.get("content_type", "unknown")
            approval_id = row["approval_id"]
            title = ""
            content = row.get("content", {})
            if isinstance(content, dict):
                title = content.get("title", ct)

            # Cancel any reminders/timers for expired items
            cancel_approval_reminders(approval_id)
            cancel_auto_publish(approval_id)

            # Special handling for prep_outline: auto-generate if meeting still future
            if ct == "prep_outline":
                event_start = content.get("event_start_time", "")
                meeting_future = False
                if event_start:
                    try:
                        from datetime import datetime, timezone
                        start_dt = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
                        meeting_future = start_dt > datetime.now(timezone.utc)
                    except (ValueError, TypeError):
                        pass

                if meeting_future:
                    try:
                        # Re-set to pending temporarily so generate can read it
                        supabase_client.update_pending_approval(approval_id, status="pending")
                        from processors.meeting_prep import generate_meeting_prep_from_outline
                        result = await generate_meeting_prep_from_outline(approval_id)
                        if result.get("status") == "success":
                            auto_generated.append(title)
                            lines.append(f"  - {title} (prep_outline → auto-generated)")
                            continue
                    except Exception as e:
                        logger.error(f"Auto-generate on expiry failed for {approval_id}: {e}")

                # Past meeting or auto-gen failed — just expire silently
                lines.append(f"  - {title} (prep_outline, meeting passed)")
                continue

            lines.append(f"  - {title} ({ct})")

        if auto_generated:
            await telegram_bot.send_to_eyal(
                f"Auto-generated {len(auto_generated)} prep(s) on expiry: "
                f"{', '.join(auto_generated)}"
            )

        await telegram_bot.send_to_eyal("\n".join(lines))
        logger.info(f"Expired {len(expired)} stale approvals")

    # Clean stale focus_active flags
    try:
        focus_timeout = settings.MEETING_PREP_FOCUS_TIMEOUT_MINUTES
        pending_preps = supabase_client.get_pending_prep_outlines()
        for pp in pending_preps:
            content = pp.get("content", {})
            if content.get("focus_active"):
                # Check if it's been too long
                updated_at = pp.get("updated_at", pp.get("created_at", ""))
                if updated_at:
                    try:
                        from datetime import datetime, timezone, timedelta
                        updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        if datetime.now(timezone.utc) - updated_dt > timedelta(minutes=focus_timeout):
                            content["focus_active"] = False
                            supabase_client.update_pending_approval(
                                pp["approval_id"], content=content
                            )
                            logger.info(f"Cleared stale focus_active for {pp['approval_id']}")
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        logger.debug(f"Focus cleanup failed: {e}")

    return expired


async def reconstruct_auto_publish_timers() -> int:
    """
    Reconstruct auto-publish timers from persistent state on startup.

    Queries the pending_approvals table for rows with auto_publish_at,
    and schedules asyncio tasks for each one:
    - If auto_publish_at is in the past → auto-approve immediately.
    - If auto_publish_at is in the future → schedule with remaining delay.

    Returns:
        Number of timers reconstructed.
    """
    rows = supabase_client.get_pending_auto_publishes()
    if not rows:
        logger.info("No auto-publish timers to reconstruct")
        return 0

    count = 0
    now = datetime.now().astimezone()

    for row in rows:
        approval_id = row["approval_id"]
        auto_publish_at_str = row["auto_publish_at"]

        try:
            auto_publish_at = datetime.fromisoformat(auto_publish_at_str)
            # Ensure timezone-aware comparison
            if auto_publish_at.tzinfo is None:
                auto_publish_at = auto_publish_at.astimezone()

            remaining_seconds = (auto_publish_at - now).total_seconds()

            if remaining_seconds <= 0:
                # Expired — auto-approve immediately
                logger.info(f"Auto-publish timer expired for {approval_id}, approving now")
                asyncio.create_task(
                    process_response(
                        meeting_id=approval_id,
                        response="approve",
                        response_source="auto_review",
                    ),
                    name=f"auto_publish_expired_{approval_id}",
                )
            else:
                # Future — schedule with remaining delay
                remaining_minutes = remaining_seconds / 60
                logger.info(
                    f"Reconstructing auto-publish timer for {approval_id}: "
                    f"{remaining_minutes:.1f} minutes remaining"
                )
                task = asyncio.create_task(
                    _auto_publish_after_delay(
                        approval_id,
                        delay_minutes=remaining_minutes,
                    ),
                    name=f"auto_publish_{approval_id}",
                )
                _pending_auto_publishes[approval_id] = task

            count += 1
        except Exception as e:
            logger.error(f"Failed to reconstruct timer for {approval_id}: {e}")

    logger.info(f"Reconstructed {count} auto-publish timers")
    return count


async def submit_for_approval(
    content_type: str,
    content: dict,
    meeting_id: str
) -> dict:
    """
    Submit content for Eyal's approval.

    Sends to both Telegram DM and email for redundancy.

    Args:
        content_type: Type of content ('meeting_summary', 'meeting_prep', etc.)
        content: The content dict to approve.
        meeting_id: UUID for tracking.

    Returns:
        Dict with:
        - approval_id: UUID for this approval request
        - status: 'pending'
        - sent_at: Timestamp
    """
    logger.info(f"Submitting {content_type} for approval: {meeting_id}")

    # Check for existing pending approvals of the same type
    existing_pending = supabase_client.get_pending_approvals_by_status("pending")
    same_type_pending = [
        a for a in existing_pending
        if a.get("content_type") == content_type and a.get("approval_id") != meeting_id
    ]
    queue_note = ""
    if same_type_pending:
        queue_note = (
            f"\n\nNote: {len(same_type_pending)} other {content_type} approval(s) "
            f"already pending."
        )

    # Persist approval state to Supabase (survives restarts)
    # Delete any stale row first (upsert pattern for edit re-submissions)
    supabase_client.delete_pending_approval(meeting_id)

    # Calculate auto_publish_at if in auto_review mode
    auto_publish_at = None
    if settings.APPROVAL_MODE == "auto_review":
        auto_publish_at = (
            datetime.now() + timedelta(minutes=settings.AUTO_REVIEW_WINDOW_MINUTES)
        ).isoformat()

    # Calculate expires_at per content type
    expiry_map = {
        "morning_brief": timedelta(hours=24),
        "debrief": timedelta(hours=48),
        "weekly_digest": timedelta(days=7),
        "weekly_review": timedelta(days=7),  # Phase 6
        "prep_outline": timedelta(hours=24),  # Phase 5
    }
    expires_at = None
    if content_type in expiry_map:
        expires_at = (datetime.now() + expiry_map[content_type]).isoformat()
    elif content_type == "prep_outline":
        # Use template-specific lead hours if available
        outline_lead = content.get("outline_lead_hours", 24)
        expires_at = (datetime.now() + timedelta(hours=outline_lead)).isoformat()

    supabase_client.create_pending_approval(
        approval_id=meeting_id,
        content_type=content_type,
        content=content,
        auto_publish_at=auto_publish_at,
        expires_at=expires_at,
    )

    # Extract key info for formatting
    meeting_title = content.get("title", "Untitled Meeting")
    summary = content.get("summary", "")
    decisions = content.get("decisions", [])
    tasks = content.get("tasks", [])
    follow_ups = content.get("follow_ups", [])
    open_questions = content.get("open_questions", [])
    discussion_summary = content.get("discussion_summary", "")
    executive_summary = content.get("executive_summary", "")
    meeting_date = content.get("date", datetime.now().strftime("%Y-%m-%d"))

    if content_type == "meeting_prep":
        # Meeting prep — minimal Telegram card (no Drive link yet — uploaded after approval).
        # Sent directly (not through send_approval_request which adds
        # "Discussion Summary" wrapping and HTML-escapes our HTML).
        from processors.meeting_prep import format_prep_approval_card
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        sensitivity = content.get("sensitivity", "normal")
        section_names = None
        if content.get("meeting_type"):
            from config.meeting_prep_templates import get_template
            tmpl = get_template(content["meeting_type"])
            section_names = [s for s in tmpl.get("structure", []) if s != "Suggested Agenda"]

        card = format_prep_approval_card(
            title=meeting_title,
            start_time=content.get("start_time", ""),
            sections=section_names,
            sensitivity=sensitivity,
        )

        keyboard = [
            [
                InlineKeyboardButton("Approve + send", callback_data=f"approve:{meeting_id}"),
                InlineKeyboardButton("Edit", callback_data=f"edit:{meeting_id}"),
            ],
            [
                InlineKeyboardButton("Reject", callback_data=f"reject:{meeting_id}"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        telegram_sent = await telegram_bot.send_to_eyal(
            card, reply_markup=reply_markup, parse_mode="HTML"
        )

        email_sent = await gmail_service.send_approval_request(
            meeting_title=f"Meeting Prep: {meeting_title}",
            summary_preview=f"Prep document ready for: {meeting_title}. Review and approve via Telegram.",
            meeting_id=meeting_id,
        )

    elif content_type == "weekly_digest":
        # Weekly digest — send preview to Eyal for approval
        week_of = content.get("week_of", "")
        digest_doc = content.get("digest_document", "")
        preview = (
            f"<b>Weekly Digest — Awaiting Approval</b>\n\n"
            f"Week of: {week_of}\n"
            f"Meetings: {content.get('meetings_count', 0)}\n"
            f"Decisions: {content.get('decisions_count', 0)}\n"
            f"Tasks completed: {content.get('tasks_completed', 0)}\n"
            f"Tasks overdue: {content.get('tasks_overdue', 0)}\n"
        )

        telegram_sent = await telegram_bot.send_approval_request(
            meeting_title=f"Weekly Digest — {week_of}",
            summary_preview=preview,
            meeting_id=meeting_id,
        )
        email_sent = await gmail_service.send_approval_request(
            meeting_title=f"Weekly Digest — Week of {week_of}",
            summary_preview=digest_doc[:1000],
            meeting_id=meeting_id,
        )

    elif content_type == "gantt_update":
        # Gantt update proposal — render human-readable diff for approval
        proposal_id = content.get("proposal_id", "")
        changes = content.get("changes", [])
        changes_count = content.get("changes_count", 0)

        preview_lines = [f"<b>Gantt Update Proposal — Awaiting Approval</b>\n"]
        preview_lines.append(f"Changes: {changes_count} cell(s)\n")
        for i, change in enumerate(changes[:10], 1):
            section = change.get("section", "")
            subsection = change.get("subsection", "")
            week = change.get("week", "")
            old_val = change.get("old_value", "")
            new_val = change.get("new_value", "")
            if old_val:
                preview_lines.append(
                    f"({i}) {section} &gt; {subsection}, W{week}: "
                    f"'{old_val}' → '{new_val}'"
                )
            else:
                preview_lines.append(
                    f"({i}) {section} &gt; {subsection}, W{week}: "
                    f"added '{new_val}' (was empty)"
                )
        if len(changes) > 10:
            preview_lines.append(f"... and {len(changes) - 10} more changes")

        preview = "\n".join(preview_lines)

        telegram_sent = await telegram_bot.send_approval_request(
            meeting_title=f"Gantt Update ({changes_count} cells)",
            summary_preview=preview,
            meeting_id=meeting_id,
        )
        email_sent = await gmail_service.send_approval_request(
            meeting_title=f"Gantt Update Proposal ({changes_count} cells)",
            summary_preview=preview.replace("<b>", "").replace("</b>", "").replace("&gt;", ">"),
            meeting_id=meeting_id,
        )

    elif content_type == "morning_brief":
        # Morning brief — consolidated daily touchpoint
        formatted = content.get("formatted", "")
        stats = content.get("stats", {})
        total = stats.get("email_scans", 0) + stats.get("constant_items", 0)

        preview = formatted if formatted else "<b>Morning Brief</b>\n\nNo details available."

        telegram_sent = await telegram_bot.send_approval_request(
            meeting_title=f"Morning Brief ({total} email items)",
            summary_preview=preview,
            meeting_id=meeting_id,
        )
        email_sent = await gmail_service.send_approval_request(
            meeting_title="Morning Brief",
            summary_preview=preview.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""),
            meeting_id=meeting_id,
        )

    elif content_type == "weekly_review":
        # Weekly review — handled by its own session flow.
        # Just log the submission; notification sent via session.
        logger.info(
            f"Weekly review submitted for approval: W{content.get('week_number', '')}"
        )
        telegram_sent = True  # Sent separately by session flow
        email_sent = False  # Review is Telegram-only

    elif content_type == "prep_outline":
        # Prep outline — Telegram-only, no email. The outline is sent via
        # telegram_bot.send_prep_outline() by the scheduler, not here.
        # We just log the submission.
        event = content.get("event", {})
        meeting_title = event.get("title", "Untitled")
        meeting_type = content.get("meeting_type", "generic")
        mode = content.get("timeline_mode", "normal")
        logger.info(
            f"Prep outline submitted: {meeting_title} (type={meeting_type}, mode={mode})"
        )
        telegram_sent = True  # Sent separately by scheduler
        email_sent = False  # Outlines are Telegram-only

    else:
        # Default: meeting_summary (original behavior)
        # v0.3: Include cross-reference results if available
        cross_ref = content.get("cross_reference")

        telegram_sent = await telegram_bot.send_approval_request(
            meeting_title=meeting_title,
            summary_preview=discussion_summary or summary[:600],
            meeting_id=meeting_id,
            decisions=decisions,
            tasks=tasks,
            follow_ups=follow_ups,
            open_questions=open_questions,
            cross_reference=cross_ref,
            executive_summary=executive_summary,
        )

        email_sent = await gmail_service.send_approval_request(
            meeting_title=meeting_title,
            summary_preview=discussion_summary or summary[:600],
            executive_summary=executive_summary,
            decisions=decisions,
            tasks=tasks,
            follow_ups=follow_ups,
            open_questions=open_questions,
            meeting_id=meeting_id,
        )

    # Inject approval context into conversation memory so the agent knows
    # what summary it just sent (enables follow-up questions and edits)
    preview = discussion_summary or summary[:600]
    if telegram_sent and settings.TELEGRAM_EYAL_CHAT_ID:
        conversation_memory.inject_approval_context(
            chat_id=settings.TELEGRAM_EYAL_CHAT_ID,
            meeting_id=meeting_id,
            title=meeting_title,
            preview=preview,
        )
    if email_sent and settings.EYAL_EMAIL:
        conversation_memory.inject_approval_context(
            chat_id=settings.EYAL_EMAIL,
            meeting_id=meeting_id,
            title=meeting_title,
            preview=preview,
        )

    # Log the action
    supabase_client.log_action(
        action="approval_requested",
        details={
            "meeting_id": meeting_id,
            "content_type": content_type,
            "telegram_sent": telegram_sent,
            "email_sent": email_sent,
        },
        triggered_by="auto",
    )

    sent_at = datetime.now().isoformat()

    logger.info(
        f"Approval request sent: Telegram={telegram_sent}, Email={email_sent}"
    )

    # Schedule auto-publish if in auto_review mode
    schedule_auto_publish(meeting_id)

    # Schedule gentle reminders
    schedule_approval_reminders(meeting_id, content_type)

    return {
        "approval_id": meeting_id,  # Use meeting_id as approval_id
        "status": "pending",
        "sent_at": sent_at,
        "telegram_sent": telegram_sent,
        "email_sent": email_sent,
    }


async def process_response(
    meeting_id: str,
    response: str,
    response_source: str = "telegram",
    force_action: str | None = None,
) -> dict:
    """
    Process Eyal's response to an approval request.

    Args:
        meeting_id: UUID of the meeting/content.
        response: Eyal's response text.
        response_source: Where the response came from ('telegram' or 'email').
        force_action: If set, skip parsing and use this action directly.
            Used when the caller already knows the intent (e.g., edit mode).

    Returns:
        Dict with:
        - action: 'approved', 'rejected', or 'edit_requested'
        - edits: List of changes to make (if edit_requested)
        - next_step: What happens next
    """
    logger.info(f"Processing approval response for {meeting_id}: {response[:50]}...")

    # Parse the response (or use forced action from caller)
    if force_action:
        action = force_action
    else:
        parsed = parse_approval_response(response)
        action = parsed["action"]

    # Detect content type from persisted pending approvals (Supabase).
    # Non-meeting content (digests, preps) uses IDs like "digest-2026-02-23"
    # which aren't UUIDs, so we can't query the meetings table.
    pending_row = supabase_client.get_pending_approval(meeting_id)
    pending_info = _row_to_pending_info(pending_row) if pending_row else None
    is_non_meeting = (
        meeting_id.startswith("digest-")
        or meeting_id.startswith("prep-")
        or meeting_id.startswith("outline-")
        or meeting_id.startswith("gantt-")
        or meeting_id.startswith("brief-")
        or meeting_id.startswith("review-")
        or (pending_info and pending_info["type"] in ("weekly_digest", "meeting_prep", "gantt_update", "morning_brief", "prep_outline", "weekly_review"))
    )

    # Phase 5: Reject email responses for prep_outline — Telegram-only
    if pending_info and pending_info["type"] == "prep_outline" and response_source == "email":
        return {
            "action": "error",
            "error": "Prep outlines can only be managed via Telegram.",
            "next_step": "Use Telegram to review this prep outline.",
        }

    if is_non_meeting:
        # Non-meeting content — build a stub meeting dict, skip DB lookup
        title = pending_info["content"].get("title", "") if pending_info else meeting_id
        meeting = {"id": meeting_id, "title": title}
    else:
        # Regular meeting — get from database
        meeting = supabase_client.get_meeting(meeting_id)
        if not meeting:
            logger.error(f"Meeting not found: {meeting_id}")
            return {
                "action": "error",
                "error": "Meeting not found",
                "next_step": "Please try again",
            }

    # Cancel any pending auto-publish timer and reminders (Eyal is acting manually)
    cancel_auto_publish(meeting_id)
    cancel_approval_reminders(meeting_id)

    if action == "approve":
        # Delete from persistent store (already fetched above, now remove it)
        supabase_client.delete_pending_approval(meeting_id)

        # Determine content type from pending_info or ID prefix
        if pending_info:
            content_type = pending_info["type"]
        elif meeting_id.startswith("digest-"):
            content_type = "weekly_digest"
        elif meeting_id.startswith("prep-"):
            content_type = "meeting_prep"
        elif meeting_id.startswith("gantt-"):
            content_type = "gantt_update"
        elif meeting_id.startswith("brief-"):
            content_type = "morning_brief"
        elif meeting_id.startswith("review-"):
            content_type = "weekly_review"
        else:
            content_type = "meeting_summary"

        # Update approval status in DB (skip for non-meeting content like digests)
        if content_type == "meeting_summary":
            await update_approval_status(meeting_id, ApprovalStatus.APPROVED)

        if content_type == "meeting_prep":
            if not pending_info:
                # Content was submitted from another process (lost on restart)
                logger.warning(f"Meeting prep {meeting_id} approved but content not found — cannot upload to Drive")
                supabase_client.log_action(action="approval_status_approved", details={"meeting_id": meeting_id}, triggered_by="eyal")
                return {"action": "approved", "edits": None, "next_step": "Approved but content not found — please regenerate"}
            distribution_result = await distribute_approved_prep(
                meeting_id=meeting_id,
                content=pending_info["content"],
            )
            return {
                "action": "approved",
                "edits": None,
                "next_step": "Meeting prep distributed to team",
                "distribution": distribution_result,
            }

        elif content_type == "weekly_digest":
            if not pending_info:
                # Content was submitted from another process (lost on restart)
                logger.warning(f"Weekly digest {meeting_id} approved but content not in memory (already saved to Drive)")
                supabase_client.log_action(action="approval_status_approved", details={"meeting_id": meeting_id}, triggered_by="eyal")
                return {"action": "approved", "edits": None, "next_step": "Approved (content already saved to Drive)"}
            distribution_result = await distribute_approved_digest(
                meeting_id=meeting_id,
                content=pending_info["content"],
            )
            return {
                "action": "approved",
                "edits": None,
                "next_step": "Weekly digest distributed to team",
                "distribution": distribution_result,
            }

        elif content_type == "gantt_update":
            if not pending_info:
                logger.warning(f"Gantt update {meeting_id} approved but content not in memory")
                supabase_client.log_action(action="approval_status_approved", details={"meeting_id": meeting_id}, triggered_by="eyal")
                return {"action": "approved", "edits": None, "next_step": "Approved (proposal may have already been executed)"}

            proposal_id = pending_info["content"].get("proposal_id")
            if proposal_id:
                from core.operator_agent import OperatorAgent
                operator = OperatorAgent()
                exec_result = await operator.execute_gantt_update(proposal_id)

                # Notify Eyal of the result
                if exec_result.get("status") == "executed":
                    cells_written = exec_result.get("cells_written", 0)
                    await telegram_bot.send_to_eyal(
                        f"Gantt updated: {cells_written} cell(s) written successfully."
                    )
                else:
                    error = exec_result.get("error", "Unknown error")
                    await telegram_bot.send_to_eyal(
                        f"Gantt update failed: {error}"
                    )

                return {
                    "action": "approved",
                    "edits": None,
                    "next_step": "Gantt chart updated",
                    "execution": exec_result,
                }
            return {"action": "approved", "edits": None, "next_step": "Approved but no proposal_id found"}

        elif content_type == "morning_brief":
            if not pending_info:
                logger.warning(f"Morning brief {meeting_id} approved but content not in memory")
                supabase_client.log_action(action="approval_status_approved", details={"meeting_id": meeting_id}, triggered_by="eyal")
                return {"action": "approved", "edits": None, "next_step": "Approved (brief content not found)"}

            result = await _apply_morning_brief_approval(pending_info["content"])
            return {
                "action": "approved",
                "edits": None,
                "next_step": "Morning brief items injected",
                "injection": result,
            }

        elif content_type == "weekly_review":
            # Weekly review handled by its own session flow (confirm_review)
            # This path is for standalone approvals submitted outside the session
            if not pending_info:
                logger.warning(f"Weekly review {meeting_id} approved but content not in memory")
                supabase_client.log_action(action="approval_status_approved", details={"meeting_id": meeting_id}, triggered_by="eyal")
                return {"action": "approved", "edits": None, "next_step": "Approved (review content not found)"}

            distribution_result = await distribute_approved_review(
                session_id=pending_info["content"].get("session_id", ""),
                agenda_data=pending_info["content"].get("agenda_data", {}),
                week_number=pending_info["content"].get("week_number", 0),
                year=pending_info["content"].get("year", 0),
            )
            return {
                "action": "approved",
                "edits": None,
                "next_step": "Weekly review distributed",
                "distribution": distribution_result,
            }

        # Default: meeting_summary — gather all related data.
        # Prefer edited content from pending_approvals (has edits applied)
        # over raw DB tables (which may still have pre-edit data).
        pending_content = (pending_info or {}).get("content", {})
        has_edited_data = any(
            key in pending_content
            for key in ("tasks", "decisions", "follow_ups", "open_questions")
        )

        content = {
            "title": pending_content.get("title") or meeting.get("title"),
            "summary": pending_content.get("summary") or meeting.get("summary"),
            "date": pending_content.get("date") or meeting.get("date"),
            "executive_summary": pending_content.get("executive_summary", ""),
            "discussion_summary": pending_content.get("discussion_summary", ""),
            "stakeholders": pending_content.get("stakeholders", []),
        }

        if has_edited_data:
            # Use edited data from pending_approvals (post-edit version)
            content["decisions"] = pending_content.get("decisions", [])
            content["tasks"] = pending_content.get("tasks", [])
            content["follow_ups"] = pending_content.get("follow_ups", [])
            content["open_questions"] = pending_content.get("open_questions", [])
            logger.info("Using edited content from pending_approvals for distribution")
        else:
            # No edits — read fresh from DB tables
            decisions = supabase_client.list_decisions(meeting_id=meeting_id)
            tasks = supabase_client.get_tasks(status=None)
            tasks = [t for t in tasks if t.get("meeting_id") == meeting_id]
            follow_ups = supabase_client.list_follow_up_meetings(
                source_meeting_id=meeting_id
            )
            open_questions = supabase_client.get_open_questions(
                meeting_id=meeting_id
            )
            content["decisions"] = decisions
            content["tasks"] = tasks
            content["follow_ups"] = follow_ups
            content["open_questions"] = open_questions

        # v0.3: Carry cross-reference data through to distribution
        if pending_info and pending_info.get("content", {}).get("cross_reference"):
            content["cross_reference"] = pending_info["content"]["cross_reference"]

        sensitivity = meeting.get("sensitivity", "normal")

        distribution_result = await distribute_approved_content(
            meeting_id=meeting_id,
            content=content,
            sensitivity=sensitivity,
        )

        return {
            "action": "approved",
            "edits": None,
            "next_step": "Content distributed to team",
            "distribution": distribution_result,
        }

    elif action == "reject":
        # Update status (skip for non-meeting content)
        if not is_non_meeting:
            await update_approval_status(meeting_id, ApprovalStatus.REJECTED)

        # Remove from persistent store if still there
        supabase_client.delete_pending_approval(meeting_id)

        # Log rejection
        supabase_client.log_action(
            action="approval_rejected",
            details={"meeting_id": meeting_id},
            triggered_by="eyal",
        )

        return {
            "action": "rejected",
            "edits": None,
            "next_step": "Content discarded",
        }

    else:  # action == "edit"
        # Update status to editing (skip for non-meeting content)
        if not is_non_meeting:
            await update_approval_status(meeting_id, ApprovalStatus.EDITING)

        # Determine content type for resubmission
        resubmit_content_type = "meeting_summary"
        if pending_info:
            resubmit_content_type = pending_info.get("type", "meeting_summary")
        elif meeting_id.startswith("prep-"):
            resubmit_content_type = "meeting_prep"
        elif meeting_id.startswith("digest-"):
            resubmit_content_type = "weekly_digest"

        # Parse edit instructions
        edits = await parse_edit_instructions_with_claude(response, meeting)

        if edits:
            if resubmit_content_type == "meeting_prep":
                # Meeting prep edits: apply to the summary text, carry over prep fields
                updated_content = await apply_edits(meeting_id, edits)

                # Carry over all prep-specific fields
                if pending_info and pending_info.get("content"):
                    for key in ("title", "start_time", "sensitivity", "meeting_type",
                                "focus_instructions", "sections", "attendees"):
                        if key in pending_info["content"] and key not in updated_content:
                            updated_content[key] = pending_info["content"][key]
            else:
                # Meeting summary edits: original flow
                current_structured = {}
                if pending_info and pending_info.get("content"):
                    for key in ("decisions", "tasks", "follow_ups", "open_questions"):
                        if key in pending_info["content"]:
                            current_structured[key] = pending_info["content"][key]

                updated_content = await apply_edits(
                    meeting_id, edits, structured_data=current_structured
                )

                if pending_info and pending_info.get("content"):
                    for key in ("executive_summary", "discussion_summary",
                                "stakeholders", "cross_reference", "commitments"):
                        if key in pending_info["content"] and key not in updated_content:
                            updated_content[key] = pending_info["content"][key]

            # Resubmit for approval with edited content
            await submit_for_approval(
                content_type=resubmit_content_type,
                content=updated_content,
                meeting_id=meeting_id,
            )

            return {
                "action": "edit_requested",
                "edits": edits,
                "next_step": "Edits applied, resubmitted for approval",
            }
        else:
            return {
                "action": "edit_requested",
                "edits": [],
                "next_step": "Could not parse edits, please clarify",
            }


def parse_approval_response(response: str) -> dict:
    """
    Parse Eyal's response to determine the action.

    Supports:
    - "Approve", "Yes", check mark emoji -> approved
    - "Reject", "No", "Discard" -> rejected
    - Any other text -> edit request

    Args:
        response: The raw response text.

    Returns:
        Dict with:
        - action: 'approve', 'reject', or 'edit'
        - edits: Parsed edit instructions (if action is 'edit')
    """
    response_lower = response.strip().lower()

    # Check for approval signals (including emoji)
    approval_signals = [
        "approve", "approved", "yes", "ok", "looks good", "lgtm",
        "good", "ship it", "send it", "go ahead"
    ]
    if any(signal in response_lower for signal in approval_signals):
        return {"action": "approve", "edits": None}

    # Check for check mark emoji
    if "✅" in response or "👍" in response:
        return {"action": "approve", "edits": None}

    # Check for rejection signals
    # "delete" removed — "delete X from the summary" is an edit, not a rejection.
    # "no" and "stop" removed — too broad, match edit instructions like "no, change it".
    rejection_signals = [
        "reject", "rejected", "discard", "don't send", "cancel",
    ]
    if any(signal in response_lower for signal in rejection_signals):
        return {"action": "reject", "edits": None}

    # Check for X emoji
    if "❌" in response or "👎" in response:
        return {"action": "reject", "edits": None}

    # Otherwise, treat as edit request
    return {"action": "edit", "edits": response}


async def parse_edit_instructions_with_claude(
    response: str,
    meeting: dict
) -> list[dict]:
    """
    Use Claude to parse natural language edit instructions.

    Args:
        response: The edit instruction text from Eyal.
        meeting: The current meeting data for context.

    Returns:
        List of structured edit instructions.
    """
    prompt = f"""Parse the following edit instructions for a meeting summary.

CURRENT SUMMARY:
{meeting.get('summary', '')[:2000]}

EDIT INSTRUCTIONS FROM EYAL:
{response}

Parse these into structured edits. Return a JSON array where each edit has:
- "type": "modify" | "remove" | "add"
- "section": "tasks" | "decisions" | "summary" | "follow_ups" | "open_questions"
- "target": specific item to edit (e.g., "task 3", "first decision")
- "change": what to change (for modify/add)

Example output:
[
    {{"type": "modify", "section": "tasks", "target": "task 3", "change": "deadline to March 5"}},
    {{"type": "remove", "section": "open_questions", "target": "second question", "change": null}},
    {{"type": "add", "section": "decisions", "target": null, "change": "We decided to use AWS"}}
]

Return ONLY the JSON array, no other text."""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=1024,
            call_site="edit_parsing",
        )

        # Parse JSON from response
        edits = json.loads(response_text)
        logger.info(f"Parsed {len(edits)} edit instructions")
        return edits

    except Exception as e:
        logger.error(f"Error parsing edit instructions: {e}")
        # Return a simple text-based edit as fallback
        return [{
            "type": "modify",
            "section": "summary",
            "target": "full",
            "change": response,
        }]


async def apply_edits(
    meeting_id: str,
    edits: list[dict],
    structured_data: dict | None = None,
) -> dict:
    """
    Apply parsed edits to the meeting summary AND structured data.

    Edits are applied to the full content (summary text, decisions, tasks,
    follow-ups, open questions) so that name corrections, deletions, etc.
    are reflected everywhere — not just in the summary text.

    Args:
        meeting_id: UUID of the meeting.
        edits: List of edit instructions.
        structured_data: Dict with decisions, tasks, follow_ups, open_questions.
            If None, fetched from DB.

    Returns:
        Updated content dict with all sections.
    """
    # Get current meeting data
    meeting = supabase_client.get_meeting(meeting_id)
    if not meeting:
        logger.error(f"Meeting not found: {meeting_id}")
        return {}

    current_summary = meeting.get("summary", "")

    # Gather structured data if not provided
    if structured_data is None:
        structured_data = {}
    decisions = structured_data.get("decisions") or supabase_client.list_decisions(
        meeting_id=meeting_id
    )
    all_tasks = structured_data.get("tasks")
    if all_tasks is None:
        all_tasks_list = supabase_client.get_tasks(status=None)
        all_tasks = [t for t in all_tasks_list if t.get("meeting_id") == meeting_id]
    follow_ups = structured_data.get("follow_ups") or supabase_client.list_follow_up_meetings(
        source_meeting_id=meeting_id
    )
    open_questions = structured_data.get("open_questions") or supabase_client.get_open_questions(
        meeting_id=meeting_id
    )

    edits_description = json.dumps(edits, indent=2)

    # Build a complete content representation for the LLM
    # so edits are applied to ALL sections, not just the summary.
    decisions_text = json.dumps(
        [{"index": i + 1, "description": d.get("description", "")}
         for i, d in enumerate(decisions)],
        indent=2,
    )
    tasks_text = json.dumps(
        [{"index": i + 1, "title": t.get("title", ""), "assignee": t.get("assignee", ""),
          "priority": t.get("priority", "M"), "deadline": t.get("deadline"),
          "category": t.get("category", ""), "status": t.get("status", "pending")}
         for i, t in enumerate(all_tasks)],
        indent=2,
    )
    follow_ups_text = json.dumps(
        [{"index": i + 1, "title": f.get("title", ""), "led_by": f.get("led_by", "")}
         for i, f in enumerate(follow_ups)],
        indent=2,
    )
    questions_text = json.dumps(
        [{"index": i + 1, "question": q.get("question", ""), "raised_by": q.get("raised_by", "")}
         for i, q in enumerate(open_questions)],
        indent=2,
    )

    prompt = f"""Apply the following edits to this meeting content.

CRITICAL RULES:
- Apply the edits to ALL sections: summary, decisions, tasks, follow-ups, and questions.
- If an edit says to rename someone, rename them EVERYWHERE they appear.
- If an edit says to delete/remove something, remove it from ALL sections where it appears.
- Preserve the exact format and structure of each section. Only change what the edits require.
- Return valid JSON with the structure shown below.

CURRENT SUMMARY:
{current_summary}

CURRENT DECISIONS:
{decisions_text}

CURRENT TASKS:
{tasks_text}

CURRENT FOLLOW-UP MEETINGS:
{follow_ups_text}

CURRENT OPEN QUESTIONS:
{questions_text}

EDITS TO APPLY:
{edits_description}

Return a JSON object with these keys:
- "summary": the full updated summary text
- "decisions": updated array of decisions (same format as above, remove items if edit says to delete)
- "tasks": updated array of tasks (same format as above, remove items if edit says to delete)
- "follow_ups": updated array of follow-up meetings (same format)
- "open_questions": updated array of open questions (same format)

Return ONLY the JSON object, no other text."""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_background,
            max_tokens=8192,
            call_site="edit_application",
            meeting_id=meeting_id,
        )

        # Parse JSON response
        # Strip markdown code fences if present
        clean = response_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        clean = clean.strip()

        edited = json.loads(clean)
        updated_summary = edited.get("summary", current_summary)

        # Update the meeting summary in database
        supabase_client.update_meeting(
            meeting_id,
            summary=updated_summary,
            approval_status="pending",
        )

        # Build edited structured data for the approval message.
        # Map LLM output back to the format expected by submit_for_approval.
        edited_decisions = []
        for d in edited.get("decisions", []):
            edited_decisions.append({"description": d.get("description", "")})

        edited_tasks = []
        for t in edited.get("tasks", []):
            edited_tasks.append({
                "title": t.get("title", ""),
                "assignee": t.get("assignee", ""),
                "priority": t.get("priority", "M"),
                "deadline": t.get("deadline"),
                "category": t.get("category", ""),
                "status": t.get("status", "pending"),
            })

        edited_follow_ups = []
        for f in edited.get("follow_ups", []):
            edited_follow_ups.append({
                "title": f.get("title", ""),
                "led_by": f.get("led_by", ""),
            })

        edited_questions = []
        for q in edited.get("open_questions", []):
            edited_questions.append({
                "question": q.get("question", ""),
                "raised_by": q.get("raised_by", ""),
            })

        logger.info(f"Applied {len(edits)} edits to meeting {meeting_id}")

        # Sync edited structured data back to DB tables.
        # This ensures DB, pending_approvals, distribution, and Sheets stay consistent.
        try:
            # Delete old records and insert edited ones for this meeting
            # Tasks
            old_tasks = supabase_client.get_tasks(status=None)
            old_task_ids = [t["id"] for t in old_tasks if t.get("meeting_id") == meeting_id]
            for tid in old_task_ids:
                supabase_client.client.table("tasks").delete().eq("id", tid).execute()
            if edited_tasks:
                supabase_client.create_tasks_batch(meeting_id, edited_tasks)

            # Decisions
            supabase_client.client.table("decisions").delete().eq(
                "meeting_id", meeting_id
            ).execute()
            if edited_decisions:
                supabase_client.create_decisions_batch(meeting_id, edited_decisions)

            # Follow-ups
            supabase_client.client.table("follow_up_meetings").delete().eq(
                "source_meeting_id", meeting_id
            ).execute()
            if edited_follow_ups:
                for fu in edited_follow_ups:
                    supabase_client.create_follow_up_meeting(
                        source_meeting_id=meeting_id,
                        title=fu.get("title", ""),
                        led_by=fu.get("led_by", ""),
                    )

            # Open questions
            supabase_client.client.table("open_questions").delete().eq(
                "meeting_id", meeting_id
            ).execute()
            if edited_questions:
                for q in edited_questions:
                    supabase_client.create_open_question(
                        meeting_id=meeting_id,
                        question=q.get("question", ""),
                        raised_by=q.get("raised_by", ""),
                    )

            logger.info(f"Synced edited data to DB for meeting {meeting_id}")
        except Exception as e:
            logger.error(f"Failed to sync edited data to DB: {e}")

        return {
            "title": meeting.get("title"),
            "summary": updated_summary,
            "date": meeting.get("date"),
            "decisions": edited_decisions,
            "tasks": edited_tasks,
            "follow_ups": edited_follow_ups,
            "open_questions": edited_questions,
        }

    except Exception as e:
        logger.error(f"Error applying edits: {e}")
        return {"error": str(e)}


async def distribute_approved_content(
    meeting_id: str,
    content: dict,
    sensitivity: str = "normal"
) -> dict:
    """
    Distribute approved content to the team.

    Actions:
    1. Save to Google Drive (Meeting Summaries folder)
    2. Update Google Sheets (Task Tracker with new tasks)
    3. Send Telegram notification to group
    4. Send email to all founders

    Args:
        meeting_id: UUID of the meeting.
        content: The approved content.
        sensitivity: 'normal' or 'sensitive' (affects distribution).

    Returns:
        Dict with distribution results.
    """
    logger.info(f"Distributing approved content: {meeting_id}")

    results = {
        "drive_saved": False,
        "sheets_updated": False,
        "stakeholders_updated": False,
        "follow_ups_added": False,
        "telegram_sent": False,
        "email_sent": False,
    }

    meeting_title = content.get("title", "Untitled")
    summary = content.get("summary", "")
    exec_summary = content.get("executive_summary", "") or summary.split("\n")[0][:200] if summary else ""
    meeting_date = content.get("date", datetime.now().strftime("%Y-%m-%d"))
    tasks = content.get("tasks", [])
    follow_ups = content.get("follow_ups", [])
    open_questions = content.get("open_questions", [])

    # Get stakeholders from the meeting record in Supabase
    # (stored during extraction in transcript_processor)
    meeting_record = supabase_client.get_meeting(meeting_id)
    stakeholders = []
    if meeting_record and meeting_record.get("summary"):
        # Extract stakeholders section from the summary if available
        # They were stored in the extraction but may only be in the summary text
        pass

    # 1. Save to Google Drive
    try:
        filename = f"{meeting_date} - {meeting_title}.md"
        drive_result = await drive_service.save_meeting_summary(
            content=summary,
            filename=filename
        )
        results["drive_saved"] = bool(drive_result.get("id"))
        results["drive_link"] = drive_result.get("webViewLink", "")
        logger.info(f"Saved to Drive: {drive_result.get('id')}")
    except Exception as e:
        logger.error(f"Error saving to Drive: {e}")

    # 1b. Generate and save Word document
    try:
        from services.word_generator import generate_summary_docx
        docx_bytes = generate_summary_docx(
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            participants=meeting_record.get("participants", []) if meeting_record else [],
            duration_minutes=meeting_record.get("duration_minutes", 0) if meeting_record else 0,
            sensitivity=sensitivity,
            decisions=content.get("decisions", []),
            tasks=tasks,
            follow_ups=follow_ups,
            open_questions=open_questions,
            discussion_summary=content.get("discussion_summary", "") or content.get("summary", ""),
            stakeholders_mentioned=content.get("stakeholders", []),
        )
        docx_result = await drive_service.save_meeting_summary_docx(
            data=docx_bytes,
            filename=f"{meeting_date} - {meeting_title}.docx",
        )
        results["docx_link"] = docx_result.get("webViewLink", "")
        results["docx_bytes"] = docx_bytes  # For email attachment
        logger.info(f"Saved .docx to Drive: {docx_result.get('id')}")
    except Exception as e:
        logger.error(f"Error saving Word document: {e}")

    # 2. Add tasks to Google Sheets Task Tracker
    try:
        if tasks:
            for task in tasks:
                await sheets_service.add_task(
                    task=task.get("title", ""),
                    assignee=task.get("assignee", "team"),
                    source_meeting=meeting_title,
                    deadline=task.get("deadline"),
                    status=task.get("status", "pending"),
                    priority=task.get("priority", "M"),
                    created_date=meeting_date,
                    category=task.get("category", ""),
                )
            results["sheets_updated"] = True
            results["tasks_added"] = len(tasks)
            logger.info(f"Added {len(tasks)} tasks to tracker")
    except Exception as e:
        logger.error(f"Error adding tasks to Sheets: {e}")
        from services.alerting import send_system_alert, AlertSeverity
        await send_system_alert(
            AlertSeverity.CRITICAL, "google_sheets",
            f"Failed to add tasks to Sheets for '{meeting_title}': {e}", error=e,
        )

    # 2b. DEPRECATED — Commitments merged into tasks (action items).
    # The Commitments Sheets tab is no longer written to.
    # Previously: await sheets_service.add_commitments_batch_to_sheet(...)

    # 2c. Add decisions to Decisions tab
    try:
        decisions = content.get("decisions", [])
        if decisions:
            await sheets_service.ensure_decisions_tab()
            await sheets_service.add_decisions_batch_to_sheet(
                decisions=decisions,
                source_meeting=meeting_title,
                meeting_date=meeting_date,
            )
            results["decisions_added"] = len(decisions)
            logger.info(f"Added {len(decisions)} decisions to Decisions tab")
    except Exception as e:
        logger.error(f"Error adding decisions to Sheets: {e}")

    # 3. Add follow-up meetings to Task Tracker as action items
    try:
        if follow_ups:
            fu_result = await sheets_service.add_follow_ups_as_tasks(
                follow_ups=follow_ups,
                source_meeting=meeting_title,
                created_date=meeting_date,
            )
            results["follow_ups_added"] = fu_result
            results["follow_ups_count"] = len(follow_ups)
            logger.info(f"Added {len(follow_ups)} follow-up meetings to tracker")
    except Exception as e:
        logger.error(f"Error adding follow-ups to Sheets: {e}")

    # 4. Add new stakeholders to Stakeholder Tracker
    try:
        # Get stakeholders from the content if available
        stakeholders = content.get("stakeholders", [])
        if stakeholders:
            sh_result = await sheets_service.add_stakeholders_batch(
                stakeholders=stakeholders,
                source_meeting=meeting_title,
            )
            results["stakeholders_updated"] = sh_result
            results["stakeholders_added"] = len(stakeholders)
            logger.info(f"Processed {len(stakeholders)} stakeholders")
    except Exception as e:
        logger.error(f"Error adding stakeholders to Sheets: {e}")

    # 5. Get distribution list based on sensitivity
    team_emails = settings.team_emails
    distribution_emails = get_distribution_list(sensitivity, team_emails)

    # 6. Send Telegram notification
    try:
        drive_link = results.get("drive_link", "")
        participants = meeting_record.get("participants", []) if meeting_record else []

        if sensitivity == "sensitive":
            # Sensitive: send full summary to Eyal only
            telegram_result = await telegram_bot.send_meeting_summary(
                title=meeting_title,
                summary=summary,
                drive_link=drive_link,
                sensitive=True,
            )
        else:
            # Normal: send teaser to group (or Eyal in dev)
            from services.telegram_bot import format_summary_teaser

            teaser = format_summary_teaser(
                title=meeting_title,
                date=meeting_date,
                participants=participants,
                content=content,
                drive_link=drive_link,
            )
            if settings.ENVIRONMENT != "production":
                telegram_result = await telegram_bot.send_to_eyal(teaser)
            else:
                telegram_result = await telegram_bot.send_to_group(teaser)

        results["telegram_sent"] = telegram_result
        logger.info(f"Telegram notification sent: {telegram_result}")
    except Exception as e:
        logger.error(f"Error sending Telegram: {e}")

    # 7. Send email
    try:
        if distribution_emails:
            email_result = await gmail_service.send_meeting_summary(
                recipients=distribution_emails,
                meeting_title=meeting_title,
                summary_content=summary,
                drive_link=results.get("drive_link", ""),
                meeting_date=meeting_date,
                executive_summary=exec_summary,
                tasks=tasks,
                docx_bytes=results.get("docx_bytes"),
                discussion_summary=content.get("discussion_summary", ""),
            )
            results["email_sent"] = email_result
            results["emails_to"] = distribution_emails
            logger.info(f"Email sent to {len(distribution_emails)} recipients")
    except Exception as e:
        logger.error(f"Error sending email: {e}")

    # 8. Apply cross-reference changes (v0.3)
    # If the approved content included cross-reference results, apply them now
    cross_ref = content.get("cross_reference")
    if cross_ref:
        results["cross_reference_applied"] = await _apply_cross_reference_changes(
            meeting_id=meeting_id,
            cross_ref=cross_ref,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
        )

    # 9. Update meeting approval status
    supabase_client.update_meeting(
        meeting_id,
        approval_status="approved",
        approved_at=datetime.now().isoformat(),
    )

    # 10. Log in audit_log
    supabase_client.log_action(
        action="content_distributed",
        details={
            "meeting_id": meeting_id,
            "sensitivity": sensitivity,
            "results": results,
        },
        triggered_by="eyal",
    )

    logger.info(f"Distribution complete for {meeting_id}: {results}")
    return results


async def _apply_cross_reference_changes(
    meeting_id: str,
    cross_ref: dict,
    meeting_title: str,
    meeting_date: str,
) -> dict:
    """
    Apply approved cross-reference changes to Supabase and Sheets.

    Called when Eyal approves a meeting summary that includes cross-reference
    results. Updates task statuses, resolves open questions.

    Args:
        meeting_id: UUID of the meeting.
        cross_ref: Cross-reference results dict.
        meeting_title: Title of the meeting (for Sheets updates).
        meeting_date: Date of the meeting.

    Returns:
        Dict summarizing what was applied.
    """
    applied = {
        "status_changes_applied": 0,
        "questions_resolved": 0,
    }

    # Apply task status changes
    status_changes = cross_ref.get("status_changes", [])
    for change in status_changes:
        task_id = change.get("task_id")
        new_status = change.get("new_status")
        if task_id and new_status:
            try:
                supabase_client.update_task(task_id, status=new_status)
                applied["status_changes_applied"] += 1
                logger.info(
                    f"Applied status change: task {task_id} -> {new_status}"
                )
            except Exception as e:
                logger.error(f"Error applying status change for task {task_id}: {e}")

    # Apply task status changes from dedup updates
    dedup = cross_ref.get("dedup", {})
    for update in dedup.get("updates", []):
        task_id = update.get("existing_task_id")
        new_status = update.get("new_status")
        if task_id and new_status:
            try:
                supabase_client.update_task(task_id, status=new_status)
                applied["status_changes_applied"] += 1
            except Exception as e:
                logger.error(f"Error applying dedup update for task {task_id}: {e}")

    # Resolve open questions
    resolved_qs = cross_ref.get("resolved_questions", [])
    for rq in resolved_qs:
        question_id = rq.get("question_id")
        if question_id:
            try:
                supabase_client.resolve_question(
                    question_id=question_id,
                    resolved_in_meeting_id=meeting_id,
                )
                applied["questions_resolved"] += 1
                logger.info(f"Resolved question {question_id}")
            except Exception as e:
                logger.error(f"Error resolving question {question_id}: {e}")

    logger.info(
        f"Cross-reference applied: {applied['status_changes_applied']} status changes, "
        f"{applied['questions_resolved']} questions resolved"
    )
    return applied


async def distribute_approved_prep(
    meeting_id: str,
    content: dict,
) -> dict:
    """
    Distribute an approved meeting prep document to the team.

    Sensitivity-aware:
    - Sensitive meetings: Eyal-only + Drive, with note about manual forwarding.
    - Normal meetings: email to participants + Telegram group + Drive.

    Generates .docx and uploads to Drive alongside the Markdown version.

    Args:
        meeting_id: Identifier for this prep approval.
        content: The prep content dict from the scheduler.

    Returns:
        Dict with distribution results.
    """
    logger.info(f"Distributing approved meeting prep: {meeting_id}")

    results = {
        "telegram_sent": False,
        "email_sent": False,
        "drive_uploaded": False,
        "docx_uploaded": False,
        "type": "meeting_prep",
    }

    title = content.get("title", "Untitled Meeting")
    sensitivity = content.get("sensitivity", "normal")
    start_time = content.get("start_time", "TBD")
    meeting_type = content.get("meeting_type", "generic")
    focus_instructions = content.get("focus_instructions", [])
    prep_document = content.get("summary", "")
    date_str = start_time[:10] if start_time and len(start_time) >= 10 else "TBD"
    drive_link = ""

    # Upload prep document to Google Drive as Google Doc (viewable on mobile)
    try:
        from services.google_drive import drive_service
        filename = f"{date_str} - Prep - {title}"

        drive_result = await drive_service.save_meeting_prep(
            content=prep_document,
            filename=filename,
        )
        if drive_result:
            drive_link = drive_result.get("webViewLink", "")
            results["drive_uploaded"] = True
            results["drive_link"] = drive_link

    except Exception as e:
        logger.warning(f"Failed to upload prep Google Doc: {e}")

    # Generate and upload .docx version
    try:
        from services.word_generator import generate_prep_docx

        attendees = content.get("attendees", [])
        participants = [
            a.get("displayName") or a.get("email", "").split("@")[0]
            for a in attendees
        ] if attendees else []

        sections = content.get("sections", [])

        docx_bytes = generate_prep_docx(
            title=title,
            date=date_str,
            meeting_type=meeting_type,
            participants=participants,
            sections=sections,
            focus_areas=focus_instructions if focus_instructions else None,
        )

        from services.google_drive import drive_service
        docx_filename = f"{date_str} - Prep - {title}.docx"
        docx_result = await drive_service.upload_file(
            content=docx_bytes,
            filename=docx_filename,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        if docx_result:
            results["docx_uploaded"] = True
            results["docx_link"] = docx_result.get("webViewLink", "")

    except Exception as e:
        logger.warning(f"Failed to generate/upload .docx for prep: {e}")

    # Build notification message
    message = (
        f"<b>Meeting Prep Ready</b>\n\n"
        f"Meeting: {title}\n"
        f"Time: {start_time}\n"
    )
    if drive_link:
        message += f'<a href="{drive_link}">View prep document</a>\n'

    try:
        if sensitivity == "sensitive":
            message += (
                "\n<i>This prep contains sensitive content — not distributed to "
                "other participants. Forward manually if appropriate.</i>"
            )
            await telegram_bot.send_to_eyal(message, parse_mode="HTML")
            results["telegram_sent"] = True
            results["distribution"] = "eyal_only"
            logger.info(f"Sensitive prep — Eyal-only distribution for {title}")
        elif settings.ENVIRONMENT != "production":
            # Non-production: Eyal only
            await telegram_bot.send_to_eyal(message, parse_mode="HTML")
            results["telegram_sent"] = True
            results["distribution"] = "eyal_only"
        else:
            # Normal: full team distribution
            await telegram_bot.send_to_eyal(message, parse_mode="HTML")
            await telegram_bot.send_to_group(message, parse_mode="HTML")
            results["telegram_sent"] = True
            results["distribution"] = "team"

            # Send email to participants
            try:
                attendee_emails = [
                    a.get("email") for a in content.get("attendees", [])
                    if a.get("email")
                ]
                if attendee_emails and drive_link:
                    email_body = (
                        f"Meeting prep document for '{title}' ({start_time}) "
                        f"is ready: {drive_link}"
                    )
                    await gmail_service.send_email(
                        to=attendee_emails,
                        subject=f"Meeting Prep: {title}",
                        body=email_body,
                    )
                    results["email_sent"] = True
            except Exception as e:
                logger.warning(f"Failed to send prep email: {e}")

    except Exception as e:
        logger.error(f"Error sending prep notification: {e}")

    supabase_client.log_action(
        action="meeting_prep_distributed",
        details={
            "meeting_id": meeting_id,
            "title": title,
            "sensitivity": sensitivity,
            "distribution": results.get("distribution", "unknown"),
            "drive_uploaded": results["drive_uploaded"],
            "docx_uploaded": results["docx_uploaded"],
        },
        triggered_by="eyal",
    )

    return results


async def distribute_approved_digest(
    meeting_id: str,
    content: dict,
) -> dict:
    """
    Distribute an approved weekly digest to the team.

    Sends via email to all founders and posts summary to Telegram group.

    Args:
        meeting_id: Identifier for this digest approval.
        content: The digest content dict from the scheduler.

    Returns:
        Dict with distribution results.
    """
    logger.info(f"Distributing approved weekly digest: {meeting_id}")

    results = {
        "email_sent": False,
        "telegram_sent": False,
        "type": "weekly_digest",
    }

    week_of = content.get("week_of", "")
    digest_doc = content.get("digest_document", "")
    drive_link = content.get("drive_link", "")

    # Send email — Eyal-only in development mode
    try:
        if settings.ENVIRONMENT != "production":
            digest_emails = [settings.EYAL_EMAIL] if settings.EYAL_EMAIL else []
        else:
            digest_emails = settings.team_emails
        if digest_emails:
            await gmail_service.send_weekly_digest(
                recipients=digest_emails,
                week_of=week_of,
                digest_content=digest_doc,
                drive_link=drive_link,
            )
            results["email_sent"] = True
            results["emails_to"] = digest_emails
    except Exception as e:
        logger.error(f"Error sending digest email: {e}")

    # Send Telegram notification — Eyal-only in development mode
    try:
        summary_msg = (
            f"<b>CropSight Weekly Digest — Week of {week_of}</b>\n\n"
            f"Meetings: {content.get('meetings_count', 0)}\n"
            f"Decisions: {content.get('decisions_count', 0)}\n"
            f"Tasks completed: {content.get('tasks_completed', 0)}\n"
            f"Tasks overdue: {content.get('tasks_overdue', 0)}\n"
        )
        if drive_link:
            summary_msg += f"\nFull digest: {drive_link}"
        if settings.ENVIRONMENT != "production":
            await telegram_bot.send_to_eyal(summary_msg)
        else:
            await telegram_bot.send_to_group(summary_msg)
        results["telegram_sent"] = True
    except Exception as e:
        logger.error(f"Error sending digest Telegram: {e}")

    supabase_client.log_action(
        action="weekly_digest_distributed",
        details={
            "week_of": week_of,
            "meetings_count": content.get("meetings_count", 0),
        },
        triggered_by="eyal",
    )

    return results


async def update_approval_status(
    meeting_id: str,
    status: ApprovalStatus,
    approved_by: str = "eyal"
) -> None:
    """
    Update the approval status of a meeting.

    Args:
        meeting_id: UUID of the meeting.
        status: New approval status.
        approved_by: Who approved/rejected.
    """
    updates = {"approval_status": status.value}

    if status == ApprovalStatus.APPROVED:
        updates["approved_at"] = datetime.now().isoformat()

    supabase_client.update_meeting(meeting_id, **updates)

    supabase_client.log_action(
        action=f"approval_status_{status.value}",
        details={
            "meeting_id": meeting_id,
            "approved_by": approved_by,
        },
        triggered_by=approved_by,
    )

    logger.info(f"Updated meeting {meeting_id} status to {status.value}")


def format_approval_request_telegram(
    meeting_title: str,
    summary_preview: str,
    tasks_count: int,
    decisions_count: int
) -> str:
    """
    Format an approval request for Telegram.

    Creates a concise preview with action buttons.

    Args:
        meeting_title: Title of the meeting.
        summary_preview: Brief preview of the summary.
        tasks_count: Number of tasks extracted.
        decisions_count: Number of decisions extracted.

    Returns:
        Formatted Telegram message.
    """
    return f"""*New Summary for Approval*

*Meeting:* {meeting_title}
*Decisions:* {decisions_count}
*Tasks:* {tasks_count}

{summary_preview}

---
Reply with:
- "Approve" to distribute to team
- Specific edits to request changes
- "Reject" to discard"""


def format_approval_request_email(
    meeting_title: str,
    meeting_date: str,
    full_summary: str,
    drive_draft_link: str | None = None
) -> str:
    """
    Format an approval request for email.

    Includes full summary and optional Drive link.

    Args:
        meeting_title: Title of the meeting.
        meeting_date: Date of the meeting.
        full_summary: Complete summary content.
        drive_draft_link: Link to draft in Google Drive.

    Returns:
        Formatted email body (HTML).
    """
    draft_section = ""
    if drive_draft_link:
        draft_section = f"""
<p><a href="{drive_draft_link}">View Draft in Google Drive</a></p>
"""

    return f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
        .header {{ background-color: #4a5568; color: white; padding: 20px; }}
        .content {{ padding: 20px; }}
        .summary {{ background-color: #f7fafc; padding: 15px; border-radius: 5px; }}
        .actions {{ margin-top: 20px; padding: 15px; background-color: #edf2f7; }}
        pre {{ white-space: pre-wrap; font-family: inherit; }}
    </style>
</head>
<body>
    <div class="header">
        <h2>Approval Request: {meeting_title}</h2>
        <p>Date: {meeting_date}</p>
    </div>

    <div class="content">
        <p>A new meeting summary is ready for your review.</p>

        <div class="summary">
            <pre>{full_summary}</pre>
        </div>

        {draft_section}

        <div class="actions">
            <h3>Actions</h3>
            <p>Reply to this email with:</p>
            <ul>
                <li><strong>APPROVE</strong> - to distribute to the team</li>
                <li><strong>Your edits</strong> - describe the changes needed</li>
                <li><strong>REJECT</strong> - to discard this summary</li>
            </ul>
        </div>
    </div>

    <p style="color: gray; font-size: 12px;">
        Generated by Gianluigi, CropSight's AI Operations Assistant
    </p>
</body>
</html>
"""


async def handle_approval_callback(
    meeting_id: str,
    action: str
) -> dict:
    """
    Handle approval callback from Telegram inline buttons.

    Called when Eyal clicks Approve/Reject buttons.

    Args:
        meeting_id: UUID of the meeting.
        action: 'approve' or 'reject' from callback data.

    Returns:
        Result dict.
    """
    if action == "approve":
        return await process_response(
            meeting_id=meeting_id,
            response="approve",
            response_source="telegram"
        )
    elif action == "reject":
        return await process_response(
            meeting_id=meeting_id,
            response="reject",
            response_source="telegram"
        )
    else:
        return {"error": f"Unknown action: {action}"}


async def submit_stakeholder_updates_for_approval(
    stakeholder_name: str,
    organization: str,
    updates: dict,
    source_meeting_id: str | None = None,
) -> dict:
    """
    Submit a stakeholder update suggestion for Eyal's approval.

    Checks if the stakeholder already exists, then sends a suggestion
    to Eyal with approve/reject buttons.

    Args:
        stakeholder_name: Name of the contact person.
        organization: Organization name.
        updates: Dict of field names to new values.
        source_meeting_id: Meeting where this was mentioned (optional).

    Returns:
        Dict with approval_id, status, details.
    """
    # Check if stakeholder already exists
    existing = await sheets_service.get_stakeholder_info(name=stakeholder_name)
    is_new = len(existing) == 0

    action = "add" if is_new else "update"

    # Send approval request via Telegram
    result = await telegram_bot.send_stakeholder_approval_request(
        stakeholder_name=stakeholder_name,
        organization=organization,
        updates=updates,
        is_new=is_new,
        source_meeting_id=source_meeting_id,
    )

    # Log the action
    supabase_client.log_action(
        action="stakeholder_update_requested",
        details={
            "stakeholder_name": stakeholder_name,
            "organization": organization,
            "action": action,
            "source_meeting_id": source_meeting_id,
        },
        triggered_by="auto",
    )

    return {
        "approval_id": f"stakeholder:{organization}",
        "status": "pending",
        "action": action,
        "is_new": is_new,
        "telegram_sent": result,
    }


# =============================================================================
# Phase 4: Morning Brief Approval
# =============================================================================

async def _apply_morning_brief_approval(content: dict) -> dict:
    """
    Apply a morning brief approval — inject extracted items into the system.

    Marks all included email_scans as approved, then injects tasks,
    decisions, commitments from extracted items (reuses debrief injection pattern).
    Creates RAG embeddings with source_type='email'.

    Args:
        content: Morning brief content dict with 'brief' and 'scan_ids'.

    Returns:
        Summary dict with counts.
    """
    from processors.debrief import _inject_debrief_items
    from datetime import date as date_type

    scan_ids = content.get("scan_ids", [])
    brief = content.get("brief", {})
    sections = brief.get("sections", [])

    # Mark all email_scans as approved
    for scan_id in scan_ids:
        try:
            supabase_client.update_email_scan(scan_id, approved=True)
        except Exception as e:
            logger.warning(f"Failed to mark email scan {scan_id} as approved: {e}")

    # Collect all extracted items from all sections
    all_items = []
    for section in sections:
        section_type = section.get("type", "")
        if section_type in ("email_scan", "constant_layer"):
            for item in section.get("items", []):
                # Map email extraction types to debrief injection types
                item_type = item.get("type", "information")
                mapped = {
                    "task": "task",
                    "decision": "decision",
                    "commitment": "commitment",
                    "gantt_relevant": "gantt_update",
                    "deadline_change": "information",
                    "stakeholder_mention": "information",
                    "information": "information",
                }
                debrief_item = {
                    "type": mapped.get(item_type, "information"),
                    "title": item.get("text", ""),
                    "description": item.get("text", ""),
                    "assignee": item.get("assignee"),
                    "speaker": item.get("speaker"),
                    "sensitive": item.get("_sensitive", False),
                }
                if item_type == "commitment":
                    debrief_item["commitment_text"] = item.get("text", "")
                    debrief_item["speaker"] = item.get("speaker", "Unknown")
                all_items.append(debrief_item)

    if not all_items:
        logger.info("Morning brief approved but no items to inject")
        supabase_client.log_action(
            action="morning_brief_approved",
            details={"items": 0, "scan_ids_count": len(scan_ids)},
            triggered_by="eyal",
        )
        return {"summary": "No items to inject.", "counts": {}}

    # Inject using the shared debrief pipeline
    source_date = date_type.today().isoformat()
    result = await _inject_debrief_items(
        session_id=None,
        items=all_items,
        source_date=source_date,
    )

    supabase_client.log_action(
        action="morning_brief_approved",
        details={
            "items": len(all_items),
            "scan_ids_count": len(scan_ids),
            "counts": result.get("counts", {}),
        },
        triggered_by="eyal",
    )

    return result


async def distribute_approved_review(
    session_id: str,
    agenda_data: dict,
    week_number: int,
    year: int,
) -> dict:
    """
    Distribute an approved weekly review to the team.

    Post-approval pipeline (steps 3-8 from plan — Gantt execution and backup
    are handled by confirm_review() before calling this):
    3. Upload PPTX to Drive (GANTT_SLIDES_FOLDER_ID)
    4. Upload digest to Drive (WEEKLY_DIGESTS_FOLDER_ID)
    5. Update weekly_reports with Drive IDs + status='distributed'
    6. Email to team (sensitivity-aware)
    7. Telegram group notification
    8. Log audit trail

    Args:
        session_id: Weekly review session ID.
        agenda_data: Compiled weekly review data.
        week_number: ISO week number.
        year: Year.

    Returns:
        Dict with distribution results.
    """
    logger.info(f"Distributing approved weekly review: W{week_number}/{year}")

    results = {
        "pptx_uploaded": False,
        "digest_uploaded": False,
        "email_sent": False,
        "telegram_sent": False,
        "type": "weekly_review",
    }

    # 3. Upload PPTX to Drive
    try:
        from processors.gantt_slide import generate_gantt_slide
        pptx_bytes = await generate_gantt_slide(week_number, year)

        if pptx_bytes and settings.GANTT_SLIDES_FOLDER_ID:
            filename = f"CropSight_Gantt_W{week_number}_{year}.pptx"
            pptx_result = await drive_service._upload_bytes_file(
                folder_id=settings.GANTT_SLIDES_FOLDER_ID,
                filename=filename,
                content=pptx_bytes,
                mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
            results["pptx_uploaded"] = True
            results["pptx_drive_id"] = pptx_result.get("id", "")
            logger.info(f"PPTX uploaded to Drive: {results['pptx_drive_id']}")
    except Exception as e:
        logger.error(f"PPTX upload failed: {e}")

    # 4. Upload digest to Drive
    digest_link = ""
    try:
        week_in_review = agenda_data.get("week_in_review", {})
        meetings_count = week_in_review.get("meetings_count", 0)
        decisions_count = week_in_review.get("decisions_count", 0)

        digest_content = (
            f"CropSight Weekly Review — Week {week_number}, {year}\n"
            f"{'=' * 50}\n\n"
            f"Meetings: {meetings_count}\n"
            f"Decisions: {decisions_count}\n\n"
        )

        # Add decisions
        decisions = week_in_review.get("decisions", [])
        if decisions:
            digest_content += "Key Decisions:\n"
            for d in decisions[:20]:
                digest_content += f"  - {d.get('description', '')}\n"
            digest_content += "\n"

        # Add task summary
        task_summary = week_in_review.get("task_summary", {})
        completed = task_summary.get("completed_this_week", [])
        overdue = task_summary.get("overdue", [])
        if completed:
            digest_content += f"Tasks Completed ({len(completed)}):\n"
            for t in completed[:20]:
                digest_content += f"  - {t.get('title', '')}\n"
            digest_content += "\n"
        if overdue:
            digest_content += f"Tasks Overdue ({len(overdue)}):\n"
            for t in overdue[:20]:
                digest_content += f"  - {t.get('title', '')}\n"
            digest_content += "\n"

        digest_content += f"\nGenerated by Gianluigi on {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        if settings.WEEKLY_DIGESTS_FOLDER_ID:
            week_of = datetime.now().strftime("%Y-%m-%d")
            drive_result = await drive_service.save_weekly_digest(
                week_of=week_of,
                digest_content=digest_content,
            )
            digest_link = drive_result.get("link", "")
            results["digest_uploaded"] = True
            results["digest_drive_id"] = drive_result.get("id", "")
            logger.info(f"Digest uploaded to Drive: {results['digest_drive_id']}")
    except Exception as e:
        logger.error(f"Digest upload failed: {e}")

    # 5. Update weekly_reports with distribution status
    try:
        session = supabase_client.get_weekly_review_session(session_id)
        report_id = session.get("report_id") if session else None
        if report_id:
            supabase_client.update_weekly_report(
                report_id,
                status="distributed",
                distributed_at=datetime.now().isoformat(),
            )
    except Exception as e:
        logger.error(f"Report status update failed: {e}")

    # 6. Email to team (sensitivity-aware)
    try:
        if settings.ENVIRONMENT != "production":
            review_emails = [settings.EYAL_EMAIL] if settings.EYAL_EMAIL else []
        else:
            review_emails = settings.team_emails

        if review_emails:
            week_in_review = agenda_data.get("week_in_review", {})
            subject = f"CropSight Weekly Review — W{week_number}/{year}"
            body = (
                f"Weekly review for Week {week_number}, {year} has been approved.\n\n"
                f"Meetings: {week_in_review.get('meetings_count', 0)}\n"
                f"Decisions: {week_in_review.get('decisions_count', 0)}\n"
            )
            if digest_link:
                body += f"\nFull digest: {digest_link}"

            await gmail_service.send_email(
                to=review_emails,
                subject=subject,
                body=body,
            )
            results["email_sent"] = True
            results["emails_to"] = review_emails
    except Exception as e:
        logger.error(f"Review email failed: {e}")

    # 7. Telegram group notification
    try:
        week_in_review = agenda_data.get("week_in_review", {})
        summary_msg = (
            f"<b>CropSight Weekly Review — W{week_number}/{year}</b>\n\n"
            f"Meetings: {week_in_review.get('meetings_count', 0)}\n"
            f"Decisions: {week_in_review.get('decisions_count', 0)}\n"
        )

        # Add report link if available
        try:
            session = supabase_client.get_weekly_review_session(session_id)
            report_id = session.get("report_id") if session else None
            if report_id:
                report = supabase_client.get_weekly_report(week_number, year)
                if report and report.get("access_token"):
                    base_url = settings.REPORTS_BASE_URL.rstrip("/") if settings.REPORTS_BASE_URL else ""
                    if base_url:
                        summary_msg += f"\nReport: {base_url}/reports/weekly/{report['access_token']}"
        except Exception as e:
            logger.warning(f"Failed to append report URL: {e}")

        if digest_link:
            summary_msg += f"\nDigest: {digest_link}"

        if settings.ENVIRONMENT != "production":
            await telegram_bot.send_to_eyal(summary_msg)
        else:
            await telegram_bot.send_to_group(summary_msg)
        results["telegram_sent"] = True
    except Exception as e:
        logger.error(f"Review Telegram notification failed: {e}")

    # 8. Audit trail
    supabase_client.log_action(
        action="weekly_review_distributed",
        details={
            "session_id": session_id,
            "week_number": week_number,
            "year": year,
            "pptx_uploaded": results["pptx_uploaded"],
            "digest_uploaded": results["digest_uploaded"],
            "email_sent": results["email_sent"],
            "telegram_sent": results["telegram_sent"],
        },
        triggered_by="eyal",
    )

    return results
