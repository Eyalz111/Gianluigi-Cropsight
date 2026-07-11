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


# =============================================================================
# Propose-don't-clobber for decisions (Phase 2 PR C). Mirrors the task
# task_update_proposal flow: a sticky (manually-set) decision field is never
# overwritten by inference — a decision_update_proposal is raised for Eyal.
# =============================================================================

# Editable decision fields (manual_<field> flags). Mirrors
# supabase_client._DECISION_MANUAL_FIELDS but kept local so validation doesn't
# depend on the (test-patched) client instance.
_DECISION_FIELDS = ("description", "label", "rationale", "confidence", "status")
# Decision sticky-field name (manual_<field>) -> DB column. Only 'status' differs.
_DECISION_FIELD_COLUMN = {"status": "decision_status"}


def _decision_column(field: str) -> str:
    return _DECISION_FIELD_COLUMN.get(field, field)


def apply_decision_update(content: dict, approve: bool) -> dict:
    """Apply or reject a decision_update_proposal.

    On approve: write the proposed value + mark the field sticky (Eyal blessed
    it). Shared by decide_proposal (Claude.ai) + the /sync review (Telegram), so
    both surfaces behave identically. Guarded + idempotent.

    Returns {"status": "applied"|"rejected"|"gone"|"invalid", ...}.
    """
    decision_id = content.get("decision_id")
    field = content.get("field")
    proposed = content.get("proposed")

    if not approve:
        return {"status": "rejected"}
    if not (decision_id and field):
        return {"status": "invalid"}
    if field not in _DECISION_FIELDS:
        return {"status": "invalid", "reason": f"unknown field {field}"}
    if not supabase_client.get_decision(decision_id):
        return {"status": "gone"}

    supabase_client.update_decision(decision_id, **{_decision_column(field): proposed})
    supabase_client.mark_decision_field_manual(decision_id, field, "eyal")
    return {"status": "applied", "decision_id": decision_id, "field": field, "value": proposed}


def propose_or_update_decision_field(
    decision_id: str, field: str, value, *, source: str = "inference", summary: str = ""
) -> str:
    """The rail any FUTURE inference path must call to change a decision field.

    If Eyal set the field by hand (manual_<field> sticky) -> raise a
    decision_update_proposal for his one-tap review (propose, don't clobber).
    Otherwise write it directly. Returns "proposed" | "updated" | "noop".

    NOTE (2026-07-11): no current path auto-overwrites decision CONTENT — this is
    the guard a future dedup/continuity updater calls instead of update_decision,
    so a system change can never silently stomp a decision Eyal edited.
    """
    if field not in _DECISION_FIELDS:
        logger.warning(f"propose_or_update_decision_field: unknown field '{field}'")
        return "noop"
    d = supabase_client.get_decision(decision_id)
    if not d:
        return "noop"
    column = _decision_column(field)
    if d.get(f"manual_{field}"):
        supabase_client.create_decision_update_proposal(
            decision_id=decision_id, field=field, proposed=value,
            summary=summary or d.get("description", ""), current=d.get(column), source=source,
        )
        return "proposed"
    supabase_client.update_decision(decision_id, **{column: value})
    return "updated"


# =============================================================================
# DecisionBrief (Phase 2 PR C, groundwork). A decision's living-state object
# (decisions.brief_json), assembled DETERMINISTICALLY from the decision + its
# supersession chain — NO LLM. The later weekly decision-synthesis phase enriches
# the `narrative`. This just keeps the object current on every approval.
# =============================================================================

_VALID_SENSITIVITY = {"public", "team", "founders", "ceo"}


def build_decision_brief(decision_id: str) -> dict | None:
    """Assemble + persist a DecisionBrief (brief_json) for one decision.

    Deterministic snapshot of the decision + its chain position. Fire-and-forget:
    returns the brief dict on success, None on any failure (never raises).
    """
    from datetime import datetime, timezone
    from models.schemas import DecisionBrief
    try:
        d = supabase_client.get_decision(decision_id)
        if not d:
            return None
        try:
            chain = supabase_client.get_decision_chain(decision_id) or []
        except Exception:
            chain = []
        # Ancestors this decision replaced = chain entries before it (oldest first).
        supersedes: list[str] = []
        for c in chain:
            cid = c.get("id")
            if cid == decision_id:
                break
            if cid:
                supersedes.append(cid)

        sens = (d.get("sensitivity") or "founders").lower()
        if sens not in _VALID_SENSITIVITY:
            sens = "founders"

        brief = DecisionBrief(
            summary=d.get("description", "") or "",
            status=d.get("decision_status", "active") or "active",
            rationale=d.get("rationale", "") or "",
            supersedes=supersedes,
            superseded_by=d.get("superseded_by"),
            chain_length=max(1, len(chain)),
            last_referenced_at=d.get("last_referenced_at"),
            sensitivity=sens,
            last_synthesized_at=datetime.now(timezone.utc).isoformat(),
        )
        payload = brief.model_dump(mode="json")
        supabase_client.update_decision(decision_id, brief_json=payload)
        return payload
    except Exception as e:
        logger.warning(f"[decision_intel] build_decision_brief({decision_id}) failed: {e}")
        return None


def refresh_decision_briefs_for_meeting(meeting_id: str) -> int:
    """Rebuild the brief for each approved decision this meeting touched, plus any
    parent it just superseded (whose status/superseded_by changed). Deterministic,
    cheap; returns the count refreshed. Runs post-approval, fire-and-forget.
    """
    refreshed = 0
    seen: set = set()
    decisions = supabase_client.list_decisions(meeting_id=meeting_id, include_pending=False)
    for d in decisions:
        for did in (d.get("id"), d.get("parent_decision_id")):
            if did and did not in seen:
                seen.add(did)
                if build_decision_brief(did):
                    refreshed += 1
    return refreshed
