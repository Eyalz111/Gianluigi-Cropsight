"""
Knowledge synthesis (v2.5 PR2) — cold-start + reusable brief generation.

Builds the living briefs that are the spine of the knowledge foundation:
- A TopicBrief per topic_thread (Sonnet), synthesized from its mentions, the
  source meetings' summaries, and (best-effort) top-k semantic chunks.
- An AreaBrief per Area (Opus), aggregating its child topic briefs.

Sensitivity follows data (#6): each fact is tagged with the tier of the source
meeting it came from — briefs are NOT collapsed to a single max tier, so a
FOUNDERS view can later be rendered by filtering out CEO-tier facts. The
brief-level `sensitivity` is the max across facts, used only as a quick gate.

This module is inert until invoked (by scripts/synthesize_initial_briefs.py for
the one-shot cold start, or the weekly scheduler in a later PR). It never runs
on the request hot path.
"""

import json
import logging
import re
from datetime import datetime, timezone

from config.settings import settings
from core.llm import call_llm
from models.schemas import (
    AreaBrief,
    BriefCitation,
    BriefFact,
    LastDecision,
    OpenItem,
    Sensitivity,
    TopicBrief,
    TopicStatus,
)
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_SENS_ORDER = {"public": 1, "team": 2, "founders": 3, "ceo": 4}
_MAX_MENTIONS = 15
_SUMMARY_CAP = 600
_RAG_CHUNKS = 6


def _to_sensitivity(value: str | None) -> Sensitivity:
    """Coerce a raw tier string to the Sensitivity enum (default FOUNDERS)."""
    try:
        return Sensitivity((value or "founders").lower())
    except ValueError:
        return Sensitivity.FOUNDERS


def _max_sensitivity(tiers: list[Sensitivity]) -> Sensitivity:
    """Most-restrictive tier in a list (FOUNDERS if empty)."""
    if not tiers:
        return Sensitivity.FOUNDERS
    return max(tiers, key=lambda s: _SENS_ORDER.get(s.value, 3))


def _parse_json(response: str) -> dict | None:
    """Extract a JSON object from an LLM response, tolerating code fences."""
    if not response:
        return None
    for candidate in (response,):
        try:
            return json.loads(candidate)
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


# =============================================================================
# Topic briefs (Sonnet)
# =============================================================================

def _build_topic_prompt(topic_name: str, sources: list[dict], chunks: list[str]) -> str:
    """Assemble the Sonnet synthesis prompt for one topic.

    `sources` is an ordered, de-duplicated list of source meetings with an
    `idx` the model can cite in each fact (tiers are NOT shown to the model —
    they're applied internally afterwards).
    """
    src_lines = []
    for s in sources:
        ctx = (s.get("context") or "").strip()
        summ = (s.get("summary") or "").strip()[:_SUMMARY_CAP]
        decs = "; ".join(s.get("decisions_made") or [])
        src_lines.append(
            f"[{s['idx']}] {s.get('title','(untitled)')} ({s.get('date','')[:10]})\n"
            f"    context: {ctx or '(none)'}\n"
            f"    decisions: {decs or '(none)'}\n"
            f"    summary: {summ or '(none)'}"
        )
    sources_block = "\n".join(src_lines) if src_lines else "(no source meetings)"
    chunks_block = "\n".join(f"  - {c[:400]}" for c in chunks) if chunks else "(none)"

    return f"""You maintain a living brief for a CropSight topic, synthesizing everything known about it.

Topic: {topic_name}

Source meetings (cite the [index] in each fact's "source"):
{sources_block}

Additional retrieved context (may be noisy — use only if clearly relevant):
{chunks_block}

Produce a current-state brief. Return ONLY valid JSON of this shape:

{{
  "narrative": "3-5 sentence current-state summary (where this topic stands now)",
  "current_status": "active" | "blocked" | "pending_decision" | "stale" | "closed",
  "key_facts": [
    {{"text": "a durable fact / milestone / decision about this topic", "source": <source index int or null>}}
  ],
  "open_items": [
    {{"kind": "task" | "question" | "blocker", "description": "...", "owner": "name or null"}}
  ],
  "stakeholders": ["people actively involved"],
  "recent_decisions": [
    {{"text": "...", "date": "YYYY-MM-DD", "meeting_title": "..."}}
  ],
  "risks": ["risks or blockers worth surfacing"],
  "next_actions": ["concrete next steps"]
}}

Rules:
- Ground every key_fact in a source; set "source" to the [index] it came from (or null if synthesized across many).
- current_status: 'blocked' if waiting on an external action, 'pending_decision' if an open decision dominates, 'stale' if no recent activity, 'closed' if resolved, else 'active'.
- Keep narrative concise and factual. No speculation.
- Return ONLY the JSON object — no prose, no code fences."""


def _assemble_topic_brief(data: dict, sources: list[dict]) -> TopicBrief:
    """Turn parsed LLM JSON + source tiers into a validated TopicBrief.

    Each fact inherits the sensitivity of its cited source meeting (per-fact
    tagging). The brief-level sensitivity is the max across facts/sources.
    """
    by_idx = {s["idx"]: s for s in sources}
    source_tiers = [_to_sensitivity(s.get("tier")) for s in sources]

    facts: list[BriefFact] = []
    for f in data.get("key_facts", []) or []:
        text = (f.get("text") or "").strip()
        if not text:
            continue
        src = by_idx.get(f.get("source"))
        tier = _to_sensitivity(src.get("tier")) if src else _max_sensitivity(source_tiers)
        citation = None
        if src:
            citation = BriefCitation(
                source_type="meeting",
                source_id=src.get("id"),
                meeting_title=src.get("title"),
                date=(src.get("date") or "")[:10] or None,
                sensitivity=tier,
            )
        facts.append(BriefFact(text=text, sensitivity=tier, citation=citation))

    open_items = [
        OpenItem(
            kind=oi.get("kind", "task"),
            description=oi.get("description", ""),
            owner=oi.get("owner"),
        )
        for oi in (data.get("open_items") or [])
        if oi.get("description")
    ]
    recent_decisions = [
        LastDecision(
            text=d.get("text", ""),
            date=(d.get("date") or "")[:10] or "",
            meeting_title=d.get("meeting_title"),
        )
        for d in (data.get("recent_decisions") or [])
        if d.get("text")
    ]
    citations = [
        BriefCitation(
            source_type="meeting",
            source_id=s.get("id"),
            meeting_title=s.get("title"),
            date=(s.get("date") or "")[:10] or None,
            sensitivity=_to_sensitivity(s.get("tier")),
        )
        for s in sources
    ]

    try:
        status = TopicStatus(data.get("current_status", "active"))
    except ValueError:
        status = TopicStatus.ACTIVE

    return TopicBrief(
        narrative=(data.get("narrative") or "").strip(),
        facts=facts,
        current_status=status,
        open_items=open_items,
        stakeholders=[s for s in (data.get("stakeholders") or []) if s],
        recent_decisions=recent_decisions,
        risks=[r for r in (data.get("risks") or []) if r],
        next_actions=[a for a in (data.get("next_actions") or []) if a],
        citations=citations,
        sensitivity=_max_sensitivity(source_tiers),
        last_synthesized_at=datetime.now(timezone.utc).isoformat(),
        version=1,
    )


def _gather_topic_sources(topic_id: str) -> list[dict]:
    """Build the ordered, de-duplicated source-meeting list for a topic."""
    mentions = (
        supabase_client.client.table("topic_thread_mentions")
        .select("meeting_id, context, decisions_made, status_at_mention, created_at")
        .eq("topic_id", topic_id)
        .order("created_at", desc=False)
        .execute()
        .data
        or []
    )
    mentions = mentions[:_MAX_MENTIONS]
    meeting_ids = [m["meeting_id"] for m in mentions if m.get("meeting_id")]
    meetings_by_id: dict[str, dict] = {}
    if meeting_ids:
        rows = (
            supabase_client.client.table("meetings")
            .select("id, title, date, summary, sensitivity")
            .in_("id", meeting_ids)
            .execute()
            .data
            or []
        )
        meetings_by_id = {r["id"]: r for r in rows}

    sources: list[dict] = []
    seen: set[str] = set()
    for m in mentions:
        mid = m.get("meeting_id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        meeting = meetings_by_id.get(mid, {})
        sources.append({
            "idx": len(sources),
            "id": mid,
            "title": meeting.get("title", "(untitled)"),
            "date": str(meeting.get("date", "")),
            "summary": meeting.get("summary", ""),
            "tier": meeting.get("sensitivity", "founders"),
            "context": m.get("context", ""),
            "decisions_made": m.get("decisions_made") or [],
        })
    return sources


async def _rag_chunks(topic_name: str) -> list[str]:
    """Best-effort top-k semantic chunks for the topic. Never raises."""
    try:
        from services.embeddings import embedding_service

        emb = await embedding_service.embed_text(topic_name)
        hits = supabase_client.search_embeddings(emb, limit=_RAG_CHUNKS, source_type="meeting")
        return [h.get("chunk_text", "") for h in hits if h.get("chunk_text")]
    except Exception as e:
        logger.warning(f"[synthesis] RAG enrichment skipped for '{topic_name}': {e}")
        return []


async def synthesize_topic_brief(topic: dict, use_rag: bool = True) -> dict | None:
    """Synthesize a TopicBrief for one topic_threads row. Returns brief_json or None."""
    topic_id = topic.get("id")
    topic_name = topic.get("topic_name", "")
    if not topic_id:
        return None
    try:
        sources = _gather_topic_sources(topic_id)
        if not sources:
            logger.info(f"[synthesis] topic '{topic_name}' has no source meetings — skipping")
            return None
        chunks = await _rag_chunks(topic_name) if use_rag else []

        prompt = _build_topic_prompt(topic_name, sources, chunks)
        response, _usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,  # Sonnet — per-topic synthesis
            max_tokens=2000,
            call_site="topic_brief_synthesis",
        )
        data = _parse_json(response)
        if not data:
            logger.warning(
                f"[synthesis] malformed topic-brief JSON for '{topic_name}'; "
                f"raw head: {response[:200]!r}"
            )
            return None

        brief = _assemble_topic_brief(data, sources).model_dump(mode="json")
        return brief
    except Exception as e:
        logger.warning(f"[synthesis] synthesize_topic_brief failed for '{topic_name}': {e}")
        return None


# =============================================================================
# Area briefs (Opus)
# =============================================================================

def _build_area_prompt(area_name: str, topic_briefs: list[dict]) -> str:
    lines = []
    for tb in topic_briefs:
        name = tb.get("_topic_name", "(topic)")
        status = tb.get("current_status", "active")
        narrative = (tb.get("narrative") or "").strip()[:400]
        lines.append(f"- {name} [{status}]: {narrative}")
    topics_block = "\n".join(lines) if lines else "(no topics yet)"

    return f"""You write the strategic brief for a CropSight Area (a workstream that groups several topics).

Area: {area_name}

Child topics (name [status]: current-state):
{topics_block}

Synthesize the Area's strategic state. Return ONLY valid JSON of this shape:

{{
  "narrative": "3-5 sentence strategic summary of where this whole area stands",
  "strategic_state": "one-line headline status for the area",
  "topic_summaries": ["one short line per topic worth surfacing"],
  "cross_topic_patterns": ["patterns, dependencies, or risks that span topics"],
  "key_facts": ["durable facts about the area"]
}}

Rules:
- Be concrete and factual; surface blockers and dependencies.
- Return ONLY the JSON object — no prose, no code fences."""


def _assemble_area_brief(data: dict, child_tiers: list[Sensitivity]) -> AreaBrief:
    tier = _max_sensitivity(child_tiers)
    facts = [
        BriefFact(text=t, sensitivity=tier)
        for t in (data.get("key_facts") or [])
        if t
    ]
    return AreaBrief(
        narrative=(data.get("narrative") or "").strip(),
        topic_summaries=[s for s in (data.get("topic_summaries") or []) if s],
        cross_topic_patterns=[p for p in (data.get("cross_topic_patterns") or []) if p],
        strategic_state=(data.get("strategic_state") or "").strip(),
        facts=facts,
        citations=[],
        sensitivity=tier,
        last_synthesized_at=datetime.now(timezone.utc).isoformat(),
        version=1,
    )


async def synthesize_area_brief(area: dict, topic_briefs: list[dict]) -> dict | None:
    """Synthesize an AreaBrief from already-synthesized child topic briefs."""
    area_name = area.get("name", "")
    try:
        child_tiers = [_to_sensitivity(tb.get("sensitivity")) for tb in topic_briefs]
        prompt = _build_area_prompt(area_name, topic_briefs)
        response, _usage = call_llm(
            prompt=prompt,
            model=settings.model_extraction,  # Opus — cross-topic strategic synthesis
            max_tokens=2500,
            call_site="area_brief_synthesis",
        )
        data = _parse_json(response)
        if not data:
            logger.warning(f"[synthesis] malformed area-brief JSON for '{area_name}'")
            return None
        return _assemble_area_brief(data, child_tiers).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"[synthesis] synthesize_area_brief failed for '{area_name}': {e}")
        return None


# =============================================================================
# Orchestration (one-shot cold start)
# =============================================================================

async def synthesize_all_topics(
    limit: int | None = None,
    force: bool = False,
    use_rag: bool = True,
    dry_run: bool = False,
) -> dict:
    """Synthesize TopicBriefs for all topics missing one (or all, if force)."""
    topics = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, area_id, brief_json")
        .execute()
        .data
        or []
    )
    pending = [t for t in topics if force or not t.get("brief_json")]
    if limit is not None:
        pending = pending[:limit]

    done, skipped, failed = 0, 0, 0
    for t in pending:
        brief = await synthesize_topic_brief(t, use_rag=use_rag)
        if not brief:
            failed += 1
            continue
        if dry_run:
            logger.info(f"[synthesis][dry-run] topic '{t.get('topic_name')}' -> {brief.get('current_status')}")
        else:
            supabase_client.update_topic_brief(t["id"], brief)
        done += 1
    skipped = len(topics) - len(pending)
    result = {"topics_total": len(topics), "synthesized": done, "skipped": skipped, "failed": failed}
    logger.info(f"[synthesis] topics complete: {result}")
    return result


async def synthesize_all_areas(force: bool = False, dry_run: bool = False) -> dict:
    """Synthesize AreaBriefs by aggregating child topic briefs."""
    areas = supabase_client.get_areas()
    done, failed = 0, 0
    for area in areas:
        if not force and area.get("brief_json"):
            continue
        child_topics = (
            supabase_client.client.table("topic_threads")
            .select("topic_name, brief_json")
            .eq("area_id", area["id"])
            .execute()
            .data
            or []
        )
        topic_briefs = []
        for ct in child_topics:
            bj = ct.get("brief_json")
            if bj:
                bj = dict(bj)
                bj["_topic_name"] = ct.get("topic_name", "")
                topic_briefs.append(bj)
        brief = await synthesize_area_brief(area, topic_briefs)
        if not brief:
            failed += 1
            continue
        if dry_run:
            logger.info(f"[synthesis][dry-run] area '{area.get('name')}' -> {brief.get('strategic_state')}")
        else:
            supabase_client.update_area_brief(area["id"], brief)
        done += 1
    result = {"areas_total": len(areas), "synthesized": done, "failed": failed}
    logger.info(f"[synthesis] areas complete: {result}")
    return result


# =============================================================================
# Weekly synthesis + reflection (v2.5 PR9)
# =============================================================================

def _build_reflection() -> dict:
    """Surface topics needing attention (blocked / stale) for the morning brief."""
    rows = (
        supabase_client.client.table("topic_threads")
        .select("topic_name, brief_json")
        .not_.is_("brief_json", "null")
        .execute()
        .data
        or []
    )
    blocked, stale = [], []
    for t in rows:
        status = (t.get("brief_json") or {}).get("current_status")
        if status == "blocked":
            blocked.append(t.get("topic_name"))
        elif status == "stale":
            stale.append(t.get("topic_name"))
    return {
        "blocked": blocked[:10],
        "stale": stale[:10],
        "blocked_count": len(blocked),
        "stale_count": len(stale),
    }


async def run_weekly_synthesis(days: int = 7) -> dict:
    """
    Weekly deep pass: re-synthesize recently-active topic briefs from history,
    refresh all area briefs, and log a reflection of topics needing attention.

    Bounded to topics with activity in the last `days` (cost control). Closed
    topics are skipped.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, area_id, brief_json, last_updated, status")
        .gte("last_updated", cutoff)
        .execute()
        .data
        or []
    )

    resynth = 0
    for t in rows:
        if (t.get("status") or "active") == "closed":
            continue
        brief = await synthesize_topic_brief(t, use_rag=True)
        if brief:
            supabase_client.update_topic_brief(t["id"], brief)
            resynth += 1

    areas_result = await synthesize_all_areas(force=True)
    reflection = _build_reflection()
    try:
        supabase_client.log_action("knowledge_reflection", details=reflection, triggered_by="auto")
    except Exception:
        pass

    summary = {"resynthesized_topics": resynth, "areas": areas_result, "reflection": reflection}
    logger.info(f"[weekly] synthesis complete: {summary}")
    return summary
