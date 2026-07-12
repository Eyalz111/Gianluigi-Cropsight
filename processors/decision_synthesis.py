"""Weekly decision synthesis (narratives) — the LLM half.

Mirrors processors/knowledge_synthesis.py (topic briefs), but for decisions. A
weekly Sonnet pass writes an evolving `narrative` onto each recently-active
decision's brief_json — what was decided, why, how it evolved through any
supersessions, and its status today — grounded in the decision + its supersession
chain + related decisions.

Discipline (locked): this ONLY writes brief_json.narrative + related. It never
writes a refined summary/rationale back to the decision row — the deterministic
`build_decision_brief` owns every structural field, and any field change must go
through the propose-don't-clobber rail (decision_intelligence.propose_or_update_
decision_field). A malformed LLM response returns None and does NOT clobber the
deterministic base. Gated by settings.DECISION_SYNTHESIS_ENABLED (via the
knowledge-weekly scheduler).
"""

import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings
from core.llm import call_llm
from models.schemas import DecisionBrief
from services.supabase_client import supabase_client
# Reuse the topic-synthesis helpers (DRY — same JSON tolerance + tier ordering).
from processors.knowledge_synthesis import _parse_json, _SENS_ORDER
from processors.decision_intelligence import build_decision_brief

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DECISIONS = 40   # hard cost cap per weekly run (~40 Sonnet calls vs ~230 total)


def _build_decision_prompt(decision: dict, chain: list[dict], related: list[dict]) -> str:
    """Sonnet prompt for one decision's living narrative (single-key JSON out)."""
    def _line(d: dict) -> str:
        date = str(d.get("created_at", ""))[:10]
        status = d.get("decision_status", "active")
        return f"- ({date}) [{status}] {(d.get('description') or '').strip()}"

    chain_txt = "\n".join(_line(c) for c in chain) if chain else "(no prior versions)"
    related_txt = (
        "\n".join(f"- {(r.get('description') or '').strip()}" for r in related)
        if related else "(none)"
    )
    status = decision.get("decision_status", "active")
    desc = (decision.get("description") or "").strip()
    rationale = (decision.get("rationale") or "").strip() or "(none)"

    return f"""You maintain a living brief for a single CropSight DECISION — its current state and how it got here.

DECISION (current):
[{status}] {desc}
Rationale: {rationale}

SUPERSESSION HISTORY (oldest -> newest; how this decision evolved):
{chain_txt}

RELATED DECISIONS (linked in the knowledge graph; context only, do NOT merge them in):
{related_txt}

Write the decision's current-state narrative. Return ONLY valid JSON:
{{"narrative": "2-4 sentence prose: what was decided, the reasoning, how it evolved through any supersessions, and its status today"}}

Rules:
- Ground STRICTLY in the decision + its history above. No speculation, no new facts.
- If superseded/reversed, say so and name what replaced it (from the history).
- FOUNDERS-safe professional prose. Return ONLY the JSON object — no code fences, no extra prose."""


def _assemble_decision_brief(base: dict, data: dict, related_ids: list[str]) -> dict:
    """Overlay ONLY narrative + related onto the deterministic base brief; validate."""
    merged = dict(base)
    merged["narrative"] = (data.get("narrative") or "").strip()
    merged["related"] = related_ids
    merged["last_synthesized_at"] = datetime.now(timezone.utc).isoformat()
    merged["version"] = int(base.get("version", 1) or 1)
    return DecisionBrief(**merged).model_dump(mode="json")


async def synthesize_decision_brief(decision: dict) -> dict | None:
    """LLM-enrich one decision's brief narrative. Returns brief_json or None (never raises)."""
    did = decision.get("id")
    if not did:
        return None
    try:
        # Deterministic base first — recomputes every structural field + persists,
        # so the LLM and deterministic layers can never diverge.
        base = build_decision_brief(did)
        if not base:
            return None

        tier_level = _SENS_ORDER.get((decision.get("sensitivity") or "founders").lower(), 3)
        chain = supabase_client.get_decision_chain(did) or []
        related = supabase_client.get_related_decisions(did, ("relates_to",)) or []
        # Sensitivity ceiling [audit P2-09]: an input above this decision's own tier
        # must not bleed into its narrative.
        chain_f = [c for c in chain
                   if _SENS_ORDER.get((c.get("sensitivity") or "founders").lower(), 3) <= tier_level]
        related_f = [r for r in related
                     if _SENS_ORDER.get((r.get("sensitivity") or "founders").lower(), 3) <= tier_level]

        prompt = _build_decision_prompt(decision, chain_f, related_f)
        response, _usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,   # Sonnet — matches topic-brief synthesis tier
            max_tokens=1200,
            call_site="decision_brief_synthesis",
        )
        data = _parse_json(response)
        if not data:
            logger.warning(
                f"[decision-synthesis] malformed narrative JSON for {did}; "
                f"raw head: {(response or '')[:150]!r}"
            )
            return None  # do NOT clobber the deterministic base

        brief = _assemble_decision_brief(base, data, [r["id"] for r in related_f if r.get("id")])
        supabase_client.update_decision(did, brief_json=brief)
        return brief
    except Exception as e:
        logger.warning(f"[decision-synthesis] synthesize_decision_brief failed for {did}: {e}")
        return None


async def run_decision_synthesis(days: int = 7, max_decisions: int = _DEFAULT_MAX_DECISIONS) -> dict:
    """Weekly pass: enrich the narrative of each recently-active decision (bounded).

    Selects active+approved+current decisions referenced OR created within `days`
    (the temporal inverse of get_stale_decisions), hard-capped at max_decisions for
    cost. Returns {synthesized, candidates, capped}.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sc = supabase_client
    rows_by_id: dict = {}
    try:
        # (a) referenced within the window
        q1 = (
            sc.client.table("decisions").select("*")
            .eq("decision_status", "active").eq("approval_status", "approved")
            .is_("valid_to", "null").gte("last_referenced_at", cutoff)
            .limit(max_decisions * 3).execute().data or []
        )
        # (b) never referenced but created within the window
        q2 = (
            sc.client.table("decisions").select("*")
            .eq("decision_status", "active").eq("approval_status", "approved")
            .is_("valid_to", "null").is_("last_referenced_at", "null").gte("created_at", cutoff)
            .limit(max_decisions * 3).execute().data or []
        )
        for d in (q1 + q2):
            if d.get("id"):
                rows_by_id[d["id"]] = d
    except Exception as e:
        logger.error(f"[decision-synthesis] selection query failed: {e}")
        return {"error": str(e)}

    candidates = list(rows_by_id.values())
    capped = len(candidates) > max_decisions
    selected = candidates[:max_decisions]
    synthesized = 0
    for d in selected:
        if await synthesize_decision_brief(d):
            synthesized += 1
    logger.info(
        f"[decision-synthesis] synthesized {synthesized}/{len(candidates)} decisions (capped={capped})"
    )
    return {"synthesized": synthesized, "candidates": len(candidates), "capped": capped}
