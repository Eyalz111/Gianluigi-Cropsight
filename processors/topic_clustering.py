"""
Topic clustering -> consolidation proposals (v2.5 PR10).

Two months of fuzzy threading produced ~158 topics, many near-duplicates. This
pass proposes structural cleanups for Eyal's approval (never auto-applied):
- topic_merge: two topics with high name overlap -> merge the smaller into the
  larger.
- topic_assign: an unassigned topic whose name matches an Area -> assign it.

Proposals are stored as pending_approvals (content_type 'topic_merge' /
'topic_assign', id prefixed 'kprop-') and acted on via the [KNOWLEDGE] MCP
tools. Rate-limited per run so the review stays tractable. Deterministic
(Jaccard word-overlap) — no LLM cost.

apply_topic_proposal performs the structural move bi-temporally: the losing
topic is closed (valid_to set, never deleted) and a 'supersedes' link records
the merge, so history is preserved.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_STOP = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "project", "plan", "meeting"}
_MERGE_THRESHOLD = 0.6
_ASSIGN_THRESHOLD = 0.34
_DEFAULT_MAX = 3
_EXPIRY_DAYS = 30


def _words(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    return {w for w in cleaned.split() if w and w not in _STOP}


def _jaccard(a: str, b: str) -> float:
    aw, bw = _words(a), _words(b)
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw)


def _existing_proposal_keys() -> set[str]:
    """Keys of pending proposals, so we don't re-propose the same thing weekly."""
    try:
        rows = supabase_client.get_pending_approvals_by_status("pending") or []
    except Exception:
        return set()
    keys = set()
    for r in rows:
        if r.get("content_type") in ("topic_merge", "topic_assign"):
            key = (r.get("content") or {}).get("key")
            if key:
                keys.add(key)
    return keys


def _store_proposal(content_type: str, content: dict) -> str:
    pid = f"kprop-{uuid.uuid4()}"
    expires = (datetime.now(timezone.utc) + timedelta(days=_EXPIRY_DAYS)).isoformat()
    supabase_client.create_pending_approval(
        approval_id=pid, content_type=content_type, content=content, expires_at=expires
    )
    return pid


async def propose_topic_consolidation(max_proposals: int = _DEFAULT_MAX) -> dict:
    """Generate up to `max_proposals` consolidation proposals; store as pending."""
    topics = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, area_id, meeting_count, status")
        .is_("valid_to", "null")
        .execute()
        .data
        or []
    )
    active = [t for t in topics if (t.get("status") or "active") != "closed"]
    existing = _existing_proposal_keys()
    proposals: list[dict] = []

    # 1. Merge near-duplicate topics (the main consolidation lever).
    for i in range(len(active)):
        if len(proposals) >= max_proposals:
            break
        for j in range(i + 1, len(active)):
            if len(proposals) >= max_proposals:
                break
            a, b = active[i], active[j]
            sim = _jaccard(a.get("topic_name", ""), b.get("topic_name", ""))
            if sim < _MERGE_THRESHOLD:
                continue
            winner, loser = (a, b) if (a.get("meeting_count", 0) >= b.get("meeting_count", 0)) else (b, a)
            if winner["id"] == loser["id"]:
                continue
            key = f"merge:{loser['id']}"
            if key in existing:
                continue
            content = {
                "proposal_type": "topic_merge",
                "winner_id": winner["id"], "winner_name": winner.get("topic_name"),
                "loser_id": loser["id"], "loser_name": loser.get("topic_name"),
                "similarity": round(sim, 2), "key": key,
            }
            _store_proposal("topic_merge", content)
            existing.add(key)
            proposals.append(content)

    # 2. Assign unassigned topics to an Area by name overlap (secondary).
    if len(proposals) < max_proposals:
        areas = supabase_client.get_areas()
        for t in active:
            if len(proposals) >= max_proposals:
                break
            if t.get("area_id"):
                continue
            best, best_sim = None, 0.0
            for ar in areas:
                sim = _jaccard(t.get("topic_name", ""), ar.get("name", ""))
                if sim > best_sim:
                    best, best_sim = ar, sim
            if not best or best_sim < _ASSIGN_THRESHOLD:
                continue
            key = f"assign:{t['id']}"
            if key in existing:
                continue
            content = {
                "proposal_type": "topic_assign",
                "topic_id": t["id"], "topic_name": t.get("topic_name"),
                "area_id": best["id"], "area_name": best.get("name"),
                "similarity": round(best_sim, 2), "key": key,
            }
            _store_proposal("topic_assign", content)
            existing.add(key)
            proposals.append(content)

    logger.info(f"[clustering] created {len(proposals)} consolidation proposal(s)")
    return {"created": len(proposals), "proposals": proposals}


def apply_topic_proposal(content: dict) -> dict:
    """
    Apply an approved consolidation proposal (structural move). Bi-temporal:
    the losing topic is closed, never deleted; a supersedes link is recorded.
    """
    ptype = content.get("proposal_type")
    if ptype == "topic_merge":
        winner, loser = content.get("winner_id"), content.get("loser_id")
        if not winner or not loser or winner == loser:
            return {"error": "invalid merge (same or missing topic)"}
        # Re-point the losing topic's mentions to the winner.
        supabase_client.client.table("topic_thread_mentions").update(
            {"topic_id": winner}
        ).eq("topic_id", loser).execute()
        # Record the merge as a supersedes link.
        supabase_client.create_knowledge_link(
            "topic", winner, "topic", loser, "supersedes", created_by="eyal"
        )
        # Bi-temporally close the loser.
        now = datetime.now(timezone.utc).isoformat()
        supabase_client.client.table("topic_threads").update(
            {"status": "closed", "valid_to": now, "superseded_at": now}
        ).eq("id", loser).execute()
        return {"merged": loser, "into": winner}

    if ptype == "topic_assign":
        topic_id, area_id = content.get("topic_id"), content.get("area_id")
        if not topic_id or not area_id:
            return {"error": "invalid assignment"}
        supabase_client.set_topic_area(topic_id, area_id)
        supabase_client.create_knowledge_link(
            "topic", topic_id, "area", area_id, "belongs_to", created_by="eyal"
        )
        return {"assigned": topic_id, "to": area_id}

    return {"error": f"unknown proposal type: {ptype}"}
