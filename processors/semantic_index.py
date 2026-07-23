"""Semantic index for decisions & topics — make the curated brain searchable.

The semantic index (the shared `embeddings` table + `match_embeddings` RPC) held
only raw meeting-transcript chunks; the distilled knowledge (decisions, topics)
was invisible to it, so `find_relevant_decisions` silently fell back to keyword
search and `search_memory` never surfaced a decision/topic. This module is the
ONLY place that turns a decision or topic into embeddings — everything else
calls `index_decision` / `index_topic` / `deindex`.

Rules (mirror `transcript_processor.generate_and_store_embeddings`):
  - **delete-then-insert** on every index → idempotent, no dupes, no staleness.
  - **top-level `sensitivity` column** on each row (the one `filter_by_sensitivity`
    reads) so retrieval stays tier-safe.
  - **flag-gated** by `SEMANTIC_INDEX_ENABLED` (default False, build dark).
  - **best-effort** — every entry point swallows its errors so a failure can
    never break the approval / edit / synthesis flow that called it.

Decisions/topics are short → one embedding each (chunk_index 0), no chunking.
Embedded text: decision = label + description + rationale (stable content, NOT
the weekly narrative → decouples the index from synthesis freshness);
topic = topic_name + narrative.
"""
import logging

from config.settings import settings
from services.embeddings import embedding_service
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

DECISION = "decision"
TOPIC = "topic"


def _enabled() -> bool:
    return bool(getattr(settings, "SEMANTIC_INDEX_ENABLED", False))


# ---------------------------------------------------------------------------
# Text + sensitivity extraction (manual_* overrides win, mirroring the readers)
# ---------------------------------------------------------------------------

def _decision_text(d: dict) -> str:
    # label/description/rationale are the live text columns — a manual edit is
    # written INTO them in place (the manual_* columns are boolean sticky flags,
    # not override text), so these already hold the current values.
    label = (d.get("label") or "").strip()
    desc = (d.get("description") or "").strip()
    rationale = (d.get("rationale") or "").strip()
    return ". ".join(p for p in (label, desc, rationale) if p)


def _topic_text(t: dict) -> str:
    name = (t.get("topic_name") or "").strip()
    brief = t.get("brief_json") or {}
    narrative = (brief.get("narrative") or "").strip() if isinstance(brief, dict) else ""
    return ". ".join(p for p in (name, narrative) if p)


def _topic_sensitivity(t: dict) -> str:
    brief = t.get("brief_json") or {}
    if isinstance(brief, dict) and brief.get("sensitivity"):
        return brief["sensitivity"]
    return t.get("sensitivity") or "founders"


def _topic_narrative(t: dict) -> str:
    brief = t.get("brief_json") or {}
    return (brief.get("narrative") or "").strip() if isinstance(brief, dict) else ""


def _topic_metadata(t: dict) -> dict:
    # Carry topic_name + narrative in metadata so retrieval can render them
    # cleanly (chunk_text is the embedded "name. narrative" blob).
    return {"kind": TOPIC, "topic_name": t.get("topic_name") or "",
            "narrative": _topic_narrative(t)}


def _record(source_type: str, source_id: str, text: str,
            embedding: list, sensitivity: str, metadata: dict) -> dict:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "chunk_text": text,
        "chunk_index": 0,
        "speaker": None,
        "timestamp_range": None,
        "embedding": embedding,
        "metadata": metadata or {},
        "sensitivity": sensitivity or "founders",
    }


# ---------------------------------------------------------------------------
# Incremental index / deindex (called from lifecycle hooks)
# ---------------------------------------------------------------------------

async def index_decision(decision: dict) -> None:
    """(Re)index one decision — delete-then-insert. Best-effort, flag-gated."""
    if not _enabled():
        return
    try:
        did = decision.get("id")
        text = _decision_text(decision)
        if not did or not text:
            return
        embedding = await embedding_service.embed_text(text)
        # `manual_label` is a BOOLEAN sticky-flag column, not an alternate label.
        # Falling back to it put the literal `True` into the index metadata
        # whenever a decision had an empty label and a sticky flag. [2026-07-22]
        meta = {"kind": DECISION, "label": decision.get("label") or ""}
        supabase_client.delete_embeddings_for_source(DECISION, did)
        supabase_client.store_embeddings_batch(
            [_record(DECISION, did, text, embedding,
                     decision.get("sensitivity") or "founders", meta)]
        )
    except Exception as e:
        logger.warning(f"[semantic_index] index_decision failed: {e}")


async def index_topic(topic: dict) -> None:
    """(Re)index one topic. A non-active topic is deindexed instead. Best-effort."""
    if not _enabled():
        return
    try:
        tid = topic.get("id")
        if not tid:
            return
        # Retire from the index if the topic is no longer active (closed/stale).
        if (topic.get("status") or "active") != "active":
            supabase_client.delete_embeddings_for_source(TOPIC, tid)
            return
        text = _topic_text(topic)
        if not text:
            return
        embedding = await embedding_service.embed_text(text)
        meta = _topic_metadata(topic)
        supabase_client.delete_embeddings_for_source(TOPIC, tid)
        supabase_client.store_embeddings_batch(
            [_record(TOPIC, tid, text, embedding, _topic_sensitivity(topic), meta)]
        )
    except Exception as e:
        logger.warning(f"[semantic_index] index_topic failed: {e}")


def deindex(source_type: str, source_id: str) -> None:
    """Remove an entity from the index (retired / merged / rejected). Best-effort."""
    if not _enabled():
        return
    try:
        if source_id:
            supabase_client.delete_embeddings_for_source(source_type, source_id)
    except Exception as e:
        logger.warning(f"[semantic_index] deindex {source_type}/{source_id} failed: {e}")


async def index_decisions_for_meeting(meeting_id: str) -> int:
    """Index every approved decision for a meeting — the post-approval hook."""
    if not _enabled():
        return 0
    count = 0
    try:
        for d in supabase_client.list_decisions(meeting_id=meeting_id, include_pending=False):
            await index_decision(d)
            count += 1
    except Exception as e:
        logger.warning(f"[semantic_index] index_decisions_for_meeting {meeting_id} failed: {e}")
    return count


async def reindex_decision(decision_id: str) -> None:
    """Fetch a decision by id and (re)index it — for content changes. Best-effort."""
    if not _enabled() or not decision_id:
        return
    try:
        d = supabase_client.get_decision(decision_id)
        if d:
            await index_decision(d)
    except Exception as e:
        logger.warning(f"[semantic_index] reindex_decision {decision_id} failed: {e}")


async def reindex_topic(topic_id: str) -> None:
    """Fetch a topic by id and (re)index it — for narrative changes. Best-effort."""
    if not _enabled() or not topic_id:
        return
    try:
        t = supabase_client.get_topic_thread(topic_id)
        if t:
            await index_topic(t)
    except Exception as e:
        logger.warning(f"[semantic_index] reindex_topic {topic_id} failed: {e}")


def schedule_reindex_decision(decision_id: str) -> None:
    """Fire-and-forget re-index from a SYNC call site running inside the event
    loop (e.g. a proposal-apply handler). No-op if there's no running loop
    (a plain script) or the flag is off. Best-effort."""
    _schedule(reindex_decision(decision_id) if _enabled() and decision_id else None)


def schedule_reindex_topic(topic_id: str) -> None:
    """Fire-and-forget topic re-index from any call site inside the event loop."""
    _schedule(reindex_topic(topic_id) if _enabled() and topic_id else None)


def _schedule(coro) -> None:
    if coro is None:
        return
    try:
        import asyncio
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        # No running loop (a plain script) — close the coroutine to avoid a warning.
        try:
            coro.close()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"[semantic_index] schedule failed: {e}")


# ---------------------------------------------------------------------------
# Backfill (batch via embed_texts — one API call per batch). Idempotent.
# ---------------------------------------------------------------------------

async def _backfill(items: list[tuple], source_type: str, apply: bool) -> dict:
    """items: list of (id, text, sensitivity, metadata). Batch-embed + replace."""
    items = [(i, t, s, m) for (i, t, s, m) in items if i and t]
    if not apply:
        return {"source_type": source_type, "candidates": len(items), "applied": False}
    indexed = 0
    for start in range(0, len(items), 100):
        batch = items[start:start + 100]
        embeddings = await embedding_service.embed_texts([t for (_, t, _, _) in batch])
        records = []
        for (sid, text, sens, meta), emb in zip(batch, embeddings):
            supabase_client.delete_embeddings_for_source(source_type, sid)
            records.append(_record(source_type, sid, text, emb, sens, meta))
        if records:
            supabase_client.store_embeddings_batch(records)
            indexed += len(records)
    return {"source_type": source_type, "candidates": len(items), "applied": True, "indexed": indexed}


async def backfill_decisions(apply: bool = False) -> dict:
    """Index all approved current decisions."""
    decisions = supabase_client.list_decisions(limit=1000)
    items = [
        (d.get("id"), _decision_text(d), d.get("sensitivity") or "founders",
         {"kind": DECISION, "label": d.get("label") or ""})
        for d in decisions
    ]
    return await _backfill(items, DECISION, apply)


async def backfill_topics(apply: bool = False) -> dict:
    """Index all active topics that have a synthesized narrative."""
    rows = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, status, brief_json")
        .eq("status", "active")
        .execute()
        .data
    )
    items = [
        (t.get("id"), _topic_text(t), _topic_sensitivity(t), _topic_metadata(t))
        for t in rows
        if isinstance(t.get("brief_json"), dict) and t["brief_json"].get("narrative")
    ]
    return await _backfill(items, TOPIC, apply)
