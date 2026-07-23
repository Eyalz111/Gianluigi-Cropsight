"""Pending-proposal review for the Telegram /sync flow.

Lets Eyal tackle knowledge/task proposals (topic merges, topic assignments,
task-field updates) from Telegram — previously they were only actionable via the
Claude.ai proposals tools. Decision logic mirrors services.mcp_server.decide_proposal
for these types (topic ops delegate to the same apply_topic_proposal), so both
surfaces stay consistent. gantt_tag proposals are intentionally excluded (their
apply does a Sheet write — left to the Claude.ai side). [proposal-review 2026-07-06]
"""

import logging

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Proposal content_types Eyal can decide from the Telegram /sync review flow.
REVIEWABLE_TYPES = (
    "topic_merge", "topic_assign", "task_update_proposal", "decision_supersede_proposal",
    "decision_update_proposal", "decision_merge", "decision_relate",
    "project_new", "question_resolved",
)


def _label(content_type: str, c: dict) -> str:
    """Human-readable HTML card body for a proposal."""
    if content_type == "topic_merge":
        return (
            f"Merge topics?\n"
            f"<b>\"{c.get('loser_name', '?')}\"</b>  →  <b>\"{c.get('winner_name', '?')}\"</b>\n"
            f"<i>(they look like the same thread)</i>"
        )
    if content_type == "topic_assign":
        return f"Assign topic <b>\"{c.get('topic_name', '?')}\"</b> to area <b>{c.get('area_name', '?')}</b>?"
    if content_type == "task_update_proposal":
        return f"Update task field <b>{c.get('field', '?')}</b> → <b>{c.get('proposed', '?')}</b>?"
    if content_type == "decision_update_proposal":
        summ = (c.get("summary") or "?")[:80]
        return (
            f"Update decision field <b>{c.get('field', '?')}</b> → "
            f"<b>{c.get('proposed', '?')}</b>?\n<i>\"{summ}\"</i>"
        )
    if content_type == "decision_merge":
        return (
            f"Merge duplicate decisions?\n"
            f"<b>KEEP:</b> \"{(c.get('winner_summary') or '?')[:80]}\"\n"
            f"<b>DROP:</b> \"{(c.get('loser_summary') or '?')[:80]}\"\n"
            f"<i>(same decision recorded twice — retires the older)</i>"
        )
    if content_type == "decision_relate":
        return (
            f"Link related decisions?\n"
            f"\"{(c.get('a_summary') or '?')[:80]}\"  ↔  \"{(c.get('b_summary') or '?')[:80]}\""
        )
    if content_type == "decision_supersede_proposal":
        old = (c.get("old_summary") or "?")[:80]
        new = (c.get("new_summary") or "?")[:80]
        return (
            f"Supersede a decision?\n"
            f"<b>OLD:</b> \"{old}\"\n"
            f"<b>NEW:</b> \"{new}\"\n"
            f"<i>(mark the old one superseded by the new)</i>"
        )
    if content_type == "project_new":
        seen = c.get("meeting_count", "?")
        samples = ", ".join((c.get("sample_meetings") or [])[:2]) or "—"
        return (
            f"Add <b>\"{c.get('name', '?')}\"</b> as a project?\n"
            f"<i>Used as a label in {seen} meetings — {samples}</i>"
        )
    if content_type == "question_resolved":
        return (
            f"Close this open question?\n"
            f"<b>Q:</b> \"{(c.get('question') or '?')[:110]}\"\n"
            f"<b>Answered by:</b> \"{(c.get('decision_summary') or '?')[:110]}\"\n"
            f"<i>(match {c.get('score', '?')})</i>"
        )
    return "Review this suggestion?"


def list_pending_proposals() -> list[dict]:
    """Pending reviewable proposals, oldest first, each with a rendered label."""
    try:
        rows = (
            supabase_client.client.table("pending_approvals")
            .select("approval_id,content_type,content,created_at")
            .eq("status", "pending")
            .in_("content_type", list(REVIEWABLE_TYPES))
            .order("created_at")
            .execute()
            .data
        ) or []
    except Exception as e:
        logger.error(f"list_pending_proposals failed: {e}")
        return []
    return [
        {
            "proposal_id": r["approval_id"],
            "content_type": r["content_type"],
            "content": r.get("content") or {},
            "label": _label(r["content_type"], r.get("content") or {}),
        }
        for r in rows
    ]


def apply_proposal_decision(proposal_id: str, decision: str) -> dict:
    """Approve/reject a reviewable proposal (topic merge/assign or task-field update).

    Returns {"status": "ok"|"gone"|"unsupported", "decision": ..., ...}. 'gone'
    means it was already decided elsewhere (harmless — the caller just advances).
    """
    pending = supabase_client.get_pending_approval(proposal_id)
    if not pending:
        return {"status": "gone"}
    content_type = pending.get("content_type")
    content = pending.get("content") or {}
    approve = decision == "approve"

    if content_type in ("topic_merge", "topic_assign"):
        result = None
        if approve:
            from processors.topic_clustering import apply_topic_proposal
            result = apply_topic_proposal(content)
        supabase_client.delete_pending_approval(proposal_id)
        supabase_client.log_action(
            "knowledge_proposal_approved" if approve else "knowledge_proposal_rejected",
            details={"proposal_id": proposal_id, "source": "telegram_sync", **content, "result": result},
            triggered_by="eyal",
        )
        return {"status": "ok", "decision": "approved" if approve else "rejected", "result": result}

    if content_type in ("project_new", "question_resolved"):
        result = None
        if approve:
            if content_type == "project_new":
                from processors.project_learning import apply_project_proposal as _apply
            else:
                from processors.question_lifecycle import apply_question_resolution as _apply
            result = _apply(content)
            if not result.get("ok"):
                # Leave it pending so it can be retried, rather than reporting a
                # success that didn't happen (mirrors the MCP surface).
                return {"status": "error", "error": result.get("error")}
        supabase_client.delete_pending_approval(proposal_id)
        supabase_client.log_action(
            f"{content_type}_approved" if approve else f"{content_type}_rejected",
            details={"proposal_id": proposal_id, "source": "telegram_sync", **content, "result": result},
            triggered_by="eyal",
        )
        return {"status": "ok", "decision": "approved" if approve else "rejected", "result": result}

    if content_type == "task_update_proposal":
        tid, field, proposed = content.get("task_id"), content.get("field"), content.get("proposed")
        if approve and tid and field:
            upd = {field: proposed}
            if field == "deadline":
                upd["deadline_confidence"] = "EXPLICIT"
            supabase_client.update_task(tid, **upd)
            supabase_client.mark_task_field_manual(tid, field, "eyal_telegram")
        supabase_client.delete_pending_approval(proposal_id)
        supabase_client.log_action(
            "task_proposal_approved" if approve else "task_proposal_rejected",
            details={"proposal_id": proposal_id, "source": "telegram_sync", **content},
            triggered_by="eyal",
        )
        return {"status": "ok", "decision": "approved" if approve else "rejected"}

    if content_type == "decision_update_proposal":
        from processors.decision_intelligence import apply_decision_update
        result = apply_decision_update(content, approve)
        supabase_client.delete_pending_approval(proposal_id)
        supabase_client.log_action(
            "decision_update_approved" if approve else "decision_update_rejected",
            details={"proposal_id": proposal_id, "source": "telegram_sync", **content, "result": result},
            triggered_by="eyal",
        )
        return {"status": "ok", "decision": "approved" if approve else "rejected", "result": result}

    if content_type == "decision_supersede_proposal":
        from processors.decision_intelligence import apply_decision_supersede
        result = apply_decision_supersede(content, approve)
        supabase_client.delete_pending_approval(proposal_id)
        supabase_client.log_action(
            "decision_supersede_approved" if approve else "decision_supersede_rejected",
            details={"proposal_id": proposal_id, "source": "telegram_sync", **content, "result": result},
            triggered_by="eyal",
        )
        return {"status": "ok", "decision": "approved" if approve else "rejected", "result": result}

    if content_type in ("decision_merge", "decision_relate"):
        from processors.decision_clustering import apply_decision_cluster_proposal
        result = apply_decision_cluster_proposal(content, approve)
        supabase_client.delete_pending_approval(proposal_id)
        supabase_client.log_action(
            f"{content_type}_approved" if approve else f"{content_type}_rejected",
            details={"proposal_id": proposal_id, "source": "telegram_sync", **content, "result": result},
            triggered_by="eyal",
        )
        return {"status": "ok", "decision": "approved" if approve else "rejected", "result": result}

    return {"status": "unsupported", "content_type": content_type}
