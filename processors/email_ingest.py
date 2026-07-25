"""Email ingestion — a thread CC'd/BCC'd to Gianluigi becomes an input source.

An email thread is ONE evolving "source": a `meetings` row with
meeting_type='email_thread', fed into the SAME extraction -> approval -> DB/Sheet
pipeline a transcript uses. Only the intake is email-specific.

Design (all edge cases fall out of two facts — the email_threads row's
`meeting_id` + `processed_message_ids`):
  * FIRST message of a thread  -> process_transcript creates the source.
  * LATER messages (delta)     -> extract only the NEW content, dedup against the
    source's existing children, append, and raise a fresh card showing everything
    still pending for the thread.
  * IDEMPOTENT on Message-ID   -> re-delivery / re-CC of a seen message is a no-op.
  * GAP (dropped from CC then re-added) -> the newest message's References names
    prior messages we never received; we backfill them from its quoted history.
  * BCC / out-of-order -> we pull the whole in-mailbox thread via get_thread and
    process every unprocessed message in order; a BCC'd message simply lands in
    the inbox with us absent from the visible headers — the same path handles it.
  * NOTHING ACTIONABLE -> indexed to email_scans (searchable), no card, no ping.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_MSGID_RE = re.compile(r"<[^>]+>")


def _addresses(*fields: str) -> list[str]:
    seen: list[str] = []
    for f in fields:
        for a in _EMAIL_RE.findall(f or ""):
            al = a.lower()
            if al not in seen:
                seen.append(al)
    return seen


def _participants(msg: dict) -> list[str]:
    people: list[str] = []
    for field in (msg.get("from", ""), msg.get("to", ""), msg.get("cc", "")):
        for chunk in (field or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            name = re.sub(r"<[^>]+>", "", chunk).strip().strip('"').strip()
            m = _EMAIL_RE.search(chunk)
            label = name or (m.group(0) if m else "")
            if label and label not in people:
                people.append(label)
    return people


def _parse_references(value: str) -> list[str]:
    """The <Message-ID> tokens of a References / In-Reply-To header, in order."""
    return _MSGID_RE.findall(value or "")


def _source_text(msg: dict, clean_body: str) -> str:
    return (
        f"Email thread: {msg.get('subject', '(no subject)')}\n"
        f"From: {msg.get('from', '')}\n"
        f"To: {msg.get('to', '')}\n"
        f"Cc: {msg.get('cc', '')}\n"
        f"Date: {msg.get('date', '')}\n\n"
        f"{clean_body}"
    )


def _pending(rows: list | None) -> list:
    """Children still awaiting approval (default status 'pending')."""
    return [r for r in (rows or []) if (r.get("approval_status") or "pending") == "pending"]


async def _extract_and_store_delta(meeting_id: str, delta_text: str, subject: str) -> dict:
    """Extract from the NEW content of a growing thread and append it to the
    existing source, deduped against what the source already holds. Reuses the
    transcript sub-functions but NOT process_transcript itself (which would
    overwrite the source's summary as a side effect)."""
    from processors.transcript_processor import (
        _extract_with_readback, _normalize_task_urgency_area, store_meeting_data,
    )
    from processors.cross_reference import run_cross_reference
    from guardrails.edit_reconcile import dedup_within, normalize
    from services.supabase_client import supabase_client
    from config.settings import settings

    sensitivity = (supabase_client.get_meeting(meeting_id) or {}).get("sensitivity") or "founders"

    extracted = await _extract_with_readback(
        transcript=delta_text, meeting_title=subject, participants=[],
        meeting_date=datetime.now().strftime("%Y-%m-%d"), duration_minutes=0,
        sensitivity=sensitivity,
    )

    # Dedup TASKS + DECISIONS against the source's existing rows (a re-stated ask
    # in a later reply must not create a second task).
    cross = await run_cross_reference(
        meeting_id=meeting_id, transcript=delta_text,
        new_tasks=extracted.get("tasks", []), new_decisions=extracted.get("decisions", []),
    )
    tasks_to_store = cross.get("dedup", {}).get("new_tasks", extracted.get("tasks", []))
    try:
        _normalize_task_urgency_area(tasks_to_store, supabase_client.get_areas() or [])
    except Exception:
        pass

    ct = settings.EDIT_RECONCILE_CHAR_THRESHOLD
    tt = settings.EDIT_RECONCILE_TOKEN_THRESHOLD
    decisions_to_store = dedup_within(
        extracted.get("decisions", []), lambda d: d.get("description", ""),
        char_threshold=ct, token_threshold=tt)
    tasks_to_store = dedup_within(
        tasks_to_store, lambda t: t.get("title", ""),
        secondary_of=lambda t: t.get("assignee", ""), char_threshold=ct, token_threshold=tt)

    # Follow-ups + questions have no cross-reference deduper — guard them by title
    # against what this source already holds.
    existing_fu = {normalize(f.get("title", "")) for f in
                   supabase_client.list_follow_up_meetings(source_meeting_id=meeting_id, include_pending=True, limit=200)}
    existing_q = {normalize(q.get("question", "")) for q in
                  supabase_client.get_open_questions(meeting_id=meeting_id, include_pending=True)}
    follow_ups = [f for f in extracted.get("follow_ups", []) if normalize(f.get("title", "")) not in existing_fu]
    questions = [q for q in extracted.get("open_questions", []) if normalize(q.get("question", "")) not in existing_q]

    await store_meeting_data(
        meeting_id=meeting_id, decisions=decisions_to_store, tasks=tasks_to_store,
        follow_ups=follow_ups, open_questions=questions, sensitivity=sensitivity)

    try:
        from processors.topic_threading import link_meeting_to_topics
        await link_meeting_to_topics(meeting_id=meeting_id, decisions=decisions_to_store, tasks=tasks_to_store)
    except Exception as e:
        logger.warning(f"[email-ingest] delta topic link failed (non-fatal): {e}")

    return {"tasks": tasks_to_store, "decisions": decisions_to_store,
            "follow_ups": follow_ups, "open_questions": questions}


async def ingest_email_thread(msg: dict) -> dict:
    """Ingest one inbound email into the pipeline. `msg` is a gmail_service
    get_message dict. Never raises — runs in the watcher hot path."""
    from services.supabase_client import supabase_client
    from services.gmail import gmail_service, strip_quoted_text, extract_quoted_text

    thread_key = (msg.get("threadId") or "").strip()
    message_id = (msg.get("message_id") or "").strip()
    subject = (msg.get("subject") or "(no subject)").strip()
    if not thread_key or not message_id:
        return {"skipped": "missing thread/message id"}

    try:
        thread = supabase_client.get_email_thread(thread_key)
        processed = set((thread or {}).get("processed_message_ids") or [])
        if message_id in processed:
            return {"skipped": "already_processed", "thread_key": thread_key}

        # Whole in-mailbox thread → process EVERY unprocessed message in order
        # (grown thread + out-of-order + BCC all collapse to "process the delta").
        thread_msgs = await gmail_service.get_thread(thread_key) or [msg]
        unprocessed = [m for m in thread_msgs
                       if (m.get("message_id") or "").strip()
                       and (m.get("message_id") or "").strip() not in processed]
        seen = {(m.get("message_id") or "").strip() for m in unprocessed}
        if message_id not in seen and message_id not in processed:
            unprocessed.append(msg)               # get_thread lagged — include the trigger
        if not unprocessed:
            return {"skipped": "nothing_new", "thread_key": thread_key}

        # GAP: the newest message's References names all prior thread IDs; any we
        # never received (dropped from the CC) is recovered from its quoted history.
        newest = unprocessed[-1]
        known = processed | {(m.get("message_id") or "").strip() for m in thread_msgs}
        gap = [r for r in _parse_references(newest.get("references", "")) if r and r not in known]

        parts = []
        for m in unprocessed:
            clean = strip_quoted_text(m.get("body", "")) or (m.get("body", "") or "").strip()
            if clean:
                parts.append(f"From {m.get('from', '')} ({m.get('date', '')}):\n{clean}")
        if gap:
            quoted = extract_quoted_text(newest.get("body", ""))
            if quoted:
                parts.append(
                    f"[Earlier messages Gianluigi was not on the thread for, "
                    f"recovered from quoted history]:\n{quoted}")
        delta_text = "\n\n---\n\n".join(parts).strip()
        if not delta_text:
            return {"skipped": "empty_body", "thread_key": thread_key}

        if thread and thread.get("meeting_id"):
            meeting_id = thread["meeting_id"]
            await _extract_and_store_delta(meeting_id, delta_text, subject)
            first = False
        else:
            from processors.transcript_processor import process_transcript
            result = await process_transcript(
                file_content=_source_text(msg, delta_text), meeting_title=subject,
                meeting_date=datetime.now().strftime("%Y-%m-%d"),
                participants=_participants(msg), source_file_path=f"email:{thread_key}")
            meeting_id = result.get("meeting_id")
            if not meeting_id:
                return {"error": "no meeting_id from process_transcript", "thread_key": thread_key}
            supabase_client.client.table("meetings").update(
                {"meeting_type": "email_thread"}).eq("id", meeting_id).execute()
            supabase_client.upsert_email_thread(thread_key, meeting_id=meeting_id, subject=subject)
            first = True

        # Mark everything consumed as processed (incl. gap ids, so a later message
        # that also quotes them doesn't re-backfill).
        for m in unprocessed:
            mid = (m.get("message_id") or "").strip()
            if mid:
                supabase_client.mark_message_processed(thread_key, mid)
        for r in gap:
            supabase_client.mark_message_processed(thread_key, r)

        # The card always reflects EVERYTHING still pending for the thread — so a
        # delta that accumulates onto an un-approved batch shows the full set, and
        # re-submitting simply refreshes it.
        p_tasks = _pending(supabase_client.get_tasks(status=None, include_pending=True, meeting_id=meeting_id, limit=500))
        p_dec = _pending(supabase_client.list_decisions(meeting_id=meeting_id, include_pending=True))
        p_fu = _pending(supabase_client.list_follow_up_meetings(source_meeting_id=meeting_id, include_pending=True, limit=200))
        p_q = _pending(supabase_client.get_open_questions(meeting_id=meeting_id, include_pending=True))
        counts = {"tasks": len(p_tasks), "decisions": len(p_dec),
                  "follow_ups": len(p_fu), "open_questions": len(p_q)}

        if any(counts.values()):
            gap_note = (f" (Note: {len(gap)} earlier message(s) recovered from quoted "
                        f"history — the thread may be incomplete.)" if gap else "")
            content = {
                "title": subject, "meeting_id": meeting_id,
                "summary": f"From email thread “{subject}”.{gap_note}",
                "tasks": p_tasks, "decisions": p_dec, "follow_ups": p_fu, "open_questions": p_q,
                "source": "email", "is_delta": not first, "gap": len(gap),
            }
            from guardrails.approval_flow import submit_for_approval
            await submit_for_approval("email_thread", content, meeting_id)
            logger.info(f"[email-ingest] '{subject[:40]}' {'delta' if not first else 'new'} → card {counts} gap={len(gap)}")
            return {"ingested": True, "meeting_id": meeting_id, "items": counts,
                    "delta": not first, "gap": len(gap)}

        # Nothing actionable → index to the brain (searchable), no card.
        try:
            supabase_client.create_email_scan(
                scan_type="ingest_no_action", email_id=message_id,
                date=datetime.now().strftime("%Y-%m-%d"), sender=msg.get("from"),
                subject=subject, classification="informational", thread_id=thread_key,
                approved=True, body_text=delta_text[:50000])
        except Exception as e:
            logger.warning(f"[email-ingest] email_scan index failed (non-fatal): {e}")
        logger.info(f"[email-ingest] '{subject[:40]}' no actionable items — indexed, no card")
        return {"ingested": True, "meeting_id": meeting_id, "items": counts, "no_actionable": True}

    except Exception as e:
        logger.error(f"[email-ingest] failed for thread {thread_key}: {e}")
        return {"error": str(e), "thread_key": thread_key}
