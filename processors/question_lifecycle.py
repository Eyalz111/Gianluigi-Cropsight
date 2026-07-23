"""Open-question lifecycle — aging, and proposal-based resolution. [2026-07-22]

The component has had an inbox and no outbox since the early days of the system.
Extraction creates a question from every meeting; the ONLY exit was a later
meeting explicitly answering the same question (cross_reference), which almost
never fires. Live result: 100+ questions open, going back to May, with no owner,
no priority and no aging — a list nobody will ever work.

Two additions, both reversible and neither destructive:

  1. AGING. `open` -> `stale` at 60 days untouched. The row is never deleted;
     flipping status back restores it exactly. Aged items leave the working view
     the same way `archived` removes a task from the Tasks tab.

  2. RESOLUTION PROPOSALS. Decisions have been embedded in the semantic index
     since 2026-07-14, so a question can be matched against decisions made
     AFTER it was raised. A match is PROPOSED, never auto-applied: silently
     closing a real question destroys it, and "Gianluigi proposes, Eyal
     approves" is the standing rule.

Statuses: open | resolved | stale | dropped | superseded  (plain TEXT, no CHECK
constraint, so the new values are additive).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

STALE_AFTER_DAYS = 60
_EXPIRY_DAYS = 30
_DEFAULT_MAX_PROPOSALS = 5
# Deliberately high: a false "this decision answers your question" closes a real
# open item, so only a strong match is worth Eyal's attention.
_MIN_SIMILARITY = 0.80

PROPOSAL_TYPE = "question_resolved"


def age_out_questions(dry_run: bool = False, days: int | None = None) -> dict:
    """Move `open` questions untouched for `days` to `stale`.

    Returns a summary. Never deletes; never raises.
    """
    cutoff_days = days if days is not None else STALE_AFTER_DAYS
    result = {"scanned": 0, "aged": 0, "dry_run": dry_run, "cutoff_days": cutoff_days}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cutoff_days)).isoformat()
    try:
        rows = (
            supabase_client.client.table("open_questions")
            .select("id, question, created_at, status")
            .eq("status", "open")
            .lt("created_at", cutoff)
            .limit(2000)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.warning(f"[question_lifecycle] could not read open questions: {e}")
        return result

    result["scanned"] = len(rows)
    if not rows or dry_run:
        return result

    now = datetime.now(timezone.utc).isoformat()
    for q in rows:
        try:
            supabase_client.client.table("open_questions").update({
                "status": "stale",
                "status_changed_at": now,
                "status_reason": f"auto-aged: no activity for {cutoff_days} days",
            }).eq("id", q["id"]).execute()
            result["aged"] += 1
        except Exception as e:
            logger.warning(f"[question_lifecycle] aging failed for {q.get('id')}: {e}")

    if result["aged"]:
        try:
            supabase_client.log_action(
                "questions_aged_out",
                details={"count": result["aged"], "cutoff_days": cutoff_days},
                triggered_by="auto",
            )
        except Exception:
            pass
        logger.info(f"[question_lifecycle] aged {result['aged']} question(s) to stale")
    return result


def restore_question(question_id: str, reason: str = "restored by Eyal") -> bool:
    """Flip a stale/dropped question back to open — the aging escape hatch."""
    try:
        supabase_client.client.table("open_questions").update({
            "status": "open",
            "status_changed_at": datetime.now(timezone.utc).isoformat(),
            "status_reason": reason,
        }).eq("id", question_id).execute()
        supabase_client.log_action(
            "question_restored", details={"question_id": question_id, "reason": reason},
            triggered_by="eyal",
        )
        return True
    except Exception as e:
        logger.error(f"[question_lifecycle] restore failed for {question_id}: {e}")
        return False


def _existing_keys() -> set[str]:
    try:
        rows = supabase_client.get_pending_approvals_by_status("pending") or []
    except Exception:
        return set()
    return {
        (r.get("content") or {}).get("key")
        for r in rows
        if r.get("content_type") == PROPOSAL_TYPE and (r.get("content") or {}).get("key")
    }


async def propose_question_resolutions(max_proposals: int = _DEFAULT_MAX_PROPOSALS) -> dict:
    """Match open questions against LATER decisions; propose closures.

    Uses the semantic index (decisions embedded since 2026-07-14). Proposes
    only — never auto-resolves.
    """
    result = {"scanned": 0, "proposed": 0, "questions": []}
    if not getattr(settings, "SEMANTIC_INDEX_ENABLED", False):
        return result
    try:
        rows = (
            supabase_client.client.table("open_questions")
            .select("id, question, created_at, meeting_id")
            .eq("status", "open")
            .order("created_at", desc=False)
            .limit(200)
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.warning(f"[question_lifecycle] could not read questions: {e}")
        return result

    result["scanned"] = len(rows)
    existing = _existing_keys()

    from services.embeddings import embedding_service

    for q in rows:
        if result["proposed"] >= max_proposals:
            break
        key = f"qres:{q['id']}"
        if key in existing:
            continue
        question = (q.get("question") or "").strip()
        if len(question) < 12:
            continue
        try:
            vec = await embedding_service.embed_text(question)
            hits = supabase_client.search_embeddings(
                query_embedding=vec, limit=3, source_type="decision",
                similarity_threshold=_MIN_SIMILARITY,
            )
        except Exception as e:
            logger.debug(f"[question_lifecycle] search failed for {q['id']}: {e}")
            continue
        if not hits:
            continue
        # Only a decision made AFTER the question was raised can answer it.
        #
        # match_embeddings does NOT return created_at (see its RETURNS TABLE in
        # migrate_semantic_index.sql), so comparing h["created_at"] directly
        # compared "" against the timestamp every time — always False, so this
        # function could never propose anything. Hydrate the dates from the
        # decisions table instead of widening the RPC contract. [2026-07-23]
        raised = str(q.get("created_at") or "")
        dec_ids = [h.get("source_id") for h in hits if h.get("source_id")]
        made_at: dict[str, str] = {}
        if dec_ids:
            try:
                for d in (supabase_client.client.table("decisions")
                          .select("id, created_at").in_("id", dec_ids)
                          .execute().data or []):
                    made_at[str(d["id"])] = str(d.get("created_at") or "")
            except Exception as e:
                logger.debug(f"[question_lifecycle] decision date lookup failed: {e}")
                continue
        candidates = [
            h for h in hits
            if made_at.get(str(h.get("source_id")), "") > raised
        ]
        if not candidates:
            continue
        best = candidates[0]
        content = {
            "proposal_type": PROPOSAL_TYPE,
            "question_id": q["id"],
            "question": question[:300],
            "decision_id": best.get("source_id"),
            "decision_summary": (best.get("chunk_text") or "")[:300],
            "score": round(float(best.get("similarity") or 0), 3),
            "key": key,
        }
        try:
            pid = f"qres-{uuid.uuid4()}"
            expires = (datetime.now(timezone.utc) + timedelta(days=_EXPIRY_DAYS)).isoformat()
            supabase_client.create_pending_approval(
                approval_id=pid, content_type=PROPOSAL_TYPE,
                content=content, expires_at=expires,
            )
            existing.add(key)
            result["proposed"] += 1
            result["questions"].append(question[:60])
        except Exception as e:
            logger.warning(f"[question_lifecycle] could not store proposal: {e}")

    return result


def apply_question_resolution(content: dict) -> dict:
    """Approve a question_resolved proposal — mark the question resolved."""
    qid = (content or {}).get("question_id")
    if not qid:
        return {"ok": False, "error": "proposal carries no question_id"}
    try:
        supabase_client.client.table("open_questions").update({
            "status": "resolved",
            "status_changed_at": datetime.now(timezone.utc).isoformat(),
            "status_reason": f"answered by decision {content.get('decision_id')}",
        }).eq("id", qid).execute()
        return {"ok": True, "question_id": qid}
    except Exception as e:
        return {"ok": False, "error": str(e)}
