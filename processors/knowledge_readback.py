"""
Read-back context for extraction (v2.5 PR3).

Before analyzing a new meeting, assemble the relevant accumulated knowledge —
the living topic briefs most related to this meeting, plus semantically similar
past discussion — so extraction can classify UPDATE-vs-NEW, detect
supersessions, and avoid re-surfacing closed items. This closes the loop that
made the system feel like "standalone analysis."

Sensitivity follows data (hard constraint): only knowledge at or below the
current meeting's tier is injected, so higher-sensitivity context never bleeds
into a summary that may distribute more broadly. Unknown tier => most
restrictive (excluded unless the meeting itself is CEO-tier).

Null-safe: returns None when there's nothing relevant (e.g. before cold-start
synthesis has populated any briefs). Never raises.
"""

import logging
import re

logger = logging.getLogger(__name__)

_SENS_LEVEL = {"public": 1, "team": 2, "founders": 3, "ceo": 4}
_STOP = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "meeting", "sync", "call"}
_DEFAULT_BUDGET = 3600  # ~60% of a ~6000-char knowledge+continuity budget (#7)


def _level(tier: str | None) -> int:
    """Sensitivity level; unknown/missing => most restrictive (4)."""
    return _SENS_LEVEL.get((tier or "ceo").lower(), 4)


def _words(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    return {w for w in cleaned.split() if w and w not in _STOP}


def _relevant_topic_briefs(meeting_title: str, participants: list[str], meeting_level: int) -> list[str]:
    """Top topic briefs whose name overlaps the meeting, filtered by tier."""
    from services.supabase_client import supabase_client

    rows = (
        supabase_client.client.table("topic_threads")
        .select("topic_name, brief_json")
        .not_.is_("brief_json", "null")
        .execute()
        .data
        or []
    )
    query_words = _words(meeting_title) | {p.lower() for p in participants if p}
    scored: list[tuple[int, str, dict]] = []
    for r in rows:
        brief = r.get("brief_json") or {}
        if _level(brief.get("sensitivity")) > meeting_level:
            continue  # too sensitive for this meeting's audience
        overlap = len(query_words & _words(r.get("topic_name", "")))
        if overlap > 0:
            scored.append((overlap, r.get("topic_name", ""), brief))
    scored.sort(key=lambda x: -x[0])

    lines = []
    for _, name, brief in scored[:3]:
        narrative = (brief.get("narrative") or "").strip()[:400]
        status = brief.get("current_status", "")
        open_items = "; ".join(
            (oi.get("description") or "") for oi in (brief.get("open_items") or [])[:3]
        )
        lines.append(
            f"Topic '{name}' [{status}]: {narrative} "
            f"Open items: {open_items or '(none)'}"
        )
    return lines


async def _relevant_chunks(meeting_title: str, meeting_level: int) -> list[str]:
    """Top semantic chunks for the meeting title, filtered by source-meeting tier."""
    from services.embeddings import embedding_service
    from services.supabase_client import supabase_client

    emb = await embedding_service.embed_text(meeting_title)
    hits = supabase_client.search_embeddings(emb, limit=8, source_type="meeting")

    # match_embeddings does not return sensitivity — derive it from the source meeting.
    src_ids = list({h.get("source_id") for h in hits if h.get("source_id")})
    tiers: dict[str, str] = {}
    if src_ids:
        rows = (
            supabase_client.client.table("meetings")
            .select("id, sensitivity")
            .in_("id", src_ids)
            .execute()
            .data
            or []
        )
        tiers = {r["id"]: r.get("sensitivity") for r in rows}

    out = []
    for h in hits:
        if _level(tiers.get(h.get("source_id"))) > meeting_level:
            continue
        txt = (h.get("chunk_text") or "").strip()[:200]
        if txt:
            out.append(txt)
    return out[:5]


async def build_knowledge_context(
    meeting_title: str,
    participants: list[str],
    sensitivity: str,
    budget_chars: int = _DEFAULT_BUDGET,
) -> str | None:
    """
    Assemble the read-back knowledge block for this meeting, or None if empty.

    Sensitivity-filtered to the meeting's tier; budget-capped. Never raises.
    """
    meeting_level = _level(sensitivity)
    sections: list[str] = []

    try:
        briefs = _relevant_topic_briefs(meeting_title, participants, meeting_level)
        if briefs:
            sections.append("RELEVANT TOPIC BRIEFS (current state):\n" + "\n".join(briefs))
    except Exception as e:
        logger.warning(f"[readback] topic-brief retrieval skipped: {e}")

    try:
        chunks = await _relevant_chunks(meeting_title, meeting_level)
        if chunks:
            sections.append(
                "RELATED PAST DISCUSSION:\n" + "\n".join(f"  - {c}" for c in chunks)
            )
    except Exception as e:
        logger.warning(f"[readback] chunk retrieval skipped: {e}")

    if not sections:
        return None
    context = "\n\n".join(sections)
    if len(context) > budget_chars:
        context = context[:budget_chars] + " …(truncated)"
    return context
