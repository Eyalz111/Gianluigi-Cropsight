"""
Cross-reference processor for v0.3 Operational Intelligence.

This module connects the dots across meetings:
1. Task deduplication — detect when a "new" task is actually an existing one
2. Status inference — detect when open tasks have been completed or progressed
3. Open question resolution — detect when open questions get answered

All inferred changes go through Eyal's approval flow (bundled with meeting summary).
Tasks are founder-level strategic commitments — inference must be conservative.

Usage:
    from processors.cross_reference import run_cross_reference

    results = await run_cross_reference(
        meeting_id="uuid",
        transcript="...",
        new_tasks=[...],
    )
"""

import json
import logging
import re
from typing import Any

from config.settings import settings
from core.llm import call_llm
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


async def deduplicate_tasks(
    new_tasks: list[dict],
    meeting_id: str,
    transcript: str,
) -> dict:
    """
    Classify newly extracted tasks as NEW, DUPLICATE, or UPDATE.

    Compares new tasks against all existing open tasks using Claude Haiku.
    Conservative: only marks duplicates when clearly the same underlying work.

    Args:
        new_tasks: Tasks just extracted from the current transcript.
        meeting_id: UUID of the current meeting.
        transcript: Full transcript text (for context).

    Returns:
        Dict with:
        - new_tasks: Genuinely new tasks to insert normally.
        - duplicates: Matched to existing tasks, create task_mention only.
        - updates: Matched + status change implied.
    """
    result = {"new_tasks": list(new_tasks), "duplicates": [], "updates": []}

    if not new_tasks:
        return result

    # Fetch existing tasks. Compare against open (pending/in_progress) PLUS
    # tasks completed in the last 30 days — the 2026-04-11 audit found that
    # excluding `done` tasks let re-mentioned work reappear as a fresh
    # duplicate (e.g. a meeting rehashes a task that was closed last week).
    # Including recent `done` tasks lets Haiku classify them as UPDATE
    # (status change implied) instead of NEW.
    from datetime import datetime, timedelta, timezone
    pending = supabase_client.get_tasks(status="pending", limit=500)
    in_progress = supabase_client.get_tasks(status="in_progress", limit=500)
    recently_done = []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        done_query = (
            supabase_client.client.table("tasks")
            .select("*, meetings(title, date)")
            .eq("status", "done")
            .eq("approval_status", "approved")
            .gte("updated_at", cutoff)
            .order("updated_at", desc=True)
            .limit(100)
            .execute()
        )
        recently_done = done_query.data or []
    except Exception as e:
        # updated_at column may not exist in older schemas — fail soft
        logger.warning(f"deduplicate_tasks: could not fetch recently-done tasks ({e}), "
                       f"falling back to open-only comparison")
    existing_tasks = pending + in_progress + recently_done

    if not existing_tasks:
        # No existing tasks to compare against — all are new
        return result

    # Build the classification prompt
    existing_lines = []
    for i, t in enumerate(existing_tasks, 1):
        tid = t.get("id", "?")
        title = t.get("title", "?")
        assignee = t.get("assignee", "?")
        category = t.get("category", "")
        status = t.get("status", "pending")
        existing_lines.append(
            f'{i}. [id: {tid}] "{title}" ({assignee}, {category}, {status})'
        )

    new_lines = []
    for i, t in enumerate(new_tasks):
        letter = chr(65 + i)  # A, B, C, ...
        title = t.get("title", "?")
        assignee = t.get("assignee", "?")
        category = t.get("category", "")
        new_lines.append(f'{letter}. "{title}" ({assignee}, {category})')

    prompt = f"""You are analyzing meeting tasks for a startup founding team. These are high-level strategic tasks, not granular work items.

EXISTING TASKS (open + recently completed in last 30 days):
{chr(10).join(existing_lines)}

NEWLY EXTRACTED TASKS FROM THIS MEETING:
{chr(10).join(new_lines)}

For each new task, classify:
- DUPLICATE of #N — same task, different wording. No new info.
- UPDATE of #N — same task, but conversation implies a status change. Specify new_status and evidence.
- NEW — genuinely new task not in the existing list.

RULES:
- Be CONSERVATIVE: only classify as DUPLICATE/UPDATE when clearly the same underlying work.
- Different tasks for the same person in the same category are NOT duplicates by default.
- EXCEPTION — scheduling tasks: if a new task is "Schedule: X meeting/session/call"
  and an existing task (any assignee) is another "Schedule: X" for the SAME event
  (same subject + same approximate timing), classify as DUPLICATE even if the
  wording differs. Two people scheduling the same meeting is one task, not two.
- If an existing task in the list has status "done" and the new task references
  the same work without implying a reopen, classify as DUPLICATE (the work is
  already captured). Only classify as UPDATE when the transcript makes a status
  change explicit.

Return JSON:
{{"classifications": [{{"new_task_index": "A", "type": "DUPLICATE|UPDATE|NEW", "existing_task_id": "abc or null", "new_status": "done|in_progress|null", "evidence": "quote or null", "reason": "why this classification"}}]}}"""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=2048,
            call_site="task_dedup",
        )
        parsed = _parse_json_response(response_text)
        classifications = parsed.get("classifications", [])
    except Exception as e:
        logger.error(f"Error in deduplicate_tasks LLM call: {e}")
        # On error, treat all as new (safe default)
        return result

    # Process classifications
    genuinely_new = []
    for classification in classifications:
        idx_letter = classification.get("new_task_index", "")
        ctype = classification.get("type", "NEW").upper()

        # Map letter index (A, B, C...) back to task
        if len(idx_letter) == 1 and idx_letter.isalpha():
            task_idx = ord(idx_letter.upper()) - 65
        else:
            task_idx = -1

        if task_idx < 0 or task_idx >= len(new_tasks):
            continue

        task = new_tasks[task_idx]
        existing_id = classification.get("existing_task_id")

        if ctype == "DUPLICATE" and existing_id:
            result["duplicates"].append({
                "task": task,
                "existing_task_id": existing_id,
                "reason": classification.get("reason", ""),
            })
        elif ctype == "UPDATE" and existing_id:
            result["updates"].append({
                "task": task,
                "existing_task_id": existing_id,
                "new_status": classification.get("new_status"),
                "evidence": classification.get("evidence", ""),
                "reason": classification.get("reason", ""),
            })
        else:
            genuinely_new.append(task)

    # Any tasks not classified are treated as new
    classified_indices = set()
    for c in classifications:
        idx_letter = c.get("new_task_index", "")
        if len(idx_letter) == 1 and idx_letter.isalpha():
            classified_indices.add(ord(idx_letter.upper()) - 65)

    for i, task in enumerate(new_tasks):
        if i not in classified_indices:
            genuinely_new.append(task)

    result["new_tasks"] = genuinely_new
    return result


async def infer_task_status_changes(
    meeting_id: str,
    transcript: str,
) -> list[dict]:
    """
    Detect when open tasks have been completed or progressed.

    Analyzes a meeting transcript against all open tasks to find
    evidence of status changes. Conservative: only flags changes
    with clear evidence.

    Args:
        meeting_id: UUID of the meeting.
        transcript: Full transcript text.

    Returns:
        List of inferred changes:
        [{"task_id", "task_title", "new_status", "evidence", "confidence", "reasoning"}]
    """
    # Fetch all open tasks
    pending = supabase_client.get_tasks(status="pending")
    in_progress = supabase_client.get_tasks(status="in_progress")
    open_tasks = pending + in_progress

    if not open_tasks:
        return []

    # Build the prompt
    task_lines = []
    for i, t in enumerate(open_tasks, 1):
        tid = t.get("id", "?")
        title = t.get("title", "?")
        assignee = t.get("assignee", "?")
        category = t.get("category", "")
        status = t.get("status", "pending")
        created = str(t.get("created_at", ""))[:10]
        task_lines.append(
            f'{i}. [id: {tid}] "{title}" ({assignee}, {status}, created {created})'
        )

    # Truncate transcript if too long (Sonnet can handle more context)
    truncated = transcript[:12000] if len(transcript) > 12000 else transcript

    prompt = f"""You are an operations analyst for a startup founding team. Your job is to detect when open tasks have been completed or progressed based on meeting conversations.

IMPORTANT: These are high-level strategic tasks (e.g., "Secure Series A funding", "Close Moldova PoC"). Be very conservative:
- "We're making progress" = still in_progress, NOT done
- "We finished it" or "It's signed" = done (with high confidence)
- Casual mentions without status info = no change
- Only flag changes you are genuinely confident about

OPEN TASKS:
{chr(10).join(task_lines)}

MEETING TRANSCRIPT:
{truncated}

Think step by step about each task. For each one that has a status change, provide:
- task_id
- current_status -> new_status
- evidence: exact quote from transcript
- confidence: high (explicit statement) / medium (strong implication) / low (weak signal)

Only include tasks where the evidence clearly supports a change. When in doubt, do NOT flag it.

Return JSON:
{{"status_changes": [{{"task_id": "abc", "new_status": "done|in_progress", "evidence": "exact quote", "confidence": "high|medium|low", "reasoning": "why"}}]}}"""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=2048,
            call_site="status_inference",
            meeting_id=meeting_id,
        )
        parsed = _parse_json_response(response_text)
        changes = parsed.get("status_changes", [])
    except Exception as e:
        logger.error(f"Error in infer_task_status_changes LLM call: {e}")
        return []

    # Enrich with task title for display
    task_map = {t["id"]: t for t in open_tasks}
    enriched = []
    for change in changes:
        task_id = change.get("task_id", "")
        task = task_map.get(task_id)
        if task:
            change["task_title"] = task.get("title", "")
            change["assignee"] = task.get("assignee", "")
            enriched.append(change)
        else:
            logger.warning(f"Status change references unknown task_id: {task_id}")

    return enriched


async def resolve_open_questions(
    meeting_id: str,
    transcript: str,
) -> list[dict]:
    """
    Detect when previously raised open questions get answered.

    Compares all open questions against the meeting transcript to find
    answers or resolutions.

    Args:
        meeting_id: UUID of the current meeting.
        transcript: Full transcript text.

    Returns:
        List of resolved questions:
        [{"question_id", "question", "answer", "evidence", "confidence"}]
    """
    # Fetch all open questions
    open_qs = supabase_client.get_open_questions(status="open")

    if not open_qs:
        return []

    # Build the prompt
    q_lines = []
    for i, q in enumerate(open_qs, 1):
        qid = q.get("id", "?")
        question = q.get("question", "?")
        raised_by = q.get("raised_by", "unknown")
        q_lines.append(f'{i}. [id: {qid}] "{question}" (raised by {raised_by})')

    truncated = transcript[:8000] if len(transcript) > 8000 else transcript

    prompt = f"""You are reviewing a meeting transcript to check if any previously raised open questions have been answered.

OPEN QUESTIONS:
{chr(10).join(q_lines)}

MEETING TRANSCRIPT:
{truncated}

For each question that was clearly answered in this meeting, provide:
- question_id
- answer: brief summary of the answer
- evidence: exact quote from transcript
- confidence: high (directly answered) / medium (implicitly answered) / low (partially addressed)

Only include questions where the answer is clear. When in doubt, leave the question as open.

Return JSON:
{{"resolved_questions": [{{"question_id": "abc", "answer": "summary", "evidence": "exact quote", "confidence": "high|medium|low"}}]}}"""

    try:
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=1024,
            call_site="question_resolution",
            meeting_id=meeting_id,
        )
        parsed = _parse_json_response(response_text)
        resolved = parsed.get("resolved_questions", [])
    except Exception as e:
        logger.error(f"Error in resolve_open_questions LLM call: {e}")
        return []

    # Enrich with question text for display
    q_map = {q["id"]: q for q in open_qs}
    enriched = []
    for r in resolved:
        qid = r.get("question_id", "")
        q = q_map.get(qid)
        if q:
            r["question"] = q.get("question", "")
            r["raised_by"] = q.get("raised_by", "")
            enriched.append(r)

    return enriched


async def run_cross_reference(
    meeting_id: str,
    transcript: str,
    new_tasks: list[dict],
    **kwargs,
) -> dict:
    """
    Orchestrate all cross-reference analyses for a meeting.

    Runs deduplication, status inference, and question resolution.
    Returns combined results for inclusion in the approval request.

    Args:
        meeting_id: UUID of the meeting.
        transcript: Full transcript text.
        new_tasks: Newly extracted tasks from this meeting.
        **kwargs:
            new_decisions: Decisions for supersession detection.
            task_match_annotations: LLM-generated match annotations (Phase 12 A2).

    Returns:
        Dict with:
        - dedup: {new_tasks, duplicates, updates} from deduplicate_tasks
        - status_changes: list from infer_task_status_changes
        - resolved_questions: list from resolve_open_questions
        - task_match_annotations: raw annotations from extraction (Phase 12 A2)
    """
    logger.info(f"Running cross-reference analysis for meeting {meeting_id}")

    # Phase 12 A2: Process LLM-generated task match annotations
    task_match_annotations = kwargs.get("task_match_annotations", [])
    if task_match_annotations:
        logger.info(
            f"Received {len(task_match_annotations)} task match annotations from extraction"
        )
        _process_task_match_annotations(
            annotations=task_match_annotations,
            meeting_id=meeting_id,
        )

    # Run deduplication
    dedup = await deduplicate_tasks(
        new_tasks=new_tasks,
        meeting_id=meeting_id,
        transcript=transcript,
    )

    # Run status inference
    status_changes = await infer_task_status_changes(
        meeting_id=meeting_id,
        transcript=transcript,
    )

    # Run open question resolution
    resolved_questions = await resolve_open_questions(
        meeting_id=meeting_id,
        transcript=transcript,
    )

    # Create task_mention records for duplicates and updates
    mentions_to_create = []
    for dup in dedup.get("duplicates", []):
        mentions_to_create.append({
            "task_id": dup["existing_task_id"],
            "meeting_id": meeting_id,
            "mention_text": dup["task"].get("title", ""),
            "implied_status": None,
            "confidence": "medium",
            "evidence": dup.get("reason", ""),
        })
    for upd in dedup.get("updates", []):
        mentions_to_create.append({
            "task_id": upd["existing_task_id"],
            "meeting_id": meeting_id,
            "mention_text": upd["task"].get("title", ""),
            "implied_status": upd.get("new_status"),
            "confidence": "medium",
            "evidence": upd.get("evidence", ""),
        })
    for change in status_changes:
        mentions_to_create.append({
            "task_id": change["task_id"],
            "meeting_id": meeting_id,
            "mention_text": change.get("task_title", ""),
            "implied_status": change.get("new_status"),
            "confidence": change.get("confidence", "medium"),
            "evidence": change.get("evidence", ""),
        })

    if mentions_to_create:
        try:
            supabase_client.create_task_mentions_batch(mentions_to_create)
            logger.info(f"Created {len(mentions_to_create)} task mentions")
        except Exception as e:
            logger.error(f"Error creating task mentions: {e}")

    result = {
        "dedup": dedup,
        "status_changes": status_changes,
        "resolved_questions": resolved_questions,
        "task_match_annotations": task_match_annotations,
    }

    # Log summary
    logger.info(
        f"Cross-reference complete: "
        f"{len(dedup.get('new_tasks', []))} new tasks, "
        f"{len(dedup.get('duplicates', []))} duplicates, "
        f"{len(dedup.get('updates', []))} updates, "
        f"{len(status_changes)} status changes, "
        f"{len(resolved_questions)} questions resolved, "
        f"{len(task_match_annotations)} match annotations"
    )

    # Phase 9A: Detect decision supersessions
    try:
        new_decisions = kwargs.get("new_decisions", [])
        if new_decisions:
            supersessions = await detect_supersessions(meeting_id, new_decisions)
            result["supersessions"] = supersessions

            # Phase 12 A4: Touch referenced decisions (freshness tracking)
            for s in supersessions:
                old_id = s.get("old_id")
                if old_id:
                    try:
                        supabase_client.touch_decision(old_id)
                    except Exception:
                        pass  # Non-fatal
        else:
            result["supersessions"] = []
    except Exception as e:
        logger.error(f"Supersession detection failed (non-fatal): {e}")
        result["supersessions"] = []

    return result


async def detect_supersessions(
    meeting_id: str,
    new_decisions: list[dict],
) -> list[dict]:
    """
    Detect when new decisions contradict or supersede existing active decisions.

    Uses Sonnet (not Haiku) because this is a reasoning task, not classification.

    Args:
        meeting_id: UUID of the current meeting.
        new_decisions: List of newly extracted decisions.

    Returns:
        List of supersession pairs: [{new_decision, old_decision_id, reason}]
    """
    if not new_decisions:
        return []

    # Fetch active decisions with labels
    existing = supabase_client.list_decisions(limit=50)
    active_decisions = [
        d for d in existing
        if d.get("decision_status", "active") == "active"
        and d.get("meeting_id") != meeting_id
    ]

    if not active_decisions:
        return []

    # Build comparison prompt
    existing_list = []
    for i, d in enumerate(active_decisions[:20], 1):
        label = d.get("label", "")
        desc = d.get("description", "")[:100]
        existing_list.append(f"  {i}. [{label}] {desc} (id: {d['id']})")

    new_list = []
    for i, d in enumerate(new_decisions, 1):
        label = d.get("label", "")
        desc = d.get("description", "")[:100]
        new_list.append(f"  {i}. [{label}] {desc}")

    prompt = f"""Compare these NEW decisions from today's meeting against EXISTING active decisions.

EXISTING ACTIVE DECISIONS:
{chr(10).join(existing_list)}

NEW DECISIONS:
{chr(10).join(new_list)}

For each new decision, determine if it SUPERSEDES (contradicts, replaces, or significantly updates) any existing decision. Be conservative — only flag true contradictions or replacements, not mere elaborations.

Return JSON: {{"supersessions": [{{"new_index": 1, "old_id": "uuid", "reason": "brief explanation"}}]}}
If no supersessions found, return: {{"supersessions": []}}"""

    try:
        from core.llm import call_llm
        import json

        response_text, _usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,  # Sonnet — reasoning task
            max_tokens=1024,
            call_site="supersession_detection",
            meeting_id=meeting_id,
        )

        clean = response_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]

        parsed = json.loads(clean.strip())
        supersessions = parsed.get("supersessions", [])

        # Validate IDs
        valid_ids = {d["id"] for d in active_decisions}
        validated = [
            s for s in supersessions
            if s.get("old_id") in valid_ids
        ]

        if validated:
            logger.info(f"Detected {len(validated)} decision supersessions in meeting {meeting_id}")

        return validated

    except Exception as e:
        logger.error(f"Supersession detection LLM call failed: {e}")
        return []


def _process_task_match_annotations(
    annotations: list[dict],
    meeting_id: str,
) -> None:
    """
    Process LLM-generated task match annotations (Phase 12 A2).

    Creates task_mention records for annotations and, when auto-apply is enabled,
    applies high-confidence status changes directly.

    Args:
        annotations: List of annotation dicts from extract_task_match_annotations().
        meeting_id: UUID of the current meeting.
    """
    if not annotations:
        return

    # Create task_mention records for all annotations (always, regardless of auto-apply)
    mentions = []
    for ann in annotations:
        evolution = ann.get("evolution")
        implied_status = None
        if evolution == "completion":
            implied_status = "done"
        elif evolution == "status_update":
            implied_status = "in_progress"

        mentions.append({
            "task_id": ann["task_id"],
            "meeting_id": meeting_id,
            "mention_text": ann.get("title", ""),
            "implied_status": implied_status,
            "confidence": ann.get("confidence", "low"),
            "evidence": f"LLM extraction match (evolution: {evolution})",
        })

    if mentions:
        try:
            supabase_client.create_task_mentions_batch(mentions)
            logger.info(
                f"Created {len(mentions)} task mentions from extraction annotations"
            )
        except Exception as e:
            logger.error(f"Error creating task mentions from annotations: {e}")

    # Feature-gated auto-apply: only when explicitly enabled
    if not settings.CONTINUITY_AUTO_APPLY_ENABLED:
        return

    # Auto-apply high-confidence changes
    for ann in annotations:
        if ann.get("confidence") != "high":
            continue

        task_id = ann["task_id"]
        evolution = ann.get("evolution")

        try:
            if evolution == "completion":
                supabase_client.update_task(task_id, status="done")
                logger.info(f"Auto-applied completion for task {task_id}")
            elif evolution == "status_update":
                supabase_client.update_task(task_id, status="in_progress")
                logger.info(f"Auto-applied status_update for task {task_id}")
        except Exception as e:
            logger.error(f"Auto-apply failed for task {task_id}: {e}")


def _parse_json_response(response_text: str) -> dict:
    """
    Parse a JSON response from Claude, handling markdown code blocks.

    Args:
        response_text: Raw response text from Claude.

    Returns:
        Parsed dict, or empty dict on failure.
    """
    # Try direct JSON parse
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in markdown code blocks
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find JSON object anywhere
    json_match = re.search(r'\{[\s\S]*\}', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning(f"Could not parse JSON from response: {response_text[:200]}")
    return {}
