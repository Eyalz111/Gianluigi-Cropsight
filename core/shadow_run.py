"""
Shadow-run helpers for the v2.5 knowledge foundation.

During shadow mode (settings.KNOWLEDGE_SHADOW_MODE), new knowledge passes
(read-back extraction, completeness check, nightly/weekly synthesis) compute
their output and LOG a comparison to audit_log WITHOUT altering the shipped
result or the live read paths. This lets us measure quality + cost/latency
over >=10 meetings before flipping the cutover flags
(KNOWLEDGE_READBACK_ENABLED / EXTRACTION_MUZZLE_REMOVED).

Review accumulated diffs via the [KNOWLEDGE] get_shadow_diff_summary MCP tool,
which aggregates the audit_log rows written here.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _task_titles(extraction: dict | None) -> set[str]:
    """Normalized set of task titles from an extraction result dict."""
    return {
        (t.get("title") or "").strip().lower()
        for t in (extraction or {}).get("tasks", [])
        if (t.get("title") or "").strip()
    }


def diff_extractions(live: dict | None, shadow: dict | None) -> dict:
    """
    Compare a baseline ('live', what we ship) extraction against a 'shadow'
    (knowledge-augmented) extraction. Returns a compact, scannable diff.

    tasks_added  = shadow surfaced these but live missed them (the upside).
    tasks_removed = live had these but shadow dropped them (regression to watch).
    """
    live_titles = _task_titles(live)
    shadow_titles = _task_titles(shadow)

    def _count(d: dict | None, key: str) -> int:
        return len((d or {}).get(key, []))

    return {
        "tasks_added": sorted(shadow_titles - live_titles),
        "tasks_removed": sorted(live_titles - shadow_titles),
        "task_count_live": len(live_titles),
        "task_count_shadow": len(shadow_titles),
        "decisions_live": _count(live, "decisions"),
        "decisions_shadow": _count(shadow, "decisions"),
        "open_questions_live": _count(live, "open_questions"),
        "open_questions_shadow": _count(shadow, "open_questions"),
    }


def log_shadow(
    pass_name: str,
    live: Any = None,
    shadow: Any = None,
    meeting_id: str | None = None,
    cost_usd: float | None = None,
    latency_s: float | None = None,
    extra: dict | None = None,
) -> None:
    """
    Record a shadow comparison to audit_log as 'shadow_<pass_name>'.

    Never raises — shadow logging must not break the pipeline it observes.
    For extraction-style passes, live/shadow are extraction dicts and we store
    a compact diff; for other passes (synthesis, consolidation), pass a summary
    dict via `extra`.
    """
    try:
        from services.supabase_client import supabase_client

        details: dict[str, Any] = {"meeting_id": meeting_id}
        if isinstance(live, dict) and isinstance(shadow, dict):
            details["diff"] = diff_extractions(live, shadow)
        if cost_usd is not None:
            details["cost_usd"] = round(cost_usd, 4)
        if latency_s is not None:
            details["latency_s"] = round(latency_s, 2)
        if extra:
            details.update(extra)

        supabase_client.log_action(
            action=f"shadow_{pass_name}",
            details=details,
            triggered_by="auto",
        )
        logger.info(
            f"[SHADOW] {pass_name} logged (meeting={meeting_id}, "
            f"cost={cost_usd}, latency={latency_s})"
        )
    except Exception as e:
        logger.warning(f"shadow log failed for pass '{pass_name}': {e}")
