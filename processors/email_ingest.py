"""Email ingestion — a thread CC'd/BCC'd to Gianluigi becomes an input source.

An email thread is treated as ONE evolving "source": a `meetings` row with
meeting_type='email_thread', fed into the SAME extraction -> approval -> DB/Sheet
pipeline a transcript uses. Nothing about the downstream (approval card, promote,
sheet write, QA) is email-specific — only the intake.

Phase 1 (this file, now): the FIRST message of a thread runs through
`process_transcript`, is tagged as an email source, and — if it yields any
tasks/decisions/follow-ups/questions — raises an approval card (content_type
'email_thread' -> file-only on approval). Idempotent on Message-ID.

Phase 2/3 (marked TODO): delta extraction for a growing thread, and gap recovery
(References + quoted history) when Gianluigi is dropped from the CC and re-added.
The `email_threads` table (thread_key -> source meeting_id + processed Message-IDs)
already carries the state those phases need.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _addresses(*fields: str) -> list[str]:
    """Bare email addresses found across the given header strings, de-duped."""
    seen: list[str] = []
    for f in fields:
        for a in _EMAIL_RE.findall(f or ""):
            al = a.lower()
            if al not in seen:
                seen.append(al)
    return seen


def _participants(msg: dict) -> list[str]:
    """Human-ish participant labels from From/To/Cc — display names when present,
    else the address. Feeds the extractor's attribution (who owns what)."""
    people: list[str] = []
    for field in (msg.get("from", ""), msg.get("to", ""), msg.get("cc", "")):
        for chunk in (field or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            name = re.sub(r"<[^>]+>", "", chunk).strip().strip('"').strip()
            label = name or (_EMAIL_RE.search(chunk).group(0) if _EMAIL_RE.search(chunk) else "")
            if label and label not in people:
                people.append(label)
    return people


def _source_text(msg: dict, clean_body: str) -> str:
    """The text handed to the extractor: a light header for context + the new
    prose (quoted history already stripped)."""
    return (
        f"Email thread: {msg.get('subject', '(no subject)')}\n"
        f"From: {msg.get('from', '')}\n"
        f"To: {msg.get('to', '')}\n"
        f"Cc: {msg.get('cc', '')}\n"
        f"Date: {msg.get('date', '')}\n\n"
        f"{clean_body}"
    )


async def ingest_email_thread(msg: dict) -> dict:
    """Ingest one inbound email into the pipeline. `msg` is a gmail_service
    get_message dict (threadId, message_id, from/to/cc, subject, body, ...).

    Returns a small status dict. Never raises — this runs in the watcher hot
    path and must not break email processing.
    """
    from services.supabase_client import supabase_client
    from services.gmail import strip_quoted_text

    thread_key = (msg.get("threadId") or "").strip()
    message_id = (msg.get("message_id") or "").strip()
    subject = (msg.get("subject") or "(no subject)").strip()
    body = msg.get("body") or ""

    if not thread_key or not message_id:
        return {"skipped": "missing thread/message id"}

    try:
        thread = supabase_client.get_email_thread(thread_key)

        # Idempotency — a Message-ID we've already extracted from is a no-op
        # (re-CC, webhook retry, IMAP resync). [Phase 2 hardens the delta path]
        if thread and message_id in (thread.get("processed_message_ids") or []):
            return {"skipped": "already_processed", "thread_key": thread_key}

        # EXISTING thread -> the DELTA append is Phase 2. Do NOT create a second
        # source, and do NOT mark processed (so Phase 2 picks this message up).
        if thread and thread.get("meeting_id"):
            logger.info(f"[email-ingest] delta on existing thread {thread_key} — deferred to Phase 2")
            return {"skipped": "delta_phase2", "thread_key": thread_key,
                    "meeting_id": thread.get("meeting_id")}

        clean = strip_quoted_text(body) or body.strip()
        if not clean:
            return {"skipped": "empty_body", "thread_key": thread_key}

        # NEW thread -> the first message runs the full transcript pipeline
        # (extraction + cross-reference dedup + entity/topic linking). It does
        # NOT distribute; distribution is decided on approval (file-only).
        from processors.transcript_processor import process_transcript
        result = await process_transcript(
            file_content=_source_text(msg, clean),
            meeting_title=subject,
            meeting_date=datetime.now().strftime("%Y-%m-%d"),
            participants=_participants(msg),
            source_file_path=f"email:{thread_key}",
        )
        meeting_id = result.get("meeting_id")
        if not meeting_id:
            return {"error": "no meeting_id from process_transcript", "thread_key": thread_key}

        # Tag the source + record the thread state (backbone for delta/gap phases)
        supabase_client.client.table("meetings").update(
            {"meeting_type": "email_thread"}
        ).eq("id", meeting_id).execute()
        supabase_client.upsert_email_thread(thread_key, meeting_id=meeting_id, subject=subject)
        supabase_client.mark_message_processed(thread_key, message_id)

        counts = {k: len(result.get(k, []) or []) for k in
                  ("tasks", "decisions", "follow_ups", "open_questions")}
        has_items = any(counts.values())

        if has_items:
            content = {
                "title": subject,
                "meeting_id": meeting_id,
                "summary": result.get("summary", ""),
                "tasks": result.get("tasks", []),
                "decisions": result.get("decisions", []),
                "follow_ups": result.get("follow_ups", []),
                "open_questions": result.get("open_questions", []),
                "source": "email",
            }
            from guardrails.approval_flow import submit_for_approval
            await submit_for_approval("email_thread", content, meeting_id)
            logger.info(f"[email-ingest] '{subject[:40]}' -> card ({counts})")
            return {"ingested": True, "meeting_id": meeting_id, "items": counts}

        # Nothing actionable. Phase 2 replaces this with a classify-first gate
        # that indexes to email_scans WITHOUT creating an empty source; for now
        # we keep the source but raise no card.
        logger.info(f"[email-ingest] '{subject[:40]}' had no actionable items — no card")
        return {"ingested": True, "meeting_id": meeting_id, "items": counts, "no_actionable": True}

    except Exception as e:
        logger.error(f"[email-ingest] failed for thread {thread_key}: {e}")
        return {"error": str(e), "thread_key": thread_key}
