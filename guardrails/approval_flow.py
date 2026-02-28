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
from datetime import datetime
from enum import Enum
from typing import Any

from anthropic import Anthropic

from config.settings import settings
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

# Track content type and data for pending approvals (in-memory)
# {approval_id: {"type": "meeting_summary"|"meeting_prep"|"weekly_digest", "content": {...}}}
_pending_approvals: dict[str, dict] = {}


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

    # Store content type and data for dispatch after approval
    _pending_approvals[meeting_id] = {
        "type": content_type,
        "content": content,
    }

    # Extract key info for formatting
    meeting_title = content.get("title", "Untitled Meeting")
    summary = content.get("summary", "")
    decisions = content.get("decisions", [])
    tasks = content.get("tasks", [])
    follow_ups = content.get("follow_ups", [])
    open_questions = content.get("open_questions", [])
    discussion_summary = content.get("discussion_summary", "")
    meeting_date = content.get("date", datetime.now().strftime("%Y-%m-%d"))

    if content_type == "meeting_prep":
        # Meeting prep — send preview to Eyal for approval
        drive_link = content.get("drive_link", "")
        sensitivity = content.get("sensitivity", "normal")
        preview = (
            f"<b>Meeting Prep — Awaiting Approval</b>\n\n"
            f"Meeting: {meeting_title}\n"
            f"Time: {content.get('start_time', 'TBD')}\n"
            f"Sensitivity: {sensitivity}\n"
        )
        if drive_link:
            preview += f"Prep document: {drive_link}\n"
        preview += f"\n{summary[:500]}" if summary else ""

        telegram_sent = await telegram_bot.send_approval_request(
            meeting_title=f"Prep: {meeting_title}",
            summary_preview=preview,
            meeting_id=meeting_id,
        )
        email_sent = await gmail_service.send_approval_request(
            meeting_title=f"Meeting Prep: {meeting_title}",
            summary_preview=summary[:1000] if summary else "See attached prep document.",
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
        )

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
        )

        email_sent = await gmail_service.send_approval_request(
            meeting_title=meeting_title,
            summary_preview=summary,
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
    response_source: str = "telegram"
) -> dict:
    """
    Process Eyal's response to an approval request.

    Args:
        meeting_id: UUID of the meeting/content.
        response: Eyal's response text.
        response_source: Where the response came from ('telegram' or 'email').

    Returns:
        Dict with:
        - action: 'approved', 'rejected', or 'edit_requested'
        - edits: List of changes to make (if edit_requested)
        - next_step: What happens next
    """
    logger.info(f"Processing approval response for {meeting_id}: {response[:50]}...")

    # Parse the response
    parsed = parse_approval_response(response)
    action = parsed["action"]

    # Detect content type from meeting_id prefix or in-memory pending approvals.
    # Non-meeting content (digests, preps) uses IDs like "digest-2026-02-23"
    # which aren't UUIDs, so we can't query the meetings table.
    pending_info = _pending_approvals.get(meeting_id)
    is_non_meeting = (
        meeting_id.startswith("digest-")
        or meeting_id.startswith("prep-")
        or (pending_info and pending_info["type"] in ("weekly_digest", "meeting_prep"))
    )

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

    # Cancel any pending auto-publish timer (Eyal is acting manually)
    cancel_auto_publish(meeting_id)

    if action == "approve":
        # Pop pending info (already fetched above via .get(), now remove it)
        pending_info = _pending_approvals.pop(meeting_id, None)

        # Determine content type from pending_info or ID prefix
        if pending_info:
            content_type = pending_info["type"]
        elif meeting_id.startswith("digest-"):
            content_type = "weekly_digest"
        elif meeting_id.startswith("prep-"):
            content_type = "meeting_prep"
        else:
            content_type = "meeting_summary"

        # Update approval status in DB (skip for non-meeting content like digests)
        if content_type == "meeting_summary":
            await update_approval_status(meeting_id, ApprovalStatus.APPROVED)

        if content_type == "meeting_prep":
            if not pending_info:
                # Content was submitted from another process (lost on restart)
                logger.warning(f"Meeting prep {meeting_id} approved but content not in memory (already saved to Drive)")
                supabase_client.log_action(action="approval_status_approved", details={"meeting_id": meeting_id}, triggered_by="eyal")
                return {"action": "approved", "edits": None, "next_step": "Approved (content already saved to Drive)"}
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

        # Default: meeting_summary — gather all related data
        content = {
            "title": meeting.get("title"),
            "summary": meeting.get("summary"),
            "date": meeting.get("date"),
        }

        # Get related data from Supabase
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

        # Remove from pending approvals if still there
        _pending_approvals.pop(meeting_id, None)

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

        # Parse edit instructions
        edits = await parse_edit_instructions_with_claude(response, meeting)

        if edits:
            # Apply edits
            updated_content = await apply_edits(meeting_id, edits)

            # Resubmit for approval
            await submit_for_approval(
                content_type="meeting_summary",
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
    rejection_signals = [
        "reject", "rejected", "no", "discard", "delete",
        "don't send", "cancel", "stop"
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
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)

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
        response_msg = client.messages.create(
            model=settings.model_simple,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response_msg.content[0].text

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
    edits: list[dict]
) -> dict:
    """
    Apply parsed edits to a pending summary.

    Args:
        meeting_id: UUID of the meeting.
        edits: List of edit instructions.

    Returns:
        Updated content dict.
    """
    # Get current meeting data
    meeting = supabase_client.get_meeting(meeting_id)
    if not meeting:
        logger.error(f"Meeting not found: {meeting_id}")
        return {}

    current_summary = meeting.get("summary", "")

    # Use Claude to apply the edits to the summary
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    edits_description = json.dumps(edits, indent=2)

    prompt = f"""Apply the following edits to this meeting summary.

CURRENT SUMMARY:
{current_summary}

EDITS TO APPLY:
{edits_description}

Return the complete updated summary with all edits applied.
Maintain the same markdown format and structure.
Only make the changes specified - don't add or remove anything else."""

    try:
        response = client.messages.create(
            model=settings.model_background,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        updated_summary = response.content[0].text

        # Update the meeting in database
        supabase_client.update_meeting(
            meeting_id,
            summary=updated_summary,
            approval_status="pending",  # Reset to pending for re-review
        )

        logger.info(f"Applied {len(edits)} edits to meeting {meeting_id}")

        return {
            "title": meeting.get("title"),
            "summary": updated_summary,
            "date": meeting.get("date"),
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
        telegram_result = await telegram_bot.send_meeting_summary(
            title=meeting_title,
            summary=summary[:500] + "..." if len(summary) > 500 else summary,
            drive_link=drive_link,
            sensitive=(sensitivity == "sensitive"),
        )
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

    Sensitivity-aware: sensitive meetings only notify Eyal.

    Args:
        meeting_id: Identifier for this prep approval.
        content: The prep content dict from the scheduler.

    Returns:
        Dict with distribution results.
    """
    logger.info(f"Distributing approved meeting prep: {meeting_id}")

    results = {
        "telegram_sent": False,
        "type": "meeting_prep",
    }

    title = content.get("title", "Untitled Meeting")
    sensitivity = content.get("sensitivity", "normal")
    drive_link = content.get("drive_link", "")
    start_time = content.get("start_time", "TBD")

    message = (
        f"<b>Meeting Prep Ready</b>\n\n"
        f"Meeting: {title}\n"
        f"Time: {start_time}\n"
    )
    if drive_link:
        message += f"Prep document: {drive_link}"

    try:
        if sensitivity == "sensitive" or settings.ENVIRONMENT != "production":
            logger.info(f"Eyal-only distribution (sensitive={sensitivity}, env={settings.ENVIRONMENT})")
            await telegram_bot.send_to_eyal(message)
        else:
            await telegram_bot.send_to_eyal(message)
            await telegram_bot.send_to_group(message)
        results["telegram_sent"] = True
    except Exception as e:
        logger.error(f"Error sending prep notification: {e}")

    supabase_client.log_action(
        action="meeting_prep_distributed",
        details={
            "meeting_id": meeting_id,
            "title": title,
            "sensitivity": sensitivity,
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
