"""
Transcript processing pipeline.

This module handles the end-to-end processing of Tactiq transcript exports:
1. Parse raw transcript (speaker labels, timestamps)
2. Send to Claude for structured extraction
3. Extract decisions, tasks, follow-ups, open questions
4. Apply tone and content guardrails
5. Store in Supabase
6. Trigger approval flow

Tactiq export format (expected):
    [00:00:15] Eyal: Welcome everyone...
    [00:01:30] Roye: Thanks, so about the MVP...

Usage:
    from processors.transcript_processor import process_transcript

    result = await process_transcript(
        file_content="...",
        meeting_title="MVP Focus",
        meeting_date="2026-02-22",
        participants=["Eyal", "Roye", "Paolo", "Yoram"]
    )
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from anthropic import APIStatusError

from config.settings import settings
from core.llm import call_llm
from core.system_prompt import (
    get_summary_extraction_prompt,
    format_summary as format_summary_template,
)
from services.supabase_client import supabase_client
from services.embeddings import embedding_service
from guardrails.sensitivity_classifier import (
    classify_sensitivity,
    classify_sensitivity_from_content,
    classify_sensitivity_llm,
    propagate_meeting_sensitivity,
)
from guardrails.content_filter import (
    filter_personal_content,
    validate_summary_tone,
)

logger = logging.getLogger(__name__)


def _normalize_task_urgency_area(tasks: list[dict], areas: list[dict]) -> None:
    """Operational floor (PR3): coerce each task's urgency to {H,M,L}, canonicalize
    the model's `category` against the live Gantt areas (2026-06 realignment —
    category IS the area taxonomy), and drop an INFERRED (guessed) deadline on an
    urgent task — the "express urgency with a fake date" trap. Mutates in place."""
    for t in tasks:
        u = str(t.get("urgency") or "").strip().upper()
        t["urgency"] = u if u in ("H", "M", "L") else "M"
        t["category"] = supabase_client.resolve_category(t.get("category"), areas=areas)
        if (t["urgency"] == "H" and t.get("deadline")
                and t.get("deadline_confidence") == "INFERRED"):
            logger.info(
                f"no-invented-dates backstop: dropped INFERRED deadline "
                f"{t.get('deadline')} on urgent task '{t.get('title', '')[:40]}'"
            )
            t["deadline"] = None
            t["deadline_confidence"] = "NONE"


async def process_transcript(
    file_content: str,
    meeting_title: str,
    meeting_date: str,
    participants: list[str],
    source_file_path: str | None = None
) -> dict:
    """
    Process a raw transcript through the full pipeline.

    Args:
        file_content: Raw transcript text from Tactiq export.
        meeting_title: Title of the meeting.
        meeting_date: Date in ISO format (YYYY-MM-DD).
        participants: List of participant names.
        source_file_path: Google Drive path to the original file.

    Returns:
        Dict containing:
        - meeting_id: UUID of the created meeting
        - summary: The formatted summary
        - decisions: List of extracted decisions
        - tasks: List of extracted tasks
        - follow_ups: List of follow-up meetings
        - open_questions: List of open questions
        - sensitivity: 'normal' or 'sensitive'
        - approval_status: 'pending'
    """
    logger.info(f"Processing transcript: {meeting_title}")

    # Step 1: Parse transcript structure
    parsed = parse_transcript(file_content)
    duration_minutes = parsed["duration_minutes"]

    # Use speakers from transcript if no participants provided
    if not participants and parsed["speakers"]:
        participants = sorted(parsed["speakers"])
        logger.info(f"Auto-detected participants: {participants}")

    # Step 2: Classify sensitivity BEFORE extraction (v2.5 #b). The read-back
    # filter must gate on the FINAL tier, not the title-only tier — and these
    # passes read the transcript text, which is already available here.
    sensitivity = classify_sensitivity({"title": meeting_title})
    content_sensitivity = classify_sensitivity_from_content(file_content)
    if content_sensitivity == "ceo":
        sensitivity = "ceo"
    if sensitivity == "founders":
        llm_sensitivity = classify_sensitivity_llm(file_content)
        if llm_sensitivity == "ceo":
            sensitivity = "ceo"
            logger.info("LLM classified meeting as ceo (keywords missed)")

    # Step 3: Structured extraction, with the v2.5 knowledge read-back loop
    # (shadow-compares by default; ships the baseline until cutover).
    extracted = await _extract_with_readback(
        transcript=file_content,
        meeting_title=meeting_title,
        participants=participants,
        meeting_date=meeting_date,
        duration_minutes=duration_minutes,
        sensitivity=sensitivity,
    )

    # Step 3b: Post-extraction sensitivity safety net (v2.5 #3) — re-check the
    # EXTRACTED items; if they reveal CEO-tier content the pre-extraction passes
    # missed, upgrade the tier (which blocks downward distribution).
    if sensitivity != "ceo":
        try:
            extracted_text = " ".join(
                [d.get("description", "") for d in extracted.get("decisions", [])]
                + [t.get("title", "") for t in extracted.get("tasks", [])]
            )
            if extracted_text.strip() and classify_sensitivity_llm(extracted_text) == "ceo":
                sensitivity = "ceo"
                logger.info("Post-extraction safety net upgraded meeting to ceo")
        except Exception as e:
            logger.warning(f"Post-extraction sensitivity check failed (non-fatal): {e}")

    # Step 3c: Completeness check (v2.5 PR4) — surface items the extraction
    # missed, before cross-reference dedup absorbs them. Shadow-logs by default;
    # merges into the shipped output only after cutover.
    extracted = await _apply_completeness_check(file_content, meeting_title, extracted)

    # Step 5: Validate tone of discussion summary
    tone_issues = validate_summary_tone(extracted.get("discussion_summary", ""))
    if tone_issues:
        logger.warning(f"Tone issues detected: {len(tone_issues)} issues")
        for issue in tone_issues:
            logger.warning(f"  - {issue['type']}: {issue.get('text', '')[:50]}...")

    # Step 6: Format the summary
    summary = format_summary_template(
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        participants=participants,
        duration_minutes=duration_minutes,
        sensitivity=sensitivity,
        decisions=extracted.get("decisions", []),
        tasks=extracted.get("tasks", []),
        follow_ups=extracted.get("follow_ups", []),
        open_questions=extracted.get("open_questions", []),
        discussion_summary=extracted.get("discussion_summary", ""),
        stakeholders_mentioned=extracted.get("stakeholders", []),
    )

    # Step 7: Store in Supabase
    # Create the meeting record first
    from datetime import datetime as dt
    meeting_datetime = dt.fromisoformat(f"{meeting_date}T00:00:00")

    meeting = supabase_client.create_meeting(
        date=meeting_datetime,
        title=meeting_title,
        participants=participants,
        raw_transcript=file_content,
        summary=summary,
        sensitivity=sensitivity,
        source_file_path=source_file_path,
        duration_minutes=duration_minutes,
    )
    meeting_id = meeting["id"]

    logger.info(f"Created meeting record: {meeting_id}")

    # Step 7b: Extract task match annotations (Phase 12 A2)
    task_match_annotations = extract_task_match_annotations(extracted.get("tasks", []))
    if task_match_annotations:
        logger.info(f"Found {len(task_match_annotations)} existing_task_match annotations")

    # Step 7c: Cross-reference analysis (v0.3)
    # Run before storing tasks so we can deduplicate them
    from processors.cross_reference import run_cross_reference

    cross_ref_results = await run_cross_reference(
        meeting_id=meeting_id,
        transcript=file_content,
        new_tasks=extracted.get("tasks", []),
        # Phase 9A: pass decisions for supersession detection
        new_decisions=extracted.get("decisions", []),
        # Phase 12 A2: pass LLM-generated match annotations
        task_match_annotations=task_match_annotations,
    )

    # Use deduplicated tasks — only insert genuinely new ones
    dedup = cross_ref_results.get("dedup", {})
    tasks_to_store = dedup.get("new_tasks", extracted.get("tasks", []))

    # Deadline-confidence safety net: if Opus forgot the field but gave us a
    # concrete deadline, the DEADLINE rule in the prompt guarantees the date
    # came from a participant utterance — promote to EXPLICIT so reminders
    # fire. Mirror on null deadline → NONE. (See memory:
    # project_v2_3_post_deploy_findings.md Priority 2.5.)
    for _t in tasks_to_store:
        dc = _t.get("deadline_confidence")
        has_deadline = bool(_t.get("deadline"))
        if has_deadline and dc in (None, "", "NONE"):
            _t["deadline_confidence"] = "EXPLICIT"
            logger.info(
                f"deadline_confidence safety net: task '{_t.get('title', '')[:40]}' "
                f"had deadline={_t.get('deadline')} but confidence={dc!r} — promoted to EXPLICIT"
            )
        elif not has_deadline and dc not in ("NONE", None, ""):
            # Task has no deadline but some non-NONE confidence — normalize to NONE
            _t["deadline_confidence"] = "NONE"
        elif dc is None:
            _t["deadline_confidence"] = "NONE"

    # Coerce urgency + canonicalize category against the live Gantt areas + the
    # no-invented-dates backstop. Runs unconditionally — category must stay
    # canonical even when the urgency extraction flag is off.
    if tasks_to_store:
        try:
            _areas = supabase_client.get_areas() or []
        except Exception:
            _areas = []
        _normalize_task_urgency_area(tasks_to_store, _areas)

    # Layer 6 — collapse near-identical items WITHIN this single extraction run
    # before insert. The extraction LLM occasionally emits the same decision (or
    # task) twice, reworded, in one pass — the "single-batch" duplicates the
    # 2026-07 sweep found (cross_reference already dedups tasks against EXISTING
    # rows, but not against each other). Tasks carry the assignee guard so an
    # intentional same-title / different-owner split survives.
    from guardrails.edit_reconcile import dedup_within
    decisions_to_store = dedup_within(
        extracted.get("decisions", []), lambda d: d.get("description", ""),
        char_threshold=settings.EDIT_RECONCILE_CHAR_THRESHOLD,
        token_threshold=settings.EDIT_RECONCILE_TOKEN_THRESHOLD,
    )
    tasks_to_store = dedup_within(
        tasks_to_store, lambda t: t.get("title", ""),
        secondary_of=lambda t: t.get("assignee", ""),
        char_threshold=settings.EDIT_RECONCILE_CHAR_THRESHOLD,
        token_threshold=settings.EDIT_RECONCILE_TOKEN_THRESHOLD,
    )

    # Store extracted data (with deduplicated tasks). [audit P1-01] Pass the
    # meeting tier so children are tagged atomically at insert.
    await store_meeting_data(
        meeting_id=meeting_id,
        decisions=decisions_to_store,
        tasks=tasks_to_store,
        follow_ups=extracted.get("follow_ups", []),
        open_questions=extracted.get("open_questions", []),
        sensitivity=sensitivity,
    )

    # Step 7b2: Link decision chains for supersessions (Phase 12 A6)
    try:
        supersessions = cross_ref_results.get("supersessions", [])
        if supersessions:
            _link_decision_chains(meeting_id, supersessions)
    except Exception as e:
        logger.error(f"Decision chain linking failed (non-fatal): {e}")

    # Step 7b3: Propagate meeting sensitivity to extracted items. Belt-and-
    # suspenders now — the tier was already stamped atomically at insert above.
    # [audit P1-01] If the redundant pass fails, surface it instead of swallowing
    # (the rows are still protected by the atomic insert, but a silent failure
    # here used to be the actual leak vector).
    _prop = propagate_meeting_sensitivity(meeting_id, sensitivity)
    if _prop.get("failed_tables"):
        try:
            from services.alerting import send_system_alert, AlertSeverity
            await send_system_alert(
                AlertSeverity.WARNING,
                "transcript_processor.propagate_sensitivity",
                f"Sensitivity propagation failed for {_prop['failed_tables']} on "
                f"meeting {meeting_id} (tier={sensitivity}). Rows are still protected "
                f"by the atomic insert; investigate the DB write.",
            )
        except Exception as _alert_err:
            logger.error(f"Alert on propagate failure also failed: {_alert_err}")

    # Step 7c: Entity extraction and linking (v0.3 Tier 2)
    from processors.entity_extraction import extract_and_link_entities

    entity_results = {}
    try:
        entity_results = await extract_and_link_entities(
            meeting_id=meeting_id,
            transcript=file_content,
            participants=participants,
            pre_extracted=extracted.get("stakeholders", []),
        )
    except Exception as e:
        logger.error(f"Entity extraction failed (non-fatal): {e}")

    # Step 7d: Topic threading — link meeting to topic threads (Phase 9B)
    linked_threads = []
    try:
        from processors.topic_threading import link_meeting_to_topics

        linked_threads = await link_meeting_to_topics(
            meeting_id=meeting_id,
            decisions=extracted.get("decisions", []),
            tasks=tasks_to_store,
        )
    except Exception as e:
        logger.error(f"Topic threading failed (non-fatal): {e}")

    # Step 7d2: Summary enrichment (re-render once now that cross-reference
    # supersessions + topic linkage have run). Two flag-gated paths, non-fatal —
    # any failure leaves the baseline Step-6 summary. DB write first, then
    # reassign the local `summary` (which flows into the returned dict →
    # approval/distribution) so a failed write can't diverge DB from in-memory.
    #
    # SUMMARY_RICH_ENABLED (PR7) is the forward-facing rich summary and a
    # superset of the context render (it folds in the same supersession + topic
    # clauses), so it takes precedence when both flags are on.
    if settings.SUMMARY_RICH_ENABLED:
        try:
            from processors.summary_rich import build_rich_summary

            enriched = await build_rich_summary(
                meeting_title=meeting_title,
                meeting_date=meeting_date,
                participants=participants,
                duration_minutes=duration_minutes,
                sensitivity=sensitivity,
                extracted=extracted,
                meeting_id=meeting_id,
                supersessions=cross_ref_results.get("supersessions", []),
                linked_threads=linked_threads,
            )
            if enriched:
                supabase_client.update_meeting(meeting_id, summary=enriched)
                summary = enriched
                logger.info(f"Rich summary applied to {meeting_id}")
        except Exception as e:
            logger.warning(f"Rich summary skipped (non-fatal): {e}")
    elif settings.SUMMARY_CONTEXT_ENABLED:
        try:
            from processors.summary_context import (
                build_supersession_clauses,
                build_topic_context,
            )

            decision_context = build_supersession_clauses(
                extracted.get("decisions", []),
                cross_ref_results.get("supersessions", []),
                sensitivity,
            )
            topic_context = build_topic_context(linked_threads, sensitivity)
            if decision_context or topic_context:
                enriched = format_summary_template(
                    meeting_title=meeting_title,
                    meeting_date=meeting_date,
                    participants=participants,
                    duration_minutes=duration_minutes,
                    sensitivity=sensitivity,
                    decisions=extracted.get("decisions", []),
                    tasks=extracted.get("tasks", []),
                    follow_ups=extracted.get("follow_ups", []),
                    open_questions=extracted.get("open_questions", []),
                    discussion_summary=extracted.get("discussion_summary", ""),
                    stakeholders_mentioned=extracted.get("stakeholders", []),
                    decision_context=decision_context,
                    topic_context=topic_context,
                )
                supabase_client.update_meeting(meeting_id, summary=enriched)
                summary = enriched
                logger.info(
                    f"Summary context enrichment applied to {meeting_id}: "
                    f"{len(decision_context)} decision clause(s), "
                    f"topic_context={'yes' if topic_context else 'no'}"
                )
        except Exception as e:
            logger.warning(f"Summary context enrichment skipped (non-fatal): {e}")

    # Step 7e: Deal signal detection (Phase 4 — non-fatal)
    deal_signals = {}
    try:
        from processors.deal_intelligence import detect_deal_signals

        deal_signals = detect_deal_signals(
            transcript_text=file_content,
            meeting_title=meeting_title,
            participants=participants,
            meeting_date=meeting_date,
            meeting_id=meeting_id,
        )
        if deal_signals.get("has_deal_signals"):
            logger.info(
                f"Deal signals detected: {deal_signals['deal_keywords_found'][:5]}, "
                f"external participants: {deal_signals['external_participants']}"
            )
    except Exception as e:
        logger.error(f"Deal signal detection failed (non-fatal): {e}")

    # Step 7f: Post-meeting proactive alerts (v0.3 Tier 2)
    from processors.proactive_alerts import generate_post_meeting_alerts

    post_meeting_alerts = []
    try:
        post_meeting_alerts = generate_post_meeting_alerts(
            meeting_id=meeting_id,
            transcript=file_content,
        )
        if post_meeting_alerts:
            logger.info(f"Generated {len(post_meeting_alerts)} post-meeting alerts")
    except Exception as e:
        logger.error(f"Post-meeting alerts failed (non-fatal): {e}")

    # Step 8: Generate and store embeddings
    await generate_and_store_embeddings(meeting_id, file_content, sensitivity=sensitivity)

    # Step 9: Log the action
    supabase_client.log_action(
        action="meeting_processed",
        details={
            "meeting_id": meeting_id,
            "title": meeting_title,
            "sensitivity": sensitivity,
            "decisions_count": len(extracted.get("decisions", [])),
            "tasks_count": len(tasks_to_store),
            "duplicates_found": len(dedup.get("duplicates", [])),
            "status_changes_found": len(cross_ref_results.get("status_changes", [])),
            "questions_resolved": len(cross_ref_results.get("resolved_questions", [])),
            "entities_new": len(entity_results.get("new_entities", [])),
            "entity_mentions": entity_results.get("total_mentions", 0),
            "deal_signals_detected": deal_signals.get("has_deal_signals", False),
        },
        triggered_by="auto",
    )

    logger.info(
        f"Transcript processing complete: {meeting_id} "
        f"({len(extracted.get('decisions', []))} decisions, "
        f"{len(tasks_to_store)} new tasks, "
        f"{len(dedup.get('duplicates', []))} duplicates)"
    )

    return {
        "meeting_id": meeting_id,
        "summary": summary,
        "executive_summary": extracted.get("executive_summary", ""),
        # Return the SAME deduped lists that were written to the DB — the watcher
        # copies these straight onto the approval card + distributed summary, so
        # returning the raw extracted['decisions'] would show a duplicate the DB
        # already collapsed (card/summary vs DB disagreement).
        "decisions": decisions_to_store,
        "tasks": tasks_to_store,
        "follow_ups": extracted.get("follow_ups", []),
        "open_questions": extracted.get("open_questions", []),
        "stakeholders": extracted.get("stakeholders", []),
        "discussion_summary": extracted.get("discussion_summary", ""),
        "sensitivity": sensitivity,
        "approval_status": "pending",
        "cross_reference": cross_ref_results,
        "entity_results": entity_results,
    }


def parse_transcript(raw_content: str) -> dict:
    """
    Parse a raw Tactiq transcript into structured format.

    Extracts:
    - Speaker labels
    - Timestamps
    - Utterances

    Args:
        raw_content: Raw transcript text.

    Returns:
        Dict with:
        - utterances: List of {speaker, timestamp, text}
        - speakers: Set of unique speakers
        - duration_minutes: Estimated duration based on timestamps
    """
    # Pattern for Tactiq format — supports both:
    #   [HH:MM:SS] Speaker: text   (bracketed)
    #   MM:SS Speaker: text         (unbracketed, actual Tactiq Google Meet export)
    pattern_bracketed = r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([^:\[\]]+):\s*(.+?)(?=\n\[|\Z)'
    pattern_unbracketed = r'^(\d{1,2}:\d{2}(?::\d{2})?)\s+([^:\d][^:]+):\s*(.+?)(?=\n\d{1,2}:\d{2}|\Z)'

    # Try bracketed first
    matches = re.findall(pattern_bracketed, raw_content, re.DOTALL)
    if not matches:
        # Fall back to unbracketed Tactiq format
        matches = re.findall(pattern_unbracketed, raw_content, re.DOTALL | re.MULTILINE)

    utterances = []
    speakers = set()

    for timestamp, speaker, text in matches:
        # Normalize speaker name to Title Case (Tactiq sometimes exports lowercase)
        speaker_clean = speaker.strip().title()
        utterances.append({
            "timestamp": timestamp.strip(),
            "speaker": speaker_clean,
            "text": text.strip()
        })
        speakers.add(speaker_clean)

    # Estimate duration
    duration_minutes = estimate_duration(utterances)

    return {
        "utterances": utterances,
        "speakers": speakers,
        "duration_minutes": duration_minutes,
    }


def estimate_duration(utterances: list[dict]) -> int:
    """
    Estimate meeting duration from transcript timestamps.

    Args:
        utterances: List of parsed utterances with timestamps.

    Returns:
        Duration in minutes.
    """
    if not utterances:
        return 0

    def parse_timestamp(ts: str) -> int:
        """Convert timestamp to total seconds."""
        parts = ts.split(":")
        if len(parts) == 2:
            # MM:SS format
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            # HH:MM:SS format
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return 0

    # Get first and last timestamps
    first_ts = utterances[0].get("timestamp", "0:00")
    last_ts = utterances[-1].get("timestamp", "0:00")

    first_seconds = parse_timestamp(first_ts)
    last_seconds = parse_timestamp(last_ts)

    duration_seconds = last_seconds - first_seconds
    duration_minutes = max(1, duration_seconds // 60)

    return duration_minutes


def _consolidation_rule() -> str:
    """The action-item consolidation instruction (v2.5 PR5 muzzle toggle).

    Default keeps the "aim for 3-7 items" cap. When EXTRACTION_MUZZLE_REMOVED is
    set (post read-back cutover), drop the numeric cap and prioritize
    completeness while still merging genuinely same-deliverable sub-tasks. Flip
    only after >=10 clean shadow meetings show the completeness/dedup chain
    absorbs the extra volume.
    """
    if settings.EXTRACTION_MUZZLE_REMOVED:
        return (
            "- CONSOLIDATION: Merge ONLY sub-tasks that serve the exact same "
            "deliverable. Otherwise capture every distinct commitment — never drop "
            "or over-merge items to hit a target count. Completeness over brevity. "
            "One exception in the other direction: do NOT split a SINGLE action "
            "into near-duplicate tasks — if one person is doing one thing that "
            "happens to involve a few parts (e.g. sending someone the set of "
            "pre-meeting documents), capture it as ONE action item that names the "
            "parts, not one task per part."
        )
    return (
        "- CONSOLIDATION: Combine related sub-tasks into one higher-level action "
        "item. If multiple items serve the same deliverable, merge them. Aim for "
        "3-7 action items per meeting, not 10-15."
    )


def _decision_extraction_rules() -> str:
    """The DECISION extraction + decision-vs-action-item disambiguation block.

    Decisions were being confused with action items at extraction (Eyal, 2026-07).
    A "decision" is a resolved CHOICE/commitment; an "action item" is a doable task
    with an owner. This block makes the distinction explicit with a test + examples
    so choices route to decisions and work routes to tasks (and neither is
    double-counted). Kept as a helper (like _consolidation_rule) so it's a named,
    testable unit.
    """
    return (
        "DECISION EXTRACTION RULES:\n"
        "- WHAT A DECISION IS: a CHOICE, CONCLUSION, or COMMITMENT the team reached "
        "— a settled position on WHAT to do, believe, or pursue (a direction, "
        "approach, policy, priority, or go/no-go). It answers \"what did we DECIDE?\", "
        "not \"who will DO what\".\n"
        "- DECISION vs ACTION ITEM (the #1 confusion — get this right):\n"
        "  * A DECISION is a resolved CHOICE: \"We'll go with a subscription model\", "
        "\"We're prioritizing the Moldova pilot over Brazil\", \"We'll use AWS, not "
        "GCP\", \"We're not raising until Q3\".\n"
        "  * An ACTION ITEM is a concrete EXECUTION step with an owner: \"Eyal to set "
        "up the AWS account by Friday\", \"Roye to send the deck to Lavazza\".\n"
        "  * TEST: if it names WHO does WHAT (a doable task), it is an ACTION ITEM -> "
        "put it in \"tasks\", NOT \"decisions\". If it is a choice/conclusion/position "
        "with no single execution step, it is a DECISION.\n"
        "  * A decision often SPAWNS action items. Do NOT extract the same thing "
        "twice: capture the CHOICE as one decision, and any concrete follow-on work "
        "as separate action item(s). Example: \"We decided to outsource design and "
        "Paolo will find a contractor\" -> decision: \"Outsource design work\"; task: "
        "\"Paolo to find a design contractor\".\n"
        "- NOT decisions (never put these in \"decisions\"): routine tasks someone "
        "agreed to do; \"I'll send X\" / \"let me check Y\" promises; status updates; "
        "questions still unresolved (those are open_questions); vague intentions with "
        "no commitment (\"we should probably think about pricing\"). If it is just "
        "work to be done, it is a task; if it is unresolved, it is an open_question.\n"
        "- A real decision reflects a CHOICE among alternatives or a clear commitment "
        "to a course of action. If no genuine choice/commitment was reached, do NOT "
        "force a decision.\n"
        "- Every decision MUST include \"rationale\" (why it was decided) and "
        "\"options_considered\" (alternatives discussed).\n"
        "- \"confidence\" is a 1-5 scale: 1=tentative/exploratory, 2=leaning toward, "
        "3=agreed but flexible, 4=firm decision, 5=irreversible commitment. Use 1-2 "
        "sparingly — a \"decision\" at confidence 1 is usually really an open question "
        "or a task.\n"
        "- If rationale or options were not explicitly discussed, infer from context. "
        "If truly unclear, set rationale to \"Not explicitly discussed\" and "
        "options_considered to [].\n"
        "- \"review_date\" defaults to 30 days from the meeting date. For "
        "urgent/time-sensitive decisions, set earlier."
    )


async def extract_structured_data(
    transcript: str,
    meeting_title: str,
    participants: list[str],
    meeting_date: str,
    duration_minutes: int | None = None,
    knowledge_context: str | None = None,
) -> dict:
    """
    Use Claude to extract structured data from transcript.

    Extracts:
    - Key decisions (with context, participants, timestamp)
    - Action items (with assignee, deadline, priority, timestamp)
    - Follow-up meetings (with leader, agenda, prep needed)
    - Open questions (with who raised them)
    - New stakeholders/contacts mentioned
    - Discussion summary

    Args:
        transcript: The full transcript text.
        meeting_title: Meeting title for context.
        participants: Known participants for context.
        meeting_date: Date of the meeting.
        duration_minutes: Estimated duration.

    Returns:
        Dict with all extracted elements.
    """
    # Build team roles context from config
    from config.team import TEAM_MEMBERS

    team_lines = []
    for m in TEAM_MEMBERS.values():
        desc = m.get("role_description", m.get("role", ""))
        team_lines.append(f"- {m['name']} ({m['role']}): {desc}")
    team_roles = "\n".join(team_lines)

    # Fetch existing open tasks for context (participant-first, max 30)
    # Graceful degradation: if this fails, extraction proceeds without context
    existing_tasks = None
    try:
        participant_names_lower = {p.lower() for p in participants}
        pending = supabase_client.get_tasks(status="pending", limit=50)
        in_progress = supabase_client.get_tasks(status="in_progress", limit=30)
        all_open = pending + in_progress

        # Sort: participant tasks first, then by created_at desc
        def task_sort_key(t):
            assignee = (t.get("assignee") or "").lower()
            is_participant = 1 if any(
                name in assignee for name in participant_names_lower if name
            ) else 0
            priority_rank = {"H": 0, "M": 1, "L": 2}.get(t.get("priority", "M"), 1)
            return (-is_participant, priority_rank)

        all_open.sort(key=task_sort_key)
        existing_tasks = all_open[:30]
    except Exception as e:
        logger.warning(f"Could not fetch existing tasks for extraction context: {e}")

    # Build meeting-to-meeting continuity context (Phase 9B)
    meeting_history_context = None
    try:
        from processors.meeting_continuity import build_meeting_continuity_context
        meeting_history_context = build_meeting_continuity_context(
            participants=participants,
        )
    except Exception as e:
        logger.warning(f"Meeting continuity context failed (non-fatal): {e}")

    # Build the extraction prompt
    prompt = get_summary_extraction_prompt(
        transcript=transcript,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        participants=participants,
        duration_minutes=duration_minutes,
        team_roles=team_roles,
        existing_tasks=existing_tasks,
        meeting_history_context=meeting_history_context,
        knowledge_context=knowledge_context,
    )

    # Build canonical project names for label normalization (dynamic from DB)
    from services.supabase_client import supabase_client as _sc
    try:
        projects = _sc.get_canonical_projects(status="active")
        names_with_aliases = []
        for p in projects:
            aliases = p.get("aliases") or []
            alias_str = f" (aliases: {', '.join(aliases)})" if aliases else ""
            names_with_aliases.append(f'"{p["name"]}"{alias_str}')
        canonical_names = ", ".join(names_with_aliases) if names_with_aliases else '"No canonical projects defined"'
    except Exception:
        canonical_names = '"Moldova Pilot", "Pre-Seed Fundraising", "SatYield Accuracy Model"'

    # v2.5 PR5: the "aim for 3-7 items" cap is the extraction "muzzle" — toggled.
    consolidation_rule = _consolidation_rule()

    # Category = Gantt-area taxonomy (2026-06 realignment): the option list comes
    # from the live areas table (seeded from the Gantt board), so extraction
    # stays structurally aligned with the board. 'General' = genuine misfit.
    try:
        _area_names = [a.get("name", "") for a in (_sc.get_areas() or []) if a.get("name")]
    except Exception:
        _area_names = []
    if not _area_names:
        from models.schemas import TaskCategory
        _area_names = [c.value for c in TaskCategory if c.value != "General"]
    category_options = " / ".join(_area_names + ["General"])
    category_rules = (
        "\n- CATEGORY: assign exactly ONE of these CropSight Gantt areas: "
        + ", ".join(f'"{n}"' for n in _area_names)
        + ", or \"General\" for a genuine misfit. The category taxonomy IS the "
        "Gantt board's area list — never invent a different bucket name."
    )

    # Operational floor (PR3): urgency (separate axis from priority), flag-gated.
    # Off -> both placeholders resolve to "" so the prompt is unchanged.
    urgency_area_json = ""
    urgency_area_rules = ""
    if getattr(settings, "TASK_URGENCY_AREA_ENABLED", False):
        urgency_area_json = '\n            "urgency": "H/M/L",'
        urgency_area_rules = (
            "\n- URGENCY: H/M/L — TIME-PRESSURE, a SEPARATE axis from priority (which is "
            "IMPORTANCE). Judge how SOON it must happen: an explicit near deadline or explicit "
            "time-pressure language (\"urgent\", \"blocking\", \"today\", \"right now\") -> H; "
            "needed this week-ish -> M; no time pressure -> L (default M when unsure). "
            "CRITICAL: \"ASAP\"/\"as soon as possible\"/\"soon\" -> urgency H WITH deadline=null "
            "and deadline_confidence=NONE — never invent a date to express urgency."
        )
    urgency_area_rules += category_rules

    # Use a structured extraction approach with JSON output
    extraction_system = """You are an expert meeting analyst. Extract structured information from meeting transcripts.

IMPORTANT: Your response must be valid JSON with this exact structure:
{
    "executive_summary": "One sentence capturing the meeting's most important outcome or decision. Write for someone deciding whether to read the full summary.",
    "decisions": [
        {
            "label": "2-3 word topic label — use canonical project names when possible",
            "description": "The decision made",
            "rationale": "Why this was decided (the reasoning behind it)",
            "options_considered": ["Option A that was discussed", "Option B that was rejected"],
            "confidence": 3,
            "context": "Surrounding context",
            "participants_involved": ["Name1", "Name2"],
            "transcript_timestamp": "MM:SS"
        }
    ],
    "tasks": [
        {
            "label": "2-3 word topic label for quick scanning",
            "title": "Task description — see TASK EXTRACTION RULES below",
            "assignee": "Name",
            "deadline": "YYYY-MM-DD or null",
            "deadline_confidence": "EXPLICIT",
            "priority": "H/M/L",{urgency_area_json}
            "category": "{category_options}",
            "transcript_timestamp": "MM:SS",
            "existing_task_match": {"task_id": "uuid or null", "confidence": "high/medium/low", "evolution": "status_update/scope_change/completion/null"}
        }
    ],
    "follow_ups": [
        {
            "label": "2-3 word topic label for quick scanning",
            "title": "Meeting title",
            "proposed_date": "Description or null",
            "led_by": "Name",
            "participants": ["Name1", "Name2"],
            "agenda_items": ["Item 1", "Item 2"],
            "prep_needed": "What needs to happen before"
        }
    ],
    "open_questions": [
        {
            "label": "2-3 word topic label for quick scanning",
            "question": "The question",
            "raised_by": "Name",
            "transcript_timestamp": "MM:SS"
        }
    ],
    "stakeholders": [
        {
            "name": "Full proper name of person or organization",
            "type": "person / organization / project / location",
            "context": "One sentence: CropSight's relationship or interaction with them",
            "speaker": "Who mentioned them",
            "relationship": "advisor / investor / partner / client / grant_body / pilot_site / vendor / other"
        }
    ],
    "discussion_summary": "2-4 paragraphs — see DISCUSSION SUMMARY RULES below"
}

ACTION ITEM EXTRACTION RULES:
- An ACTION ITEM is a doable TASK someone will execute (WHO does WHAT) — this is DISTINCT from a DECISION (a choice/conclusion). If something is a settled choice with no single execution step, it belongs in "decisions", not here. See DECISION EXTRACTION RULES for the test.
- Extract ACTION ITEMS: anything a participant agreed to do, was asked to do, or volunteered to do.
- Include both formally assigned tasks ("Eyal, can you draft the abstract?") and verbal promises ("I'll send that over").
- Each action item title should answer: WHO does WHAT by WHEN and WHY.
- BAD: "Write accuracy abstract"
- GOOD: "Write 1-page accuracy abstract documenting model performance benchmarks — needed before the client meeting"
- Include the business context from the conversation, not just the bare action.
{consolidation_rule}
  Example: "set up AWS account", "configure IAM roles", "prepare budget" → "Prepare AWS infrastructure (account, IAM, budget)"
- DEADLINE: Only set a deadline if the transcript explicitly mentions a specific date, day of the week, or relative timeframe (e.g., "by Friday", "next week", "March 30"). "ASAP", "soon", "as early as possible" are NOT deadlines — set to null. Do NOT infer deadlines from context or urgency.
- DEADLINE_CONFIDENCE: EVERY task MUST have this field set to one of three literal strings:
  * "EXPLICIT" — a participant stated a specific date, day, week number, or short timeframe verbatim ("by March 15", "next Tuesday", "end of this week", "before W22", "in two weeks"). Use EXPLICIT whenever the task has a non-null deadline that came from a participant utterance.
  * "INFERRED" — no date was stated but you set a deadline anyway based on context (milestone pressure, urgency signals, imminent meeting). Rare — prefer NONE.
  * "NONE" — no deadline signal at all. This is mandatory when deadline is null, and is the correct default when you are unsure.
  Rule of thumb: if deadline is non-null, deadline_confidence is almost always EXPLICIT. If deadline is null, deadline_confidence is NONE. Never leave this field missing — downstream reminders depend on it.{urgency_area_rules}
- DEDUPLICATION: Never extract the same action as two separate items. If someone says "I'll do X" and is later formally assigned X, extract only once.
- ASSIGNEE: Only assign to a specific person if the transcript makes it clear who is responsible. If unclear, set "assignee" to "" (empty string). Do NOT use "team", "everyone", or "TBD".
- EXISTING TASK AWARENESS: If the prompt includes an EXISTING OPEN TASKS section, reference it. When the discussion clearly refers to an existing task, do NOT extract it as new. Instead, use the "existing_task_match" field to link it:
  - Set "existing_task_match.task_id" to the existing task's ID
  - Set "confidence": "high" (explicit reference), "medium" (strong implication), "low" (possible match)
  - Set "evolution": "status_update" (status changed), "scope_change" (scope modified), "completion" (task done), or null (just mentioned)
  - If the discussion reveals a status change, prefix the title with "UPDATE:" as a hint for deduplication.
  TASK EVOLUTION EXAMPLES:
  - Existing: "Write accuracy abstract" → Transcript: "I finished the abstract" → existing_task_match: {task_id: "...", confidence: "high", evolution: "completion"}
  - Existing: "Send capability deck to Lavazza" → Transcript: "I'm updating the deck with Moldova results" → existing_task_match: {task_id: "...", confidence: "high", evolution: "scope_change"}
  - Existing: "Review budget projections" → Transcript: "We discussed the budget but haven't finished" → existing_task_match: {task_id: "...", confidence: "medium", evolution: "status_update"}
  - If NO match: set "existing_task_match" to null (genuinely new task).
- PERSONAL FILTER: EXCLUDE personal academic commitments, thesis work, university courses, degree programs, or other non-CropSight activities. Only extract items directly related to CropSight business. If a team member mentions personal academic work, do NOT create a task or decision for it.

LABEL RULES:
Every decision, task, follow-up meeting, and open question MUST include a "label" field — a 2-3 word topic tag for quick scanning. Use canonical project names when possible: {canonical_names}. If a topic doesn't match any canonical name, create a short descriptive label (2-4 words). Normalize variations: "Moldova PoC", "Gagauzia project", "Moldova wheat" → "Moldova Pilot".

{decision_rules}

DISCUSSION SUMMARY RULES:
- Opening paragraph: What was the meeting's purpose and key outcome?
- Body paragraphs: Group by theme, not chronology. Connect related discussions even if they happened at different points in the meeting.
- Closing paragraph: What's the overall trajectory? Are things on track, at risk, or pivoting?
- Write for a reader who missed the meeting — they should understand both WHAT happened and WHY it matters.
- Use active voice. Be specific, not vague.
- BAD: "The team discussed the Moldova pilot and various challenges."
- GOOD: "The Moldova pilot timeline was the central tension point: delivery is on track technically, but client expectations around accuracy documentation need to be managed before the next milestone."

STAKEHOLDER EXTRACTION RULES:
A "stakeholder" is someone CropSight has a DIRECT business relationship with — someone you'd put in a CRM.
INCLUDE: specific advisors/contacts, partner companies, grant bodies, named pilot sites/projects.
EXCLUDE: big tech/infra (AWS, Google, Microsoft, IBM), countries/cities mentioned casually, tools/platforms (Zoom, Slack, Tactiq), generic terms, meeting participants, CropSight team members.

LANGUAGE HANDLING:
Meetings may be in Hebrew, English, or mixed. Regardless of language:
- Extract ALL field values in English
- Translate Hebrew titles, descriptions to English
- Person names are proper nouns — keep as-is (Eyal, Roye, Paolo, Yoram)
- Keep company/organization names as-is
- If a Hebrew term has no clear English equivalent, transliterate and add brief explanation

Apply all tone guardrails: no emotional characterizations, professional language only, cite timestamps.""".replace("{canonical_names}", canonical_names).replace("{consolidation_rule}", consolidation_rule).replace("{decision_rules}", _decision_extraction_rules()).replace("{urgency_area_json}", urgency_area_json).replace("{urgency_area_rules}", urgency_area_rules).replace("{category_options}", category_options)

    # Retry with exponential backoff for transient errors (529 overloaded, 500, etc.)
    max_retries = 4
    base_delay = 5  # seconds

    for attempt in range(max_retries):
        try:
            response_text, _ = call_llm(
                prompt=prompt,
                model=settings.model_extraction,
                max_tokens=16384,
                system=extraction_system,
                call_site="transcript_extraction",
            )

            # Try to extract JSON from the response
            extracted = _parse_extraction_response(response_text)

            logger.info(f"Extracted: {len(extracted.get('decisions', []))} decisions, "
                        f"{len(extracted.get('tasks', []))} tasks")

            return extracted

        except APIStatusError as e:
            if e.status_code in (529, 500, 502, 503) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # 5s, 10s, 20s, 40s
                logger.warning(
                    f"Claude API error {e.status_code} (attempt {attempt + 1}/{max_retries}), "
                    f"retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
                continue
            logger.error(f"Claude API error after {attempt + 1} attempts: {e}")
            return {
                "executive_summary": "",
                "decisions": [],
                "tasks": [],
                "follow_ups": [],
                "open_questions": [],
                "stakeholders": [],
                "discussion_summary": f"Error during extraction: {str(e)}",
            }

        except Exception as e:
            logger.error(f"Error calling Claude for extraction: {e}")
            return {
                "executive_summary": "",
                "decisions": [],
                "tasks": [],
                "follow_ups": [],
                "open_questions": [],
                "stakeholders": [],
                "discussion_summary": f"Error during extraction: {str(e)}",
            }

    # Should not reach here, but just in case
    return {
        "executive_summary": "",
        "decisions": [],
        "tasks": [],
        "follow_ups": [],
        "open_questions": [],
        "stakeholders": [],
        "discussion_summary": "Extraction failed after all retries",
    }


async def _extract_with_readback(
    transcript: str,
    meeting_title: str,
    participants: list[str],
    meeting_date: str,
    duration_minutes: int | None,
    sensitivity: str,
) -> dict:
    """
    Run extraction with the v2.5 knowledge read-back loop.

    - SHADOW_MODE (default): run the baseline (shipped) extraction + a
      knowledge-augmented one; log the diff to audit_log; return the BASELINE
      so shipped output is unchanged.
    - READBACK_ENABLED and not shadow (post-cutover): return the augmented
      extraction.
    - Neither flag: plain baseline (pre-v2.5 behavior).

    The augmented run is best-effort — any failure is non-fatal and the
    baseline is returned.
    """
    shadow = settings.KNOWLEDGE_SHADOW_MODE
    readback = settings.KNOWLEDGE_READBACK_ENABLED

    knowledge_context = None
    if shadow or readback:
        try:
            from processors.knowledge_readback import build_knowledge_context
            knowledge_context = await build_knowledge_context(
                meeting_title, participants, sensitivity
            )
        except Exception as e:
            logger.warning(f"[readback] context build failed (non-fatal): {e}")
            knowledge_context = None

    # Post-cutover: ship the augmented extraction directly.
    if readback and not shadow:
        return await extract_structured_data(
            transcript, meeting_title, participants, meeting_date,
            duration_minutes, knowledge_context=knowledge_context,
        )

    # Baseline is what we ship.
    baseline = await extract_structured_data(
        transcript, meeting_title, participants, meeting_date, duration_minutes,
    )

    # Shadow: also run the augmented extraction, log the diff, discard it.
    if shadow and knowledge_context:
        try:
            import time
            t0 = time.monotonic()
            augmented = await extract_structured_data(
                transcript, meeting_title, participants, meeting_date,
                duration_minutes, knowledge_context=knowledge_context,
            )
            latency = time.monotonic() - t0
            from core.shadow_run import log_shadow
            log_shadow(
                "extraction", live=baseline, shadow=augmented,
                latency_s=latency, extra={"meeting_title": meeting_title},
            )
        except Exception as e:
            logger.warning(f"[readback] shadow extraction failed (non-fatal): {e}")

    return baseline


async def _apply_completeness_check(
    transcript: str,
    meeting_title: str,
    extracted: dict,
) -> dict:
    """
    v2.5 PR4 completeness check, gated by the same flags as read-back.

    - SHADOW_MODE: compute the items extraction missed, log the diff to
      audit_log, ship the extraction UNCHANGED.
    - READBACK_ENABLED and not shadow (cutover): merge the additions into the
      extraction so they flow into cross-reference (which dedups them).
    - Neither flag: no-op.

    Best-effort — never raises.
    """
    shadow = settings.KNOWLEDGE_SHADOW_MODE
    readback = settings.KNOWLEDGE_READBACK_ENABLED
    if not (shadow or readback):
        return extracted

    try:
        from processors.completeness_check import find_missing_items

        open_titles: list[str] = []
        try:
            open_titles = [
                (t.get("title") or "")
                for t in supabase_client.get_tasks(status="pending", limit=50)
            ]
        except Exception:
            pass

        additions = find_missing_items(transcript, extracted, open_titles, meeting_title)
        add_tasks = additions.get("tasks") or []
        add_decisions = additions.get("decisions") or []
        if not add_tasks and not add_decisions:
            return extracted

        augmented = {
            **extracted,
            "tasks": (extracted.get("tasks") or []) + add_tasks,
            "decisions": (extracted.get("decisions") or []) + add_decisions,
        }

        if readback and not shadow:
            return augmented  # cutover: additions flow into cross-reference dedup

        # Shadow: log what we'd add, ship unchanged.
        from core.shadow_run import log_shadow
        log_shadow(
            "completeness", live=extracted, shadow=augmented,
            extra={"meeting_title": meeting_title},
        )
        return extracted
    except Exception as e:
        logger.warning(f"[completeness] apply failed (non-fatal): {e}")
        return extracted


def _parse_extraction_response(response_text: str) -> dict:
    """
    Parse Claude's extraction response into structured data.

    Handles both clean JSON responses and JSON embedded in text.

    Args:
        response_text: Raw response from Claude.

    Returns:
        Parsed dict with extracted data.
    """
    # Try direct JSON parse first
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in the response (common with markdown code blocks)
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find JSON object anywhere in response
    json_match = re.search(r'\{[\s\S]*\}', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse extraction response as JSON")

    # Return default structure
    return {
        "executive_summary": "",
        "decisions": [],
        "tasks": [],
        "follow_ups": [],
        "open_questions": [],
        "stakeholders": [],
        "discussion_summary": response_text[:500] if response_text else "",
    }


def _link_decision_chains(meeting_id: str, supersessions: list[dict]) -> None:
    """
    Link superseded decisions with parent_decision_id (Phase 12 A6).

    After decisions are stored, find the newly created decisions for this meeting
    and set parent_decision_id on them to link the chain.

    Args:
        meeting_id: UUID of the current meeting.
        supersessions: List of supersession dicts from cross_reference.
    """
    if not supersessions:
        return

    # Get decisions just stored for this meeting.
    # Tier 3.1: extraction writes children as 'pending' — must include pending
    # to see the ones we just inserted (they're pre-approval).
    new_decisions = supabase_client.list_decisions(
        meeting_id=meeting_id, include_pending=True
    )
    if not new_decisions:
        return

    for s in supersessions:
        old_id = s.get("old_id")
        new_index = s.get("new_index")

        if not old_id or new_index is None:
            continue

        # new_index is 1-based from the LLM
        idx = new_index - 1
        if 0 <= idx < len(new_decisions):
            new_decision = new_decisions[idx]
            new_id = new_decision.get("id")
            if new_id:
                try:
                    supabase_client.set_decision_parent(new_id, old_id)
                    logger.info(f"Linked decision chain: {new_id} → parent {old_id}")
                except Exception as e:
                    logger.debug(f"Failed to link decision chain: {e}")


def extract_task_match_annotations(tasks: list[dict]) -> list[dict]:
    """
    Extract existing_task_match annotations from LLM-extracted tasks (Phase 12 A2).

    Pulls out the structured match annotations that the extraction LLM generates
    when it recognizes a task as related to an existing one.

    Args:
        tasks: List of task dicts from extraction, potentially containing
               existing_task_match fields.

    Returns:
        List of annotation dicts: [{task_index, task_id, confidence, evolution, title}]
        Only includes tasks where existing_task_match is non-null with a task_id.
    """
    annotations = []
    for i, task in enumerate(tasks):
        match = task.get("existing_task_match")
        if not match or not isinstance(match, dict):
            continue

        task_id = match.get("task_id")
        if not task_id:
            continue

        annotations.append({
            "task_index": i,
            "task_id": task_id,
            "confidence": match.get("confidence", "low"),
            "evolution": match.get("evolution"),
            "title": task.get("title", ""),
        })

    return annotations


async def store_meeting_data(
    meeting_id: str,
    decisions: list[dict],
    tasks: list[dict],
    follow_ups: list[dict],
    open_questions: list[dict],
    sensitivity: str = "founders",
) -> None:
    """
    Store extracted data in Supabase tables.

    Args:
        meeting_id: UUID of the parent meeting.
        decisions: List of decision dicts to store.
        tasks: List of task dicts to store.
        follow_ups: List of follow-up meeting dicts to store.
        open_questions: List of open question dicts to store.
        sensitivity: Meeting tier, stamped on each child at insert so the tier is
            atomic with the row (a later propagate failure can't leave CEO content
            at the team-visible default). [audit P1-01]
    """
    # Store decisions
    if decisions:
        supabase_client.create_decisions_batch(meeting_id, decisions, sensitivity=sensitivity)
        logger.info(f"Stored {len(decisions)} decisions")

    # Store tasks
    if tasks:
        supabase_client.create_tasks_batch(meeting_id, tasks, sensitivity=sensitivity)
        logger.info(f"Stored {len(tasks)} tasks")

    # Store follow-up meetings. [audit P1-05] Only stamp the tier once the
    # follow_up_meetings.sensitivity column exists (migration applied + flag on);
    # otherwise pass None so the insert doesn't reference a missing column.
    if follow_ups:
        _fu_sensitivity = sensitivity if settings.FOLLOW_UP_SENSITIVITY_ENABLED else None
        supabase_client.create_follow_ups_batch(
            meeting_id, follow_ups, sensitivity=_fu_sensitivity
        )
        logger.info(f"Stored {len(follow_ups)} follow-up meetings")

    # Store open questions
    if open_questions:
        supabase_client.create_open_questions_batch(meeting_id, open_questions, sensitivity=sensitivity)
        logger.info(f"Stored {len(open_questions)} open questions")


async def generate_and_store_embeddings(
    meeting_id: str,
    transcript: str,
    sensitivity: str = "founders",
) -> None:
    """
    Chunk transcript and store embeddings in Supabase.

    v0.2 upgrade: When meeting metadata (title, date, participants) is
    available, uses context-enriched embeddings so the vectors capture
    who/when/what — improving search recall for questions like
    "What did Roye say in the MVP meeting?".

    Falls back to the original non-contextual method if the meeting
    record cannot be retrieved.

    Args:
        meeting_id: UUID of the meeting.
        transcript: Full transcript text.
    """
    try:
        # Try to get meeting metadata for contextual embeddings
        meeting = None
        try:
            meeting = supabase_client.get_meeting(meeting_id)
        except Exception as e:
            logger.warning(f"Could not fetch meeting {meeting_id} for context: {e}")

        # Use contextual method if we have meeting info, otherwise fall back
        if meeting and meeting.get("title"):
            # v0.2: Context-enriched embeddings — vectors include meeting context
            embedded_chunks = await embedding_service.chunk_and_embed_transcript_with_context(
                transcript=transcript,
                meeting_id=meeting_id,
                meeting_title=meeting.get("title", ""),
                meeting_date=meeting.get("date", ""),
                participants=meeting.get("participants", []),
            )
            logger.info(f"Using context-enriched embeddings for meeting {meeting_id}")
        else:
            # Fallback: original method without context prefix
            embedded_chunks = await embedding_service.chunk_and_embed_transcript(
                transcript=transcript,
                meeting_id=meeting_id,
            )
            logger.info(f"Using standard embeddings for meeting {meeting_id} (no metadata)")

        if not embedded_chunks:
            logger.warning(f"No embeddings generated for meeting {meeting_id}")
            return

        # Prepare records for storage
        embedding_records = [
            {
                "source_type": "meeting",
                "source_id": meeting_id,
                "chunk_text": chunk["text"],
                "chunk_index": chunk["chunk_index"],
                "speaker": chunk.get("speaker"),
                "timestamp_range": chunk.get("timestamp_range"),
                "embedding": chunk["embedding"],
                "metadata": chunk.get("metadata", {}),
                "sensitivity": sensitivity,
            }
            for chunk in embedded_chunks
        ]

        # Store in Supabase
        supabase_client.store_embeddings_batch(embedding_records)
        logger.info(f"Stored {len(embedding_records)} embeddings for meeting {meeting_id}")

    except Exception as e:
        logger.error(f"Error generating/storing embeddings: {e}")
        # Don't fail the whole process if embeddings fail
        # The meeting data is still valuable without embeddings
