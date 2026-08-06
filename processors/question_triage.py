"""
Re-triage of the OPEN QUESTIONS backlog.

Why this exists: question resolution only ran at INGESTION, against the single
meeting being processed. A question answered by a decision three meetings later
therefore stayed open forever, and the backlog only grew — 69 open as of
2026-08-06, of which a manual pass found 9 already settled.

This re-checks open questions against decisions and completed work that came
AFTER they were raised, and PROPOSES closures. It never closes anything itself:
"Gianluigi proposes, Eyal approves" (I1). Proposals land in `pending_approvals`
as `question_close_proposal` and are actioned via decide_proposal / the Telegram
review.

Deliberately conservative — leaving a settled question open is a nuisance;
closing a live strategic question loses it.
"""

import logging

from config.settings import settings
from core.llm import call_llm, parse_json_array
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

BATCH_SIZE = 12
# Evidence budget. Enough recent context to judge, small enough to stay cheap.
MAX_DECISIONS = 220
MAX_TASKS = 200

PROPOSAL_PREFIX = "qclose-"


def _meeting_date(row: dict) -> str:
    return str((row.get("meetings") or {}).get("date") or row.get("created_at") or "")[:10]


def _fetch_open_questions() -> list[dict]:
    try:
        return (
            supabase_client.client.table("open_questions")
            .select("id,question,raised_by,created_at,"
                    "meetings!open_questions_meeting_id_fkey(title,date)")
            .eq("approval_status", "approved")
            .eq("status", "open")
            .limit(500)
            .execute()
            .data
        ) or []
    except Exception as e:
        logger.error(f"[question-triage] question fetch failed: {e}")
        return []


def _evidence() -> str:
    """Decisions taken and work done — what a question could have been settled by."""
    try:
        decs = (
            supabase_client.client.table("decisions")
            .select("description,meetings(date)")
            .eq("approval_status", "approved").is_("valid_to", "null")
            .limit(500).execute().data
        ) or []
        tasks = (
            supabase_client.client.table("tasks")
            .select("title,status")
            .eq("approval_status", "approved").is_("valid_to", "null")
            .in_("status", ["done", "in_progress"])
            .limit(500).execute().data
        ) or []
    except Exception as e:
        logger.error(f"[question-triage] evidence fetch failed: {e}")
        return ""

    dec_lines = sorted({
        f"[{str((d.get('meetings') or {}).get('date') or '')[:10]}] {d['description'][:150]}"
        for d in decs if d.get("description")
    })
    task_lines = sorted({
        f"({t['status']}) {t['title'][:110]}" for t in tasks if t.get("title")
    })
    return ("DECISIONS TAKEN (dated):\n" + "\n".join(dec_lines[-MAX_DECISIONS:]) +
            "\n\nWORK DONE / IN PROGRESS:\n" + "\n".join(task_lines[:MAX_TASKS]))


def _prompt(context: str, chunk: list[dict]) -> str:
    listing = "\n".join(
        f'{n}. [raised {_meeting_date(q)} in "{(q.get("meetings") or {}).get("title", "?")[:45]}"] '
        f'{q["question"]}'
        for n, q in enumerate(chunk, 1)
    )
    return f"""You are triaging CropSight's OPEN QUESTIONS backlog for the CEO.

For each question decide ONE verdict:
- "answered"  - a listed decision or completed work clearly resolves it. Quote the evidence.
- "obsolete"  - the situation moved on, so the question no longer matters. Say why.
- "open"      - still a live, unresolved question. Default to this when unsure.

Be CONSERVATIVE. Closing a live strategic question is far worse than leaving it open.
A question is NOT answered merely because it was discussed, or because a related
task exists. It must be genuinely settled, and your evidence must address the
question actually asked — not a neighbouring topic.

{context}

QUESTIONS TO TRIAGE:
{listing}

Return ONLY a JSON array, one object per question, in order:
[{{"n": 1, "verdict": "answered|obsolete|open", "reason": "<=18 words", "evidence": "<quote or empty>"}}]"""


def triage_open_questions(limit: int | None = None) -> list[dict]:
    """Return [{question_id, question, verdict, reason, evidence}] for non-open verdicts."""
    questions = _fetch_open_questions()
    if limit:
        questions = questions[:limit]
    if not questions:
        return []
    context = _evidence()
    if not context.strip():
        logger.warning("[question-triage] no evidence available — skipping")
        return []

    proposals: list[dict] = []
    for i in range(0, len(questions), BATCH_SIZE):
        chunk = questions[i:i + BATCH_SIZE]
        try:
            text, _ = call_llm(
                prompt=_prompt(context, chunk),
                model=settings.model_agent,
                max_tokens=2000,
                call_site="question_triage",
            )
        except Exception as e:
            logger.error(f"[question-triage] LLM call failed on batch {i//BATCH_SIZE+1}: {e}")
            continue
        for item in parse_json_array(text) or []:
            try:
                idx = int(item.get("n", 0)) - 1
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(chunk)):
                continue
            verdict = str(item.get("verdict", "open")).lower()
            if verdict not in ("answered", "obsolete"):
                continue
            q = chunk[idx]
            proposals.append({
                "question_id": q["id"],
                "question": q["question"],
                "meeting": (q.get("meetings") or {}).get("title", ""),
                "raised": _meeting_date(q),
                "verdict": verdict,
                "reason": str(item.get("reason", ""))[:200],
                "evidence": str(item.get("evidence", ""))[:300],
            })

    logger.info(
        f"[question-triage] {len(questions)} open -> {len(proposals)} closure proposal(s)"
    )
    return proposals


def submit_close_proposals(proposals: list[dict]) -> int:
    """Persist proposals as pending_approvals. Idempotent per question."""
    created = 0
    for p in proposals:
        approval_id = f"{PROPOSAL_PREFIX}{p['question_id']}"
        try:
            if supabase_client.get_pending_approval(approval_id):
                continue  # already proposed and not yet actioned
            supabase_client.upsert_pending_approval(
                approval_id=approval_id,
                content_type="question_close_proposal",
                content=p,
            )
            created += 1
        except Exception as e:
            logger.warning(f"[question-triage] could not submit {approval_id}: {e}")
    if created:
        logger.info(f"[question-triage] submitted {created} closure proposal(s)")
    return created


def apply_close_proposal(content: dict) -> dict:
    """Close the question a proposal refers to. Called on Eyal's approval only."""
    from datetime import datetime, timezone

    qid = content.get("question_id")
    if not qid:
        return {"closed": False, "error": "no question_id"}
    reason = f"Closed by weekly triage: {content.get('reason', '')}"[:400]
    try:
        supabase_client.client.table("open_questions").update({
            "status": "resolved",
            "status_reason": reason,
            "status_changed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", qid).execute()
        return {"closed": True, "question_id": qid}
    except Exception as e:
        logger.error(f"[question-triage] close failed for {qid}: {e}")
        return {"closed": False, "error": str(e)}
