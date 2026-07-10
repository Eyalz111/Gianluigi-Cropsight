"""Decision intelligence (Phase 2) — supersession raised for Eyal's approval.

Decisions already carry the substrate for living chains (`decision_status`,
`parent_decision_id`, `superseded_by`, `get_decision_chain`) but none of the
behavior: `mark_decision_superseded` was orphaned and the status flip was manual
only. This wires the flip in — but as a PROPOSAL Eyal approves, never an auto-flip
(the I1 "Gianluigi proposes, Eyal approves" gate).

Flow:
  extraction  -> detect_supersessions + _link_decision_chains set parent_decision_id
  approval    -> propose_supersessions_for_meeting() raises a decision_supersede_proposal
                 for each newly-approved decision whose parent is still active
  Eyal taps   -> apply_decision_supersede() marks the old decision superseded +
                 records a 'supersedes' knowledge_link (decision -> decision)

Gated by settings.DECISION_INTELLIGENCE_ENABLED (default off = dormant). The
apply half is shared by the Claude.ai decide_proposal tool and the Telegram /sync
review, so both surfaces behave identically. This is the first increment of the
decision-intelligence layer (see docs/DECISION_INTELLIGENCE_DESIGN_2026_07.md).
"""

import logging

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


def propose_supersessions_for_meeting(meeting_id: str) -> int:
    """Raise a supersession proposal for each newly-approved decision that
    superseded an older ACTIVE decision. Returns the number proposed.

    Runs post-approval (decisions are 'approved' and durable). Idempotent — the
    producer skips a (old,new) pair that already has an open proposal.
    """
    proposed = 0
    # include_pending=False -> only the just-promoted approved decisions
    decisions = supabase_client.list_decisions(meeting_id=meeting_id, include_pending=False)
    for d in decisions:
        parent_id = d.get("parent_decision_id")
        if not parent_id:
            continue  # this decision didn't supersede anything
        parent = supabase_client.get_decision(parent_id)
        if not parent:
            continue  # parent gone (rejected/cascaded) — nothing to supersede
        if (parent.get("decision_status") or "active") != "active":
            continue  # already superseded/reversed — don't re-propose
        if supabase_client.create_decision_supersede_proposal(
            new_id=d["id"],
            old_id=parent_id,
            new_summary=d.get("description", ""),
            old_summary=parent.get("description", ""),
            source=f"meeting:{meeting_id}",
        ):
            proposed += 1
    if proposed:
        logger.info(
            f"[decision_intel] proposed {proposed} supersession(s) for meeting {meeting_id}"
        )
    return proposed


def apply_decision_supersede(content: dict, approve: bool) -> dict:
    """Apply or reject a decision_supersede_proposal.

    On approve: mark the old decision superseded_by the new one + record a
    'supersedes' knowledge_link. Guarded + idempotent — safe to double-apply.

    Returns a status dict:
      {"status": "applied"|"rejected"|"gone"|"already_superseded"|"invalid", ...}
    'gone' = the old decision no longer exists (harmless; caller just advances).
    """
    old_id = content.get("old_decision_id")
    new_id = content.get("new_decision_id")

    if not approve:
        return {"status": "rejected"}
    if not (old_id and new_id):
        return {"status": "invalid"}

    old = supabase_client.get_decision(old_id)
    if not old:
        return {"status": "gone"}
    if (old.get("decision_status") or "active") != "active":
        return {"status": "already_superseded"}

    supabase_client.mark_decision_superseded(old_id, new_id)
    # Record the chain edge in the knowledge graph (mirrors topic-merge 'supersedes').
    supabase_client.create_knowledge_link(
        from_type="decision", from_id=new_id,
        to_type="decision", to_id=old_id,
        link_type="supersedes", created_by="eyal",
    )
    return {"status": "applied", "old_id": old_id, "new_id": new_id}
