"""Forward-facing rich meeting summary (PR7 — SUMMARY_RICH_ENABLED).

Builds the executive-grade enrichment blocks the rich summary template renders:
an executive TL;DR, a Decision Intelligence block, per-area focus, risks &
blockers, and a cross-meeting "what changed since last time" delta. The renderer
(`core.system_prompt.format_summary`, rich=True) stays stateless — this module
gathers the (tier-safe) text it is handed.

Guardrails carried from the rest of the operational upgrade:
- **No invented facts/dates.** The TL;DR prompt is told to use only the provided
  items; on any LLM failure it falls back to a deterministic, fact-only line.
- **Tier safety.** Every block is filtered to the meeting's distribution tier —
  a decision/topic above the meeting tier is omitted, never leaked into a
  summary that a lower-tier recipient might see.
- **Never crashes the flow.** Each builder is defensive; the orchestrator is
  wrapped by the caller so a total failure leaves the baseline summary intact.
"""
import logging

logger = logging.getLogger(__name__)

# Tier ladder (mirrors schemas.TIER_LEVELS / filter_by_sensitivity). A missing
# or unknown tier defaults to founders (3) — the system's safe middle default.
_TIER_LEVELS = {"public": 1, "team": 2, "founders": 3, "ceo": 4}
_TIER_ALIASES = {
    "normal": "founders", "sensitive": "ceo", "ceo_only": "ceo",
    "restricted": "ceo", "legal": "ceo",
}


def _tier_level(sensitivity: str | None) -> int:
    """Numeric tier for a sensitivity string (default founders=3)."""
    s = (sensitivity or "").strip().lower()
    s = _TIER_ALIASES.get(s, s)
    return _TIER_LEVELS.get(s, 3)


def _confidence_label(confidence) -> str:
    """Map a 1-5 decision confidence to a short label."""
    try:
        n = int(confidence)
    except (TypeError, ValueError):
        return ""
    return {1: "low", 2: "low", 3: "medium", 4: "high", 5: "high"}.get(n, "")


# ---------------------------------------------------------------------------
# Executive TL;DR — LLM headline with a deterministic, fact-only fallback
# ---------------------------------------------------------------------------
def _format_tl_dr(text: str) -> str:
    """Wrap TL;DR text as a blockquote callout the rich template slots in at
    the top (it expects a block that already carries its leading newlines)."""
    quoted = "\n".join(f"> {ln}" for ln in text.splitlines() if ln.strip())
    if not quoted:
        return ""
    return f"\n\n> **🎯 TL;DR**\n{quoted}"


def _deterministic_tl_dr(extracted: dict) -> str:
    """A fact-only TL;DR: counts + the single highest-urgency next action. Used
    when the LLM is unavailable, so the headline never blocks or invents."""
    decisions = extracted.get("decisions") or []
    tasks = extracted.get("tasks") or []
    if not decisions and not tasks:
        return ""
    bits = []
    if decisions:
        bits.append(f"{len(decisions)} decision{'s' if len(decisions) != 1 else ''} recorded")
    nxt = ""
    if tasks:
        bits.append(f"{len(tasks)} action item{'s' if len(tasks) != 1 else ''}")
        rank = {"H": 0, "M": 1, "L": 2}
        top = sorted(tasks, key=lambda t: rank.get((t.get("urgency") or "M").upper(), 1))[0]
        nxt = f"Next: {(top.get('title') or '')[:80]} ({top.get('assignee') or 'TBD'})"
    text = "; ".join(bits) + (("\n" + nxt) if nxt else "")
    return _format_tl_dr(text)


async def build_tl_dr(meeting_title: str, extracted: dict) -> str:
    """Executive TL;DR: line 1 = most important outcome, line 2 = most important
    next action. LLM-headlined from the extracted facts (told never to invent),
    with the deterministic fallback on any failure."""
    fallback = _deterministic_tl_dr(extracted)
    decisions = extracted.get("decisions") or []
    tasks = extracted.get("tasks") or []
    if not decisions and not tasks:
        return fallback
    try:
        import asyncio
        from core.llm import call_llm
        from config.settings import settings

        dec_lines = [f"- decision: {(d.get('description') or '')[:120]}" for d in decisions[:5]]
        task_lines = [
            f"- task: {(t.get('title') or '')[:100]} "
            f"(owner {t.get('assignee') or 'TBD'}, urgency {t.get('urgency') or 'M'})"
            for t in tasks[:5]
        ]
        system = (
            "You write a 2-line executive TL;DR for the top of a meeting summary: "
            "line 1 = the single most important OUTCOME, line 2 = the single most "
            "important NEXT ACTION. Use ONLY the provided items — never invent facts, "
            "dates, owners, or numbers. No greeting, no emoji, no markdown headers."
        )
        prompt = f"Meeting: {meeting_title}\nItems:\n" + "\n".join(dec_lines + task_lines)

        def _run() -> str:
            text, _usage = call_llm(
                prompt=prompt, model=settings.model_simple, max_tokens=120,
                system=system, call_site="summary_tl_dr",
            )
            return text

        text = (await asyncio.to_thread(_run) or "").strip()
        return _format_tl_dr(text) if text else fallback
    except Exception as e:
        logger.debug(f"TL;DR LLM failed, using deterministic fallback: {e}")
        return fallback


# ---------------------------------------------------------------------------
# Decision Intelligence — rationale / options / confidence / supersession
# ---------------------------------------------------------------------------
def build_decision_intelligence(
    decisions: list[dict],
    supersession_clauses: dict[int, str] | None,
    meeting_sensitivity: str | None,
) -> str:
    """Surface the Phase-9A decision metadata that today's summary discards.
    Only renders decisions that actually carry intelligence (rationale, options,
    confidence, or a supersession). Tier-safe: a decision above the meeting tier
    is skipped. `supersession_clauses` is the tier-safe dict[1-based idx → str]
    from summary_context.build_supersession_clauses."""
    meeting_level = _tier_level(meeting_sensitivity)
    blocks = []
    for i, d in enumerate(decisions or [], 1):
        if _tier_level(d.get("sensitivity") or meeting_sensitivity) > meeting_level:
            continue
        rationale = (d.get("rationale") or "").strip()
        options = d.get("options_considered") or []
        confidence = d.get("confidence")
        clause = (supersession_clauses or {}).get(i, "").strip()
        if not (rationale or options or confidence or clause):
            continue
        desc = (d.get("description") or "")[:100]
        lines = [f"**{i}. {desc}**"]
        if clause:
            lines.append(f"  - {clause}")
        if rationale:
            lines.append(f"  - Rationale: {rationale}")
        if options and isinstance(options, list):
            opts = ", ".join(str(o) for o in options if str(o).strip())
            if opts:
                lines.append(f"  - Options weighed: {opts}")
        label = _confidence_label(confidence)
        if label:
            lines.append(f"  - Confidence: {label}")
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n\n## Decision Intelligence\n" + "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Per-Area Focus — group this meeting's action items by the Gantt area
# ---------------------------------------------------------------------------
def build_area_rollup(tasks: list[dict]) -> str:
    """Group the meeting's action items by area_label with an urgent-count flag.
    Deterministic; empty (no section) when there are no tasks."""
    if not tasks:
        return ""
    by_area: dict[str, dict] = {}
    for t in tasks:
        area = t.get("area_label") or "non-area"
        slot = by_area.setdefault(area, {"count": 0, "urgent": 0})
        slot["count"] += 1
        if (t.get("urgency") or "M").upper() == "H":
            slot["urgent"] += 1
    if not by_area:
        return ""
    rows = []
    for area, c in sorted(by_area.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
        urgent = f" ({c['urgent']} urgent)" if c["urgent"] else ""
        plural = "s" if c["count"] != 1 else ""
        rows.append(f"- **{area}**: {c['count']} action item{plural}{urgent}")
    return "\n\n## Per-Area Focus\n" + "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# Risks & Blockers — from the linked topic briefs (tier-safe)
# ---------------------------------------------------------------------------
def build_risks_blockers(
    linked_threads: list[dict] | None,
    meeting_sensitivity: str | None,
) -> str:
    """Pull risks + blocked status + blocker open-items off the linked TopicBriefs.
    Tier-safe (skips briefs above the meeting tier); deduped; empty when none."""
    meeting_level = _tier_level(meeting_sensitivity)
    items: list[str] = []
    for th in linked_threads or []:
        brief = th.get("brief_json") or {}
        if not isinstance(brief, dict):
            continue
        if _tier_level(brief.get("sensitivity")) > meeting_level:
            continue
        name = th.get("topic_name") or "topic"
        for r in brief.get("risks") or []:
            if r and str(r).strip():
                items.append(f"- ⚠️ {name}: {str(r).strip()}")
        if brief.get("current_status") == "blocked":
            items.append(f"- 🔴 {name}: blocked")
        for oi in brief.get("open_items") or []:
            if isinstance(oi, dict) and oi.get("kind") == "blocker":
                txt = oi.get("text") or oi.get("description") or "blocker"
                items.append(f"- 🔴 {name}: {str(txt).strip()}")
    seen, uniq = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    if not uniq:
        return ""
    return "\n\n## Risks & Blockers\n" + "\n".join(uniq) + "\n"


# ---------------------------------------------------------------------------
# What Changed Since Last Time — cross-meeting delta for these participants
# ---------------------------------------------------------------------------
def build_changed_since(
    participants: list[str],
    current_meeting_id: str | None,
    max_sensitivity_level: int,
) -> str:
    """Wrap meeting_continuity's prior-context block as a forward-facing
    'what changed' section. Tier-bounded by max_sensitivity_level; empty when
    there's no prior context."""
    try:
        from processors.meeting_continuity import build_meeting_continuity_context
        ctx = build_meeting_continuity_context(
            participants, current_meeting_id, max_sensitivity_level
        )
    except Exception as e:
        logger.debug(f"changed-since context failed (non-fatal): {e}")
        return ""
    if not ctx or not ctx.strip():
        return ""
    return "\n\n## What Changed Since Last Time\n" + ctx.strip() + "\n"


# ---------------------------------------------------------------------------
# Orchestrator — gather every block (tier-safe) and render the rich summary
# ---------------------------------------------------------------------------
async def build_rich_summary(
    *,
    meeting_title: str,
    meeting_date: str,
    participants: list[str],
    duration_minutes: int,
    sensitivity: str,
    extracted: dict,
    meeting_id: str | None,
    supersessions: list[dict] | None = None,
    linked_threads: list[dict] | None = None,
) -> str | None:
    """Build the full rich summary string (or None on a hard failure). Mirrors
    the base-render args from transcript_processor Step 6 so the only delta is
    the enrichment. Supersession clauses are surfaced ONLY in the Decision
    Intelligence block (decision_context=None into the renderer) so a clause is
    never printed twice."""
    from core.system_prompt import format_summary
    from processors.summary_context import build_supersession_clauses, build_topic_context

    decisions = extracted.get("decisions", []) or []
    tasks = extracted.get("tasks", []) or []
    meeting_level = _tier_level(sensitivity)

    try:
        decision_clauses = build_supersession_clauses(decisions, supersessions or [], sensitivity)
    except Exception as e:
        logger.debug(f"supersession clauses failed (non-fatal): {e}")
        decision_clauses = {}
    try:
        topic_context = build_topic_context(linked_threads or [], sensitivity)
    except Exception as e:
        logger.debug(f"topic context failed (non-fatal): {e}")
        topic_context = None

    tl_dr = await build_tl_dr(meeting_title, extracted)
    decision_intel = build_decision_intelligence(decisions, decision_clauses, sensitivity)
    area_rollup = build_area_rollup(tasks)
    risks = build_risks_blockers(linked_threads or [], sensitivity)
    changed = build_changed_since(participants, meeting_id, meeting_level)

    return format_summary(
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        participants=participants,
        duration_minutes=duration_minutes,
        sensitivity=sensitivity,
        decisions=decisions,
        tasks=tasks,
        follow_ups=extracted.get("follow_ups", []) or [],
        open_questions=extracted.get("open_questions", []) or [],
        discussion_summary=extracted.get("discussion_summary", "") or "",
        stakeholders_mentioned=extracted.get("stakeholders", []) or [],
        decision_context=None,          # supersession is in the intel block, not inline
        topic_context=topic_context,
        rich=True,
        tl_dr=tl_dr,
        decision_intelligence=decision_intel,
        area_rollup=area_rollup,
        risks_text=risks,
        changed_since=changed,
    )
