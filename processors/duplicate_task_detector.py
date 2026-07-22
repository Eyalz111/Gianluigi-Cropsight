"""Flag within-meeting duplicate action items on the approval card.

WHY
---
Two tasks can be the SAME underlying action worded differently — "send the
Scope-of-Work doc to Ido" vs "send the deployment task-list doc to Ido" (both
"send Ido a doc before the meeting"). The text-similarity dedup elsewhere is
deliberately STRICT (it must never false-merge two genuinely-distinct tasks),
so it can't catch these SEMANTIC duplicates. Telling "two phrasings of one task"
from "two real different tasks" is a judgement call — so we ask an LLM, and we
only FLAG the suspected pairs for Eyal to resolve on the approval card. We never
auto-merge: a wrong merge would silently drop a real task, which is worse than a
visible duplicate Eyal can remove with one edit.

Runs at approval time (guardrails/approval_flow.submit_for_approval), so it
catches duplicates whether extraction OR a later edit created them. Non-fatal:
any error just yields no flag.
"""
from __future__ import annotations

import json
import logging

from config.settings import settings
from core.llm import call_llm

logger = logging.getLogger(__name__)


def _parse(text: str) -> dict:
    clean = (text or "").strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
    if clean.endswith("```"):
        clean = clean[:-3]
    try:
        return json.loads(clean.strip())
    except Exception:
        return {}


async def detect_duplicate_task_pairs(
    tasks: list[dict], meeting_id: str | None = None
) -> list[dict]:
    """Return pairs of tasks that look like the SAME underlying action.

    Each pair is {"a": <1-based idx>, "b": <1-based idx>, "reason": str}. The
    indices match the position in `tasks`. Returns [] for <2 tasks or on any
    error (fail-open — never blocks approval).
    """
    if not tasks or len(tasks) < 2:
        return []

    numbered = "\n".join(
        f'{i + 1}. "{(t.get("title") or "").strip()}" '
        f'(owner: {t.get("assignee") or "unassigned"})'
        for i, t in enumerate(tasks)
    )
    prompt = f"""You are reviewing a meeting's ACTION ITEMS for a startup founding team, looking for pairs that are the SAME underlying task worded differently — so a human can merge them before the summary goes out.

ACTION ITEMS:
{numbered}

Flag a pair ONLY when the two items are genuinely the SAME action/deliverable, just phrased differently or split apart. Examples of a DUPLICATE pair:
- "Send the Scope-of-Work doc to Ido" + "Send the deployment task-list doc to Ido" — both are "send Ido the pre-meeting document(s)".
- "Schedule a call with Avi Perl" + "Set up an intro call with Avi Perl" — same call.

Do NOT flag genuinely-distinct tasks that merely share words or an owner:
- "Review the Q3 report" vs "Review the Q4 report" — different reports.
- "Email Bar Topper" vs "Call Bar Topper" — different actions.
- Two different documents/deliverables that both matter are NOT a duplicate.

Be CONSERVATIVE — when unsure, do NOT flag. Never flag an item against itself.

Return ONLY JSON:
{{"duplicates": [{{"a": <item number>, "b": <item number>, "reason": "<short: the one shared action>"}}]}}
If there are no duplicates, return {{"duplicates": []}}."""

    try:
        text, _ = call_llm(
            prompt=prompt,
            model=settings.model_background,  # Sonnet — real judgement, not Haiku
            max_tokens=1024,
            call_site="task_dup_flag",
            meeting_id=meeting_id,
        )
    except Exception as e:
        logger.warning(f"detect_duplicate_task_pairs LLM call failed (non-fatal): {e}")
        return []

    n = len(tasks)
    seen: set[tuple[int, int]] = set()
    pairs: list[dict] = []
    for d in _parse(text).get("duplicates", []):
        a, b = d.get("a"), d.get("b")
        if not isinstance(a, int) or not isinstance(b, int):
            continue
        if a == b or not (1 <= a <= n) or not (1 <= b <= n):
            continue  # drop hallucinated / self / out-of-range indices
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)
        pairs.append({"a": key[0], "b": key[1], "reason": (d.get("reason") or "").strip()})
    return pairs


def format_duplicate_flag(pairs: list[dict], tasks: list[dict]) -> str:
    """Render the suspected-duplicate pairs as a short approval-card banner.
    Empty string when there's nothing to flag."""
    if not pairs:
        return ""
    lines = ["⚠️ <b>Possible duplicate action items — review before approving:</b>"]
    for p in pairs:
        a, b = p["a"], p["b"]
        ta = (tasks[a - 1].get("title") or "")[:48]
        tb = (tasks[b - 1].get("title") or "")[:48]
        reason = p.get("reason") or "look like the same task"
        lines.append(f"• #{a} “{ta}” + #{b} “{tb}” — {reason}")
    lines.append("<i>Merge or drop one with an edit, or approve as-is if they're distinct.</i>")
    return "\n".join(lines)
