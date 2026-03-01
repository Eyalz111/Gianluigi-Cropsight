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

from anthropic import Anthropic

from config.settings import settings
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

    # Fetch all open tasks from Supabase
    pending = supabase_client.get_tasks(status="pending")
    in_progress = supabase_client.get_tasks(status="in_progress")
    existing_tasks = pending + in_progress

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

EXISTING OPEN TASKS:
{chr(10).join(existing_lines)}

NEWLY EXTRACTED TASKS FROM THIS MEETING:
{chr(10).join(new_lines)}

For each new task, classify:
- DUPLICATE of #N — same task, different wording. No new info.
- UPDATE of #N — same task, but conversation implies a status change. Specify new_status and evidence.
- NEW — genuinely new task not in the existing list.

Be CONSERVATIVE: only classify as DUPLICATE/UPDATE when clearly the same underlying work. Different tasks for the same person in the same category are NOT duplicates.

Return JSON:
{{"classifications": [{{"new_task_index": "A", "type": "DUPLICATE|UPDATE|NEW", "existing_task_id": "abc or null", "new_status": "done|in_progress|null", "evidence": "quote or null", "reason": "why this classification"}}]}}"""

    try:
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=settings.model_simple,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text
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
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=settings.model_agent,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text
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
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=settings.model_simple,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text
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
    pre_extracted_commitments: list[dict] | None = None,
) -> dict:
    """
    Orchestrate all cross-reference analyses for a meeting.

    Runs deduplication, status inference, and question resolution.
    Returns combined results for inclusion in the approval request.

    Args:
        meeting_id: UUID of the meeting.
        transcript: Full transcript text.
        new_tasks: Newly extracted tasks from this meeting.
        pre_extracted_commitments: Commitments already extracted by Opus
            (avoids 8K truncation). Falls back to Haiku extraction if None.

    Returns:
        Dict with:
        - dedup: {new_tasks, duplicates, updates} from deduplicate_tasks
        - status_changes: list from infer_task_status_changes
        - resolved_questions: list from resolve_open_questions
    """
    logger.info(f"Running cross-reference analysis for meeting {meeting_id}")

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

    # v0.3 Tier 2: Extract commitments (prefer Opus pre-extraction over Haiku fallback)
    if pre_extracted_commitments:
        new_commitments = pre_extracted_commitments
        logger.info(f"Using {len(new_commitments)} pre-extracted commitments from Opus")
    else:
        new_commitments = await extract_commitments(
            meeting_id=meeting_id,
            transcript=transcript,
            participants=[],  # Participants not available here, but not critical
        )

    # Store new commitments in Supabase
    if new_commitments:
        try:
            supabase_client.create_commitments_batch(meeting_id, new_commitments)
        except Exception as e:
            logger.error(f"Error storing commitments: {e}")

    # v0.3 Tier 2: Check for commitment fulfillment
    fulfillments = await check_commitment_fulfillment(
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
        "new_commitments": new_commitments,
        "commitment_fulfillments": fulfillments,
    }

    # Log summary
    logger.info(
        f"Cross-reference complete: "
        f"{len(dedup.get('new_tasks', []))} new tasks, "
        f"{len(dedup.get('duplicates', []))} duplicates, "
        f"{len(dedup.get('updates', []))} updates, "
        f"{len(status_changes)} status changes, "
        f"{len(resolved_questions)} questions resolved, "
        f"{len(new_commitments)} commitments, "
        f"{len(fulfillments)} fulfillments"
    )

    return result


async def extract_commitments(
    meeting_id: str,
    transcript: str,
    participants: list[str],
) -> list[dict]:
    """
    Extract verbal commitments from a meeting transcript.

    Commitments are promises like "I'll send that by Friday" or
    "Let me check with the lawyers and get back to you". These may
    or may not overlap with formal tasks.

    Args:
        meeting_id: UUID of the meeting.
        transcript: Full transcript text.
        participants: List of participant names.

    Returns:
        List of commitment dicts:
        [{speaker, commitment_text, context, implied_deadline}]
    """
    truncated = transcript[:8000] if len(transcript) > 8000 else transcript

    prompt = f"""You are analyzing a startup founding team meeting for verbal commitments — promises or pledges to do something.

Examples of commitments:
- "I'll send that by Friday"
- "Let me check with the lawyers and get back to you"
- "I'll set up a meeting with Jason next week"
- "We'll have the prototype ready by end of month"

NOT commitments (filter these out):
- Past-tense statements ("I already sent it")
- General observations ("We should think about this")
- Questions ("Can you look into it?")

PARTICIPANTS: {', '.join(participants)}

TRANSCRIPT:
{truncated}

For each commitment, provide:
- speaker: Who made the commitment
- commitment_text: What they committed to (concise)
- context: Brief surrounding context (1 sentence)
- implied_deadline: Deadline mentioned or "none"

Return JSON:
{{"commitments": [{{"speaker": "...", "commitment_text": "...", "context": "...", "implied_deadline": "..."}}]}}

Be conservative — only include clear, actionable commitments. Exclude vague intentions."""

    try:
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=settings.model_simple,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text
        parsed = _parse_json_response(response_text)
        commitments = parsed.get("commitments", [])
        logger.info(f"Extracted {len(commitments)} commitments from meeting {meeting_id}")
        return commitments
    except Exception as e:
        logger.error(f"Error extracting commitments: {e}")
        return []


async def check_commitment_fulfillment(
    meeting_id: str,
    transcript: str,
) -> list[dict]:
    """
    Check if any open commitments have been fulfilled in this meeting.

    Compares all open commitments against the transcript to detect
    explicit or implicit fulfillment.

    Args:
        meeting_id: UUID of the current meeting.
        transcript: Full transcript text.

    Returns:
        List of fulfillment detections:
        [{commitment_id, evidence, confidence}]
    """
    # Fetch all open commitments
    open_commitments = supabase_client.get_commitments(status="open")

    if not open_commitments:
        return []

    # Build the prompt
    c_lines = []
    for i, c in enumerate(open_commitments, 1):
        cid = c.get("id", "?")
        speaker = c.get("speaker", "?")
        text = c.get("commitment_text", "?")
        deadline = c.get("implied_deadline", "none")
        c_lines.append(
            f'{i}. [id: {cid}] {speaker}: "{text}" (deadline: {deadline})'
        )

    truncated = transcript[:8000] if len(transcript) > 8000 else transcript

    prompt = f"""You are checking if any open commitments have been fulfilled based on a meeting transcript.

OPEN COMMITMENTS:
{chr(10).join(c_lines)}

MEETING TRANSCRIPT:
{truncated}

For each commitment that was clearly fulfilled (e.g., someone says "I sent that email" or "the meeting with Jason is set up"), provide:
- commitment_id: The ID from the list above
- evidence: Exact quote from transcript showing fulfillment
- confidence: high (explicitly stated) / medium (strongly implied) / low (partially addressed)

Only include commitments where fulfillment is clearly evidenced. When in doubt, leave it as open.

Return JSON:
{{"fulfilled": [{{"commitment_id": "...", "evidence": "...", "confidence": "..."}}]}}"""

    try:
        client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=settings.model_simple,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.content[0].text
        parsed = _parse_json_response(response_text)
        fulfilled = parsed.get("fulfilled", [])

        # Validate commitment IDs exist
        valid_ids = {c["id"] for c in open_commitments}
        enriched = []
        for f in fulfilled:
            if f.get("commitment_id") in valid_ids:
                enriched.append(f)
            else:
                logger.warning(
                    f"Fulfillment references unknown commitment: {f.get('commitment_id')}"
                )

        logger.info(f"Found {len(enriched)} commitment fulfillments in meeting {meeting_id}")
        return enriched

    except Exception as e:
        logger.error(f"Error checking commitment fulfillment: {e}")
        return []


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
