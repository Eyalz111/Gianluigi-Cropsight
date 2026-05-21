"""
Completeness check (v2.5 PR4).

A one-shot Haiku pass that audits the primary extraction: given the transcript,
the items already extracted, and known open commitments, it surfaces action
items / decisions that were clearly stated but MISSED. Conservative by design —
it only adds things that are unambiguously in the transcript and not already
covered.

It runs after extraction and BEFORE cross-reference, so any additions are
deduplicated by the existing deduplicate_tasks pass (no double-counting). The
flag-gating + shadow logging live in the caller
(processors.transcript_processor._apply_completeness_check).
"""

import json
import logging
import re

from config.settings import settings
from core.llm import call_llm

logger = logging.getLogger(__name__)

_MAX_LIST = 30


def _parse_json(response: str) -> dict | None:
    """Extract a JSON object from an LLM response, tolerating code fences."""
    if not response:
        return None
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    obj = re.search(r"\{[\s\S]*\}", response)
    if obj:
        try:
            return json.loads(obj.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _build_prompt(
    transcript: str,
    extracted: dict,
    open_commitments: list[str],
    meeting_title: str,
) -> str:
    extracted_tasks = [
        (t.get("title") or "").strip()
        for t in (extracted.get("tasks") or [])[:_MAX_LIST]
        if (t.get("title") or "").strip()
    ]
    extracted_decisions = [
        (d.get("description") or "").strip()
        for d in (extracted.get("decisions") or [])[:_MAX_LIST]
        if (d.get("description") or "").strip()
    ]

    def _bullets(items: list[str]) -> str:
        return "\n".join(f"  - {i}" for i in items) if items else "  (none)"

    return f"""You are a completeness auditor for meeting-extraction output.

Meeting: {meeting_title}

Action items ALREADY extracted:
{_bullets(extracted_tasks)}

Decisions ALREADY extracted:
{_bullets(extracted_decisions)}

Existing open commitments already tracked elsewhere (do NOT re-add these):
{_bullets(open_commitments[:_MAX_LIST])}

Transcript:
{transcript}

Find ONLY action items or decisions that are clearly and explicitly stated in
the transcript but are MISSING from the lists above. Be conservative:
- If an item is already covered (even loosely) by an extracted item or an
  existing commitment, do NOT include it.
- If it is only vaguely implied or hypothetical, do NOT include it.
- Prefer returning nothing over guessing.

Return ONLY valid JSON:
{{
  "tasks": [{{"title": "...", "assignee": "name or empty", "priority": "M"}}],
  "decisions": [{{"description": "...", "label": "2-3 word topic"}}]
}}
Use empty arrays if nothing was missed. No prose, no code fences."""


def find_missing_items(
    transcript: str,
    extracted: dict,
    open_commitments: list[str],
    meeting_title: str,
) -> dict:
    """
    Return {"tasks": [...], "decisions": [...]} of items the extraction missed.

    Conservative Haiku pass. Never raises — returns empty lists on any failure.
    Added tasks carry deadline_confidence='NONE' so they don't trigger reminders
    without an explicit deadline.
    """
    try:
        prompt = _build_prompt(transcript, extracted, open_commitments, meeting_title)
        response, _usage = call_llm(
            prompt=prompt,
            model=settings.model_simple,  # Haiku — cheap, runs per meeting
            max_tokens=1500,
            call_site="completeness_check",
        )
        data = _parse_json(response) or {}
        tasks = []
        for t in (data.get("tasks") or []):
            title = (t.get("title") or "").strip()
            if not title:
                continue
            tasks.append({
                "title": title,
                "assignee": t.get("assignee", ""),
                "priority": t.get("priority", "M"),
                "deadline_confidence": "NONE",
                "_source": "completeness_check",
            })
        decisions = []
        for d in (data.get("decisions") or []):
            desc = (d.get("description") or "").strip()
            if not desc:
                continue
            decisions.append({
                "description": desc,
                "label": d.get("label", ""),
                "_source": "completeness_check",
            })
        if tasks or decisions:
            logger.info(
                f"[completeness] surfaced {len(tasks)} task(s), {len(decisions)} decision(s) "
                f"for '{meeting_title}'"
            )
        return {"tasks": tasks, "decisions": decisions}
    except Exception as e:
        logger.warning(f"[completeness] check failed (non-fatal) for '{meeting_title}': {e}")
        return {"tasks": [], "decisions": []}
