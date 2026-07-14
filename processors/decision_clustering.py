"""Cross-decision clustering -> merge/relate PROPOSALS (weekly, deterministic).

Mirrors processors/topic_clustering.py, for decisions. A weekly Jaccard sweep over
active decisions PROPOSES structural cleanups for Eyal's approval (never auto):
- decision_merge: two near-identical decisions -> retire the older duplicate.
- decision_relate: two moderately-overlapping decisions -> link them as 'related'.

NO deterministic supersede: Jaccard is symmetric and can't distinguish
"refines/replaces" from "duplicate" from "related"; directional supersede stays
with the LLM path (cross_reference.detect_supersessions -> parent_decision_id ->
propose_supersessions_for_meeting). This is a safety choice.

Proposals are pending_approvals (content_type decision_merge/decision_relate, id
'dprop-'), rate-limited + de-duped per run, applied only on Eyal's approval.
apply_decision_merge bi-temporally CLOSES the loser (reversible, never deleted),
mirroring apply_topic_proposal — decisions have no mentions to re-point, so
retiring the duplicate is the only structural act.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_STOP = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "project", "plan",
         "meeting", "decide", "decided", "decision", "use", "go", "with", "that",
         "this", "we", "will", "our", "be"}
_MERGE_THRESHOLD = 0.75    # near-identical => duplicate
_RELATE_THRESHOLD = 0.45   # moderate overlap => related-but-distinct
_DEFAULT_MAX = 3
_EXPIRY_DAYS = 30
_MAX_CANDIDATES = 60       # cap the O(n^2) pairwise scan + keep the review queue small


def _words(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    return {w for w in cleaned.split() if w and w not in _STOP}


def _jaccard(a: str, b: str) -> float:
    aw, bw = _words(a), _words(b)
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw)


def _text(d: dict) -> str:
    """Match key for a decision: label + description (the analog of topic_name)."""
    return f"{d.get('label', '')} {d.get('description', '')}"


def _existing_proposal_keys() -> set[str]:
    """Keys of pending decision-cluster proposals, so we don't re-propose weekly."""
    try:
        rows = supabase_client.get_pending_approvals_by_status("pending") or []
    except Exception:
        return set()
    keys = set()
    for r in rows:
        if r.get("content_type") in ("decision_merge", "decision_relate"):
            key = (r.get("content") or {}).get("key")
            if key:
                keys.add(key)
    return keys


def _store_proposal(content_type: str, content: dict) -> str:
    pid = f"dprop-{uuid.uuid4()}"
    expires = (datetime.now(timezone.utc) + timedelta(days=_EXPIRY_DAYS)).isoformat()
    supabase_client.create_pending_approval(
        approval_id=pid, content_type=content_type, content=content, expires_at=expires
    )
    return pid


async def propose_decision_consolidation(max_proposals: int = _DEFAULT_MAX) -> dict:
    """Generate up to `max_proposals` merge/relate proposals; store as pending."""
    decisions = (
        supabase_client.client.table("decisions")
        .select("id, label, description, created_at, decision_status, approval_status")
        .eq("decision_status", "active").eq("approval_status", "approved")
        .is_("valid_to", "null")
        .order("created_at", desc=True)
        .limit(_MAX_CANDIDATES)
        .execute()
        .data
        or []
    )
    existing = _existing_proposal_keys()
    proposals: list[dict] = []

    for i in range(len(decisions)):
        if len(proposals) >= max_proposals:
            break
        for j in range(i + 1, len(decisions)):
            if len(proposals) >= max_proposals:
                break
            a, b = decisions[i], decisions[j]
            if a["id"] == b["id"]:
                continue
            sim = _jaccard(_text(a), _text(b))

            if sim >= _MERGE_THRESHOLD:
                # winner = newer (current phrasing), loser = older (the duplicate to retire)
                winner, loser = (a, b) if a.get("created_at", "") >= b.get("created_at", "") else (b, a)
                key = f"merge:{loser['id']}:{winner['id']}"
                if key in existing:
                    continue
                content = {
                    "proposal_type": "decision_merge",
                    "winner_id": winner["id"], "winner_summary": (winner.get("description") or "")[:120],
                    "loser_id": loser["id"], "loser_summary": (loser.get("description") or "")[:120],
                    "similarity": round(sim, 2), "key": key,
                }
                _store_proposal("decision_merge", content)
                existing.add(key)
                proposals.append(content)

            elif sim >= _RELATE_THRESHOLD:
                lo, hi = sorted((a["id"], b["id"]))
                key = f"relate:{lo}:{hi}"
                if key in existing:
                    continue
                content = {
                    "proposal_type": "decision_relate",
                    "a_id": a["id"], "a_summary": (a.get("description") or "")[:120],
                    "b_id": b["id"], "b_summary": (b.get("description") or "")[:120],
                    "similarity": round(sim, 2), "key": key,
                }
                _store_proposal("decision_relate", content)
                existing.add(key)
                proposals.append(content)

    logger.info(f"[decision-clustering] created {len(proposals)} proposal(s)")
    return {"created": len(proposals), "proposals": proposals}


def _resolve_active_winner(decision_id: str, _depth: int = 0) -> str:
    """Follow a decision's superseded_by chain to the ultimate ACTIVE winner.

    A merge proposal may name a winner that was itself merged away since; pointing
    the loser at a retired winner would strand it (audit KP-01). Guards against
    cycles / runaway depth by capping the walk.
    """
    if _depth > 10:
        return decision_id
    d = supabase_client.get_decision(decision_id)
    if not d or (d.get("decision_status") or "active") == "active":
        return decision_id
    nxt = d.get("superseded_by")
    if not nxt or nxt == decision_id:
        return decision_id
    return _resolve_active_winner(nxt, _depth + 1)


def apply_decision_merge(content: dict) -> dict:
    """Retire the older duplicate: mark superseded + bi-temporally close + link.

    Reversible (null valid_to), never deleted. Does NOT touch parent_decision_id
    (avoid corrupting a genuine supersession chain). Mirrors apply_topic_proposal's
    bi-temporal close. Returns {status: applied|invalid|gone|already_superseded, ...}.
    """
    winner, loser = content.get("winner_id"), content.get("loser_id")
    if not winner or not loser or winner == loser:
        return {"status": "invalid", "reason": "same or missing decision"}
    old = supabase_client.get_decision(loser)
    if not old:
        return {"status": "gone"}
    if (old.get("decision_status") or "active") != "active":
        return {"status": "already_superseded"}
    # Resolve the winner to the ultimate ACTIVE decision — if it was itself merged
    # away since the proposal was created, point the loser at the live winner, not
    # a retired one (audit KP-01).
    winner = _resolve_active_winner(winner)
    if winner == loser:
        return {"status": "invalid", "reason": "winner resolves to loser"}
    supabase_client.mark_decision_superseded(loser, winner)          # status='superseded', superseded_by
    supabase_client.supersede_decision(loser, superseded_by=winner)  # valid_to + superseded_at
    supabase_client.create_knowledge_link(
        "decision", winner, "decision", loser, "supersedes", created_by="eyal"
    )
    # Retire the merged-away duplicate from the semantic index. [Phase 2]
    from processors.semantic_index import deindex as _si_deindex
    _si_deindex("decision", loser)
    return {"status": "applied", "merged": loser, "into": winner}


def apply_decision_relate(content: dict) -> dict:
    """Link two decisions as related (bidirectional relates_to). No status change.

    create_knowledge_link de-dupes current links, so double-apply is safe.
    """
    a, b = content.get("a_id"), content.get("b_id")
    if not a or not b or a == b:
        return {"status": "invalid", "reason": "same or missing decision"}
    if not supabase_client.get_decision(a) or not supabase_client.get_decision(b):
        return {"status": "gone"}
    supabase_client.create_knowledge_link("decision", a, "decision", b, "relates_to", created_by="eyal")
    supabase_client.create_knowledge_link("decision", b, "decision", a, "relates_to", created_by="eyal")
    return {"status": "applied", "related": [a, b]}


def apply_decision_cluster_proposal(content: dict, approve: bool) -> dict:
    """Shared apply for decision_merge / decision_relate (both consumer surfaces)."""
    if not approve:
        return {"status": "rejected"}
    ptype = content.get("proposal_type")
    if ptype == "decision_merge":
        return apply_decision_merge(content)
    if ptype == "decision_relate":
        return apply_decision_relate(content)
    return {"status": "invalid", "reason": f"unknown proposal type: {ptype}"}
