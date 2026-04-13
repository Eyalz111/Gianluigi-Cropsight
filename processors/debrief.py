"""
End-of-day debrief and quick injection processor.

Two interaction patterns:
1. Quick injection: Single message → extract → confirm → inject (stateless)
2. Full debrief: /debrief → interactive session → finalize → approve → inject

Both share the same injection pipeline (_inject_debrief_items).
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone

from config.settings import settings
from core.debrief_prompt import (
    get_debrief_system_prompt,
    get_quick_injection_prompt,
    get_debrief_extraction_prompt,
)
from core.llm import call_llm
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


# =========================================================================
# Quick Injection Flow
# =========================================================================

async def process_quick_injection(
    user_message: str,
    user_id: str,
) -> dict:
    """
    Process a quick information injection (single message, no session).

    Calls Sonnet with the quick injection prompt to extract items
    from a single user message. Returns extracted items for confirmation.

    Args:
        user_message: The user's message with information to inject.
        user_id: User identifier.

    Returns:
        Dict with:
        - response: Text response for the user
        - extracted_items: List of extracted items
        - action: "quick_injection_confirm" (signals Telegram to show buttons)
    """
    system_prompt = get_quick_injection_prompt()

    try:
        text, _usage = call_llm(
            prompt=user_message,
            model=settings.model_agent,
            max_tokens=2048,
            system=system_prompt,
            call_site="quick_injection",
        )

        parsed = _parse_llm_json(text)
        items = parsed.get("extracted_items", [])
        response_text = parsed.get("response_text", "Got it.")

        if not items:
            return {
                "response": "I didn't find any tasks, decisions, or information to capture from that message.",
                "extracted_items": [],
                "action": "none",
            }

        return {
            "response": response_text,
            "extracted_items": items,
            "action": "quick_injection_confirm",
        }

    except Exception as e:
        logger.error(f"Quick injection failed: {e}")
        return {
            "response": "Sorry, I had trouble extracting information from that. Could you rephrase?",
            "extracted_items": [],
            "action": "none",
        }


# =========================================================================
# Full Debrief Flow
# =========================================================================

async def start_debrief(
    user_id: str,
    trigger: str = "explicit",
) -> dict:
    """
    Start a new debrief session or resume an existing one.

    Checks for an existing active session:
    - Same date → resume it
    - Different date → cancel stale, create new

    Fetches today's calendar events and checks which have transcripts.

    Args:
        user_id: User identifier.
        trigger: How the debrief was triggered ("explicit", "scheduled").

    Returns:
        Dict with:
        - response: Greeting message with calendar context
        - session_id: The debrief session ID
        - action: "debrief_started"
    """
    today = date.today()
    today_str = today.isoformat()

    # Check for existing active session
    existing = supabase_client.get_active_debrief_session()
    if existing:
        existing_date = existing.get("date", "")
        if existing_date == today_str:
            # Resume same-day session
            items_count = len(existing.get("items_captured", []))
            remaining = existing.get("calendar_events_remaining", [])
            response = f"Picking up where we left off — {items_count} items so far."
            if remaining:
                response += f"\n\nStill haven't touched on: {', '.join(remaining)}"
            response += "\n\nWhat else?"
            return {
                "response": response,
                "session_id": existing["id"],
                "action": "debrief_resumed",
            }
        else:
            # Cancel stale session
            supabase_client.update_debrief_session(
                existing["id"], status="cancelled"
            )
            logger.info(f"Cancelled stale debrief session {existing['id']} from {existing_date}")

    # Fetch today's calendar events
    calendar_events = []
    uncovered_events = []
    try:
        from services.google_calendar import calendar_service
        from guardrails.calendar_filter import is_cropsight_meeting
        events = await calendar_service.get_todays_events()
        # Filter to CropSight events
        cropsight_events = [e for e in events if is_cropsight_meeting(e) is not False]

        # Build event descriptions with participants
        for e in cropsight_events:
            title = e.get("title", "Untitled")
            # Extract participant names from attendees + title
            attendees = e.get("attendees", []) or []
            participant_names = [
                a.get("displayName") or a.get("email", "").split("@")[0]
                for a in attendees if a.get("email")
            ]
            if participant_names:
                event_desc = f"{title} (with {', '.join(participant_names)})"
            else:
                event_desc = title

            calendar_events.append(event_desc)
            has_transcript = _check_transcript_exists(title, today)
            if not has_transcript:
                uncovered_events.append(event_desc)
    except Exception as e:
        logger.warning(f"Could not fetch calendar events for debrief: {e}")

    # Create new session
    session = supabase_client.create_debrief_session(today_str)
    session_id = session["id"]

    # Store calendar context
    if calendar_events or uncovered_events:
        supabase_client.update_debrief_session(
            session_id,
            calendar_events_covered=[e for e in calendar_events if e not in uncovered_events],
            calendar_events_remaining=uncovered_events,
        )

    # Check for pending prep outlines
    pending_preps_note = ""
    try:
        pending_preps = supabase_client.get_pending_prep_outlines()
        if pending_preps:
            titles = []
            for pp in pending_preps:
                content = pp.get("content", {})
                event = content.get("outline", {}).get("event", content.get("event", {}))
                titles.append(event.get("title", "Unknown"))
            if titles:
                pending_preps_note = (
                    f"\n\nYou have {len(titles)} pending prep outline(s): "
                    f"{', '.join(titles)}. Handle those first?"
                )
    except Exception as e:
        logger.debug(f"Pending prep check in debrief failed: {e}")

    # Build greeting
    greeting = "Ready for your end-of-day wrap-up."
    if uncovered_events:
        greeting += f"\n\nI see these meetings today without transcripts:\n"
        for event in uncovered_events:
            greeting += f"- {event}\n"
        greeting += "\nAnything to capture from these or other conversations today?"
    elif calendar_events:
        greeting += " All your CropSight meetings today have transcripts."
        greeting += "\n\nAnything else to capture from today — calls, messages, decisions?"
    else:
        greeting += "\n\nNo CropSight meetings on the calendar today."
        greeting += " What happened today that should be captured?"

    if pending_preps_note:
        greeting += pending_preps_note

    return {
        "response": greeting,
        "session_id": session_id,
        "action": "debrief_started",
    }


async def process_debrief_message(
    session_id: str,
    user_message: str,
    user_id: str,
) -> dict:
    """
    Process a message during an active debrief session.

    Loads session state, calls Sonnet with debrief prompt + context,
    accumulates extracted items, and returns the response.

    Args:
        session_id: Debrief session UUID.
        user_message: The user's message.
        user_id: User identifier.

    Returns:
        Dict with:
        - response: Text response with follow-up question
        - session_id: Session UUID
        - items_count: Total items captured so far
        - show_finish_button: True (Telegram should show "Finish debrief")
        - action: "debrief_message"
    """
    session = supabase_client.get_debrief_session(session_id)
    if not session:
        return {
            "response": "Debrief session not found. Start a new one with /debrief.",
            "action": "error",
        }

    # Check TTL
    created_at = session.get("created_at", "")
    if _is_session_expired(created_at):
        supabase_client.update_debrief_session(session_id, status="cancelled")
        return {
            "response": "This debrief session has expired. Start a new one with /debrief.",
            "action": "session_expired",
        }

    # Load state
    raw_messages = session.get("raw_messages", []) or []
    items_captured = session.get("items_captured", []) or []
    calendar_remaining = session.get("calendar_events_remaining", []) or []

    # Safety cap
    if len(items_captured) >= settings.DEBRIEF_MAX_ITEMS:
        return {
            "response": f"You've reached the maximum of {settings.DEBRIEF_MAX_ITEMS} items. Please finish the debrief to review and approve.",
            "session_id": session_id,
            "items_count": len(items_captured),
            "show_finish_button": True,
            "action": "debrief_max_items",
        }

    # Append message
    raw_messages.append({"role": "user", "text": user_message, "ts": datetime.now().isoformat()})

    # Build context for LLM
    context = _build_debrief_context(session, items_captured, calendar_remaining)
    msg_count = len(raw_messages)
    context += f"\nMESSAGE COUNT: This is message #{msg_count} in this debrief session.\n"
    full_prompt = f"{context}\n\nEyal's message:\n{user_message}"

    system_prompt = get_debrief_system_prompt()

    try:
        text, _usage = call_llm(
            prompt=full_prompt,
            model=settings.model_agent,
            max_tokens=2048,
            system=system_prompt,
            call_site="debrief_message",
        )

        parsed = _parse_llm_json(text)
        new_items = parsed.get("extracted_items", [])
        response_text = parsed.get("response_text", "Got it.")
        follow_up = parsed.get("follow_up_question")

        # Accumulate items
        items_captured.extend(new_items)

        # Update covered events if mentioned
        if calendar_remaining:
            msg_lower = user_message.lower()
            still_remaining = [
                e for e in calendar_remaining
                if e.lower() not in msg_lower
            ]
            calendar_remaining = still_remaining

        # Persist state
        supabase_client.update_debrief_session(
            session_id,
            raw_messages=raw_messages,
            items_captured=items_captured,
            calendar_events_remaining=calendar_remaining,
        )

        # Build response
        if follow_up:
            response_text += f"\n\n{follow_up}"

        return {
            "response": response_text,
            "session_id": session_id,
            "items_count": len(items_captured),
            "show_finish_button": True,
            "action": "debrief_message",
        }

    except Exception as e:
        logger.error(f"Debrief message processing failed: {e}")
        # Still save the raw message even if extraction failed
        supabase_client.update_debrief_session(
            session_id, raw_messages=raw_messages
        )
        return {
            "response": "I had trouble processing that. Could you say it again differently?",
            "session_id": session_id,
            "items_count": len(items_captured),
            "show_finish_button": True,
            "action": "debrief_message",
        }


async def finalize_debrief(session_id: str) -> dict:
    """
    Finalize a debrief — optionally run Opus validation, show summary.

    Transitions session to 'confirming' status. If items exceed
    DEBRIEF_OPUS_THRESHOLD, runs Opus validation pass.

    Args:
        session_id: Debrief session UUID.

    Returns:
        Dict with:
        - response: Extraction summary
        - session_id: Session UUID
        - items: Validated/final items list
        - action: "debrief_confirm" (signals Telegram to show Approve/Edit/Cancel)
    """
    session = supabase_client.get_debrief_session(session_id)
    if not session:
        return {"response": "Session not found.", "action": "error"}

    # Guard against double-finalize (button + text "done" simultaneously)
    status = session.get("status", "")
    if status in ("confirming", "approved", "cancelled"):
        items = session.get("items_captured", []) or []
        if status == "confirming" and items:
            summary = _format_extraction_summary(items)
            return {
                "response": summary,
                "session_id": session_id,
                "items": items,
                "action": "debrief_confirm",
            }
        return {"response": "This debrief has already been finalized.", "action": "error"}

    items = session.get("items_captured", []) or []

    if not items:
        supabase_client.update_debrief_session(session_id, status="cancelled")
        return {
            "response": "No items were captured in this debrief. Session cancelled.",
            "action": "debrief_cancelled",
        }

    # Opus validation for large debriefs
    if len(items) > settings.DEBRIEF_OPUS_THRESHOLD:
        try:
            from core.analyst_agent import analyst_agent
            raw_messages = session.get("raw_messages", []) or []
            raw_texts = [m.get("text", "") for m in raw_messages if m.get("role") == "user"]
            validated = await analyst_agent.extract_from_debrief(raw_texts, items)
            if validated:
                items = validated
                supabase_client.update_debrief_session(
                    session_id, items_captured=items
                )
        except Exception as e:
            logger.warning(f"Opus validation failed, using Sonnet items: {e}")

    # Transition to confirming
    supabase_client.update_debrief_session(session_id, status="confirming")

    summary = _format_extraction_summary(items)

    return {
        "response": summary,
        "session_id": session_id,
        "items": items,
        "action": "debrief_confirm",
    }


async def edit_debrief_items(
    session_id: str,
    edit_instruction: str,
    user_id: str,
) -> dict:
    """
    Edit debrief items based on user correction.

    Calls Sonnet with the edit instruction and current items,
    returns modified items for re-confirmation.

    Args:
        session_id: Debrief session UUID.
        edit_instruction: User's correction text.
        user_id: User identifier.

    Returns:
        Dict with:
        - response: Updated summary
        - session_id: Session UUID
        - items: Updated items list
        - action: "debrief_confirm" (re-show Approve/Edit/Cancel)
    """
    session = supabase_client.get_debrief_session(session_id)
    if not session:
        return {"response": "Session not found.", "action": "error"}

    items = session.get("items_captured", []) or []

    prompt = f"""Current extracted items:
{json.dumps(items, indent=2, default=str)}

Edit instruction from the CEO:
{edit_instruction}

Apply the edit and return the updated items list.

RESPONSE FORMAT:
You MUST respond with valid JSON only.
{{"updated_items": [...], "response_text": "Brief description of what changed"}}"""

    try:
        text, _usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,
            max_tokens=2048,
            system="You are editing a list of extracted debrief items. Apply the user's correction precisely.",
            call_site="debrief_edit",
        )

        parsed = _parse_llm_json(text)
        updated_items = parsed.get("updated_items", items)
        response_text = parsed.get("response_text", "Items updated.")

        # Persist
        supabase_client.update_debrief_session(
            session_id, items_captured=updated_items
        )

        summary = _format_extraction_summary(updated_items)
        return {
            "response": f"{response_text}\n\n{summary}",
            "session_id": session_id,
            "items": updated_items,
            "action": "debrief_confirm",
        }

    except Exception as e:
        logger.error(f"Debrief edit failed: {e}")
        return {
            "response": "Sorry, I couldn't apply that edit. Try again with different wording.",
            "session_id": session_id,
            "items": items,
            "action": "debrief_confirm",
        }


async def confirm_debrief(session_id: str, approved: bool) -> dict:
    """
    Confirm or cancel a finalized debrief.

    If approved, injects all items into the system. If rejected,
    cancels the session with no side effects.

    Args:
        session_id: Debrief session UUID.
        approved: True to inject items, False to cancel.

    Returns:
        Dict with:
        - response: Confirmation message
        - action: "debrief_approved" or "debrief_cancelled"
        - injected: Summary of what was injected (if approved)
    """
    session = supabase_client.get_debrief_session(session_id)
    if not session:
        return {"response": "Session not found.", "action": "error"}

    # Guard against double-approve/cancel (check current status)
    current_status = session.get("status", "")
    if current_status in ("approved", "cancelled"):
        return {
            "response": f"This debrief has already been {current_status}.",
            "action": f"debrief_{current_status}",
        }

    if not approved:
        supabase_client.update_debrief_session(session_id, status="cancelled")
        return {
            "response": "Cancelled — nothing saved.",
            "action": "debrief_cancelled",
        }

    # Atomic claim: update status to "approving" only if still "confirming".
    # This prevents double-injection from concurrent approve callbacks.
    claim = (
        supabase_client.client.table("debrief_sessions")
        .update({"status": "approving"})
        .eq("id", session_id)
        .eq("status", "confirming")
        .execute()
    )
    if not claim.data:
        # Another call already claimed this session
        return {
            "response": "This debrief has already been approved.",
            "action": "debrief_approved",
        }

    items = session.get("items_captured", []) or []
    source_date = session.get("date", date.today().isoformat())

    try:
        result = await _inject_debrief_items(session_id, items, source_date)
        supabase_client.update_debrief_session(session_id, status="approved")

        return {
            "response": f"All saved. {result.get('summary', '')}",
            "action": "debrief_approved",
            "injected": result,
        }
    except Exception as e:
        logger.error(f"Debrief injection failed: {e}", exc_info=True)
        return {
            "response": "Error saving debrief items. Please try again or check with the logs.",
            "action": "error",
        }


# =========================================================================
# Shared Injection Pipeline
# =========================================================================

async def _inject_debrief_items(
    session_id: str | None,
    items: list[dict],
    source_date: str,
) -> dict:
    """
    Inject debrief items into the system.

    Creates a pseudo-meeting record for FK constraints, then processes
    each item by type: tasks, decisions, gantt_updates,
    and information (embed only).

    Args:
        session_id: Debrief session UUID (None for quick injection).
        items: List of extracted item dicts.
        source_date: Date string (ISO format).

    Returns:
        Summary dict with counts of items injected.
    """
    from services.embeddings import embedding_service

    # Create pseudo-meeting for FK constraints
    title = f"Debrief: {source_date}"
    pseudo_meeting = supabase_client.create_meeting(
        date=datetime.fromisoformat(source_date) if isinstance(source_date, str) else datetime.now(),
        title=title,
        participants=["Eyal"],
        source_file_path="debrief",
        sensitivity="founders",
    )
    meeting_id = pseudo_meeting["id"]

    counts = {"tasks": 0, "decisions": 0, "gantt_proposals": 0, "information": 0}
    embed_texts = []

    for item in items:
        item_type = item.get("type", "information")

        try:
            if item_type == "task":
                # Debrief/quick-inject is CEO-authored free text — trust the input
                # and create directly. No cross-meeting dedup: Haiku false-positives
                # would silently drop the task with no fallback (data-loss bug found
                # 2026-04-11 on the 2026-04-10 U Bank / D&O / Yoram injection).
                supabase_client.create_task(
                    title=item.get("title", item.get("description", "")),
                    assignee=item.get("assignee") or "Eyal",
                    priority=item.get("priority", "M"),
                    deadline=item.get("deadline"),
                    meeting_id=meeting_id,
                    category=item.get("category"),
                )
                counts["tasks"] += 1

            elif item_type == "decision":
                supabase_client.create_decision(
                    meeting_id=meeting_id,
                    description=item.get("description", item.get("title", "")),
                    context=f"From debrief on {source_date}",
                    participants_involved=item.get("participants_involved"),
                )
                counts["decisions"] += 1

            elif item_type == "gantt_update":
                try:
                    from services.gantt_manager import gantt_manager
                    await gantt_manager.propose_gantt_update(
                        changes=[{
                            "section": item.get("section", ""),
                            "description": item.get("description", item.get("title", "")),
                            "week": item.get("week"),
                        }],
                        source="debrief",
                    )
                    counts["gantt_proposals"] += 1
                except Exception as e:
                    logger.warning(f"Gantt proposal from debrief failed: {e}")

            # All items get embedded as information
            desc = item.get("description", item.get("title", ""))
            if desc:
                embed_texts.append(f"[{item_type}] {desc}")
                if item_type == "information":
                    counts["information"] += 1

        except Exception as e:
            logger.error(f"Failed to inject debrief item: {item} — {e}")

    # Embed debrief content
    if embed_texts:
        try:
            full_text = "\n".join(embed_texts)
            embedded_chunks = await embedding_service.chunk_and_embed_document(
                document=full_text,
                document_id=meeting_id,
            )
            if embedded_chunks:
                records = [
                    {
                        "source_type": "debrief",
                        "source_id": meeting_id,
                        "chunk_text": chunk["text"],
                        "chunk_index": chunk["chunk_index"],
                        "embedding": chunk["embedding"],
                        "metadata": {
                            "meeting_date": source_date,
                            "source_type": "debrief",
                            "meeting_id": meeting_id,
                        },
                    }
                    for chunk in embedded_chunks
                ]
                supabase_client.store_embeddings_batch(records)
        except Exception as e:
            logger.error(f"Debrief embedding failed: {e}")

    # T3.1 approval gate: debrief rows inherit DB default approval_status='pending'
    # and would be invisible to the central read helpers. Debrief is already
    # CEO-confirmed via the Inject button, so promote the pseudo-meeting and its
    # children to 'approved' here (debrief bypasses the normal approval flow).
    try:
        supabase_client.client.table("meetings").update({
            "approval_status": "approved",
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", meeting_id).execute()
        for child_table in ("tasks", "decisions", "open_questions", "follow_up_meetings"):
            try:
                supabase_client.client.table(child_table).update({
                    "approval_status": "approved",
                }).eq("meeting_id", meeting_id).execute()
            except Exception as child_err:
                logger.debug(
                    f"Debrief approval promote skipped for {child_table}: {child_err}"
                )
    except Exception as promote_err:
        logger.error(
            f"Debrief approval promote failed for meeting {meeting_id}: {promote_err}",
            exc_info=True,
        )

    summary_parts = []
    if counts["tasks"]:
        summary_parts.append(f"{counts['tasks']} tasks")
    if counts["decisions"]:
        summary_parts.append(f"{counts['decisions']} decisions")
    if counts["gantt_proposals"]:
        summary_parts.append(f"{counts['gantt_proposals']} Gantt proposals")
    if counts["information"]:
        summary_parts.append(f"{counts['information']} info items")

    # v2.3 PR 3: log approval observation. Quick-inject and full-debrief are
    # both CEO-authored content explicitly confirmed by Eyal — treat them as
    # 'approved' observations. session_id distinguishes the flow: None means
    # quick_inject (single message), else full debrief session.
    try:
        supabase_client.log_approval_observation(
            content_type="quick_inject" if session_id is None else "debrief",
            action="approved",
            content_id=meeting_id,
            final_content={"counts": counts, "item_count": len(items)},
            context={
                "session_id": session_id,
                "source_date": source_date,
                "meeting_id": meeting_id,
            },
        )
    except Exception as e:
        logger.warning(f"[observation] debrief log failed (non-fatal): {e}")

    return {
        "summary": f"Injected: {', '.join(summary_parts)}." if summary_parts else "No items to inject.",
        "counts": counts,
        "meeting_id": meeting_id,
    }


# =========================================================================
# Helper Functions
# =========================================================================

def _build_debrief_context(
    session: dict,
    items_captured: list[dict],
    calendar_remaining: list[str],
) -> str:
    """
    Build context string for the debrief LLM call.

    Includes: calendar events, items captured, top 10 open tasks.
    """
    parts = []

    # Calendar context
    if calendar_remaining:
        parts.append("UN-COVERED CALENDAR EVENTS TODAY:")
        for event in calendar_remaining:
            parts.append(f"- {event}")
        parts.append("")

    # Items captured so far
    if items_captured:
        parts.append(f"ITEMS CAPTURED SO FAR ({len(items_captured)}):")
        for i, item in enumerate(items_captured[-10:], 1):  # Last 10 only
            item_type = item.get("type", "info")
            title = item.get("title", item.get("description", ""))[:60]
            parts.append(f"  {i}. [{item_type}] {title}")
        parts.append("")

    # Open tasks for context
    try:
        open_tasks = supabase_client.get_tasks(status="pending", limit=10)
        if open_tasks:
            parts.append("TOP OPEN TASKS:")
            for t in open_tasks[:10]:
                parts.append(f"  - {t.get('title', '')} ({t.get('assignee', '')})")
            parts.append("")
    except Exception:
        pass

    # Recent decisions for context (last 2 weeks)
    try:
        decisions = supabase_client.list_decisions()[:10]
        if decisions:
            parts.append("RECENT DECISIONS:")
            for d in decisions[:10]:
                desc = d.get("description", "")[:80]
                parts.append(f"  - {desc}")
            parts.append("")
    except Exception:
        pass

    # Phase 4: Queued email extractions not yet in morning brief
    try:
        today_str = date.today().isoformat()
        unapproved = supabase_client.get_unapproved_email_scans(date_from=today_str)
        email_items = []
        for scan in unapproved:
            for item in scan.get("extracted_items") or []:
                email_items.append(item)
        if email_items:
            parts.append(f"QUEUED EMAIL ITEMS ({len(email_items)} from today's emails):")
            for item in email_items[:10]:
                item_type = item.get("type", "info")
                text = item.get("text", "")[:80]
                parts.append(f"  - [{item_type}] {text}")
            parts.append("")
            parts.append("You can ask: 'I also captured items from emails today — want to review those too?'")
            parts.append("")
    except Exception:
        pass

    return "\n".join(parts) if parts else ""


def _format_extraction_summary(items: list[dict]) -> str:
    """
    Format extraction items as a natural-language summary for Telegram.

    1-4 items per type: prose sentence(s).
    5+ items per type: natural count then compact numbered list.
    Caps at 3500 chars for Telegram limits.
    """
    if not items:
        return "No items captured."

    by_type: dict[str, list[dict]] = {}
    for item in items:
        t = item.get("type", "information")
        by_type.setdefault(t, []).append(item)

    # Natural number words for small counts
    _num_word = {1: "One", 2: "Two", 3: "Three", 4: "Four"}

    def _item_desc(item: dict) -> str:
        title = item.get("title", item.get("description", ""))
        title = title[:80] if title else "Untitled"
        assignee = item.get("assignee", item.get("speaker", ""))
        sensitive = " (sensitive)" if item.get("sensitive") else ""
        if assignee:
            return f"{assignee} to {title.lower()}{sensitive}" if item.get("type") == "task" else f"{title} — {assignee}{sensitive}"
        return f"{title}{sensitive}"

    def _format_group_prose(group: list[dict], type_name: str) -> str:
        """Format 1-4 items as a prose sentence."""
        count = len(group)
        count_word = _num_word.get(count, str(count))
        noun = type_name if count > 1 else type_name.rstrip("s")
        descs = [_item_desc(item) for item in group]
        if count == 1:
            return f"{count_word} {noun}: {descs[0]}."
        items_text = ", ".join(descs[:-1]) + f", and {descs[-1]}"
        return f"{count_word} {type_name} — {items_text}."

    def _format_group_list(group: list[dict], type_name: str) -> str:
        """Format 5+ items as a count header + numbered list."""
        count = len(group)
        lines = [f"{count} {type_name}:"]
        for i, item in enumerate(group, 1):
            title = item.get("title", item.get("description", ""))
            title = title[:80] if title else "Untitled"
            assignee = item.get("assignee", item.get("speaker", ""))
            suffix = f" — {assignee}" if assignee else ""
            sensitive = " (sensitive)" if item.get("sensitive") else ""
            lines.append(f"  {i}. {title}{suffix}{sensitive}")
        return "\n".join(lines)

    type_config = [
        ("task", "tasks"),
        ("decision", "decisions"),
        ("gantt_update", "Gantt updates"),
        ("information", "notes"),
    ]

    parts = ["Here's what I got:\n"]
    for item_type, type_name in type_config:
        group = by_type.get(item_type, [])
        if not group:
            continue
        if len(group) <= 4:
            parts.append(_format_group_prose(group, type_name))
        else:
            parts.append(_format_group_list(group, type_name))

    result = "\n\n".join(parts)

    if len(result) > 3500:
        result = result[:3500] + "\n\n(...)"

    return result


def _parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM response, handling markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        # Remove code fences
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        logger.warning(f"Could not parse LLM JSON: {text[:200]}")
        return {}


def _check_transcript_exists(event_title: str, event_date: date) -> bool:
    """Check if a meeting with a similar title exists for the given date."""
    try:
        # Query meetings table for today
        result = (
            supabase_client.client.table("meetings")
            .select("id, title")
            .gte("date", event_date.isoformat())
            .lt("date", (event_date + timedelta(days=1)).isoformat())
            .neq("source_file_path", "debrief")
            .execute()
        )
        if result.data:
            title_lower = event_title.lower()
            for m in result.data:
                if title_lower in m.get("title", "").lower():
                    return True
        return False
    except Exception:
        return False


def _is_session_expired(created_at: str) -> bool:
    """Check if a debrief session has exceeded its TTL."""
    if not created_at:
        return False
    try:
        # Supabase stores timestamps in UTC
        created = datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        ).replace(tzinfo=None)
        # Compare against UTC to avoid timezone mismatch
        elapsed = datetime.utcnow() - created
        return elapsed > timedelta(minutes=settings.DEBRIEF_TTL_MINUTES)
    except (ValueError, TypeError):
        return False


def is_done_signal(message: str) -> bool:
    """
    Check if a message is a "done" signal for ending a debrief.

    Only matches short messages (<5 words) with done-like patterns.
    "Done with the Moldova call" (5+ words) → NOT a done signal.
    "Done" or "That's it" → done signal.

    Args:
        message: The user's message text.

    Returns:
        True if the message signals end of debrief.
    """
    words = message.strip().split()
    if len(words) >= 5:
        return False

    msg_lower = message.strip().lower()
    done_patterns = [
        "done", "that's it", "thats it", "finish", "finished",
        "nothing else", "that's all", "thats all", "no more",
        "all done", "i'm done", "im done", "end debrief",
    ]
    return any(msg_lower == p or msg_lower.startswith(p + ".") or msg_lower.startswith(p + "!") for p in done_patterns)
