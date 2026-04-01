"""
Meeting-to-meeting continuity — cross-meeting context for extraction.

Before extracting from a new transcript, fetches summaries of 2-3 recent
meetings with overlapping participants. This gives the extraction LLM
awareness of what was discussed previously, enabling smarter task status
inference and deduplication.

Phase 12 A1: Enhanced context gatherer — adds task completion stats,
decision review dates, question aging, plus two new entry points:
- build_daily_continuity_context()  — for morning brief (Haiku)
- build_pre_meeting_continuity_context() — for meeting prep (Sonnet)

Usage:
    from processors.meeting_continuity import build_meeting_continuity_context
    context = build_meeting_continuity_context(participants, meeting_id)
"""

import logging
from datetime import date, datetime, timedelta, timezone

from config.settings import settings
from core.llm import call_llm
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Max tokens for meeting history context (keeps extraction prompt manageable)
_MAX_CONTEXT_CHARS = 3000

# Max chars for daily/pre-meeting contexts
_MAX_DAILY_CONTEXT_CHARS = 4000
_MAX_PRE_MEETING_CONTEXT_CHARS = 6000


def _days_ago(iso_date_str: str | None) -> int | None:
    """Return how many days ago an ISO date string is, or None if unparseable."""
    if not iso_date_str:
        return None
    try:
        d = datetime.fromisoformat(str(iso_date_str).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - d
        return max(0, delta.days)
    except (ValueError, TypeError):
        return None


def _format_task_stats(tasks: list[dict]) -> dict:
    """Compute task completion stats from a list of tasks."""
    total = len(tasks)
    if total == 0:
        return {"total": 0, "done": 0, "open": 0, "overdue": 0}

    done = sum(1 for t in tasks if t.get("status") == "done")
    overdue = sum(1 for t in tasks if t.get("status") == "overdue")
    open_count = total - done

    return {
        "total": total,
        "done": done,
        "open": open_count,
        "overdue": overdue,
    }


def build_meeting_continuity_context(
    participants: list[str],
    current_meeting_id: str | None = None,
) -> str | None:
    """
    Build a compressed context block from recent meetings with overlapping participants.

    Enhanced in Phase 12 with task completion stats, decision review dates,
    and question aging.

    Args:
        participants: Participant names from the current meeting.
        current_meeting_id: UUID of the current meeting (to exclude).

    Returns:
        Formatted context string or None if no relevant history found.
    """
    if not participants:
        return None

    try:
        recent_meetings = supabase_client.get_meetings_by_participant_overlap(
            participants=participants,
            exclude_meeting_id=current_meeting_id,
            limit=3,
        )
    except Exception as e:
        logger.warning(f"Could not fetch meeting history for continuity: {e}")
        return None

    if not recent_meetings:
        return None

    context_parts = []

    for meeting in recent_meetings:
        title = meeting.get("title", "Untitled")
        date_str = str(meeting.get("date", ""))[:10]
        meeting_id = meeting.get("id", "")

        # Get decisions, tasks, and questions from this meeting
        try:
            decisions = supabase_client.list_decisions(meeting_id=meeting_id)
            tasks_all = supabase_client.get_tasks(status=None)
            tasks = [t for t in tasks_all if t.get("meeting_id") == meeting_id]
            open_tasks = [t for t in tasks if t.get("status") in ("pending", "in_progress")]
            questions = supabase_client.get_open_questions(meeting_id=meeting_id)
            open_qs = [q for q in questions if q.get("status") == "open"]
        except Exception as e:
            logger.debug(f"Could not fetch details for meeting {meeting_id}: {e}")
            decisions = []
            tasks = []
            open_tasks = []
            open_qs = []

        parts = [f"Meeting: \"{title}\" ({date_str})"]

        # Task completion stats (Phase 12)
        stats = _format_task_stats(tasks)
        if stats["total"] > 0:
            parts.append(
                f"  Tasks: {stats['done']}/{stats['total']} completed"
                + (f", {stats['overdue']} overdue" if stats["overdue"] else "")
            )

        if decisions:
            parts.append("  Decisions: " + "; ".join(
                d.get("description", "")[:60] for d in decisions[:3]
            ))
            # Decision review dates (Phase 12)
            approaching = [
                d for d in decisions
                if d.get("review_date") and _days_until_review(d["review_date"]) is not None
                and 0 <= _days_until_review(d["review_date"]) <= 14  # type: ignore[operator]
            ]
            if approaching:
                review_strs = [
                    f"{d.get('description', '')[:40]} (review in {_days_until_review(d['review_date'])}d)"
                    for d in approaching[:2]
                ]
                parts.append(f"  Approaching review: {'; '.join(review_strs)}")

        if open_tasks:
            task_lines = [
                f"{t.get('assignee', '?')}: {t.get('title', '')[:50]}"
                for t in open_tasks[:3]
            ]
            parts.append(f"  Open tasks: {'; '.join(task_lines)}")

        if open_qs:
            # Question aging (Phase 12)
            q_lines = []
            for q in open_qs[:2]:
                age = _days_ago(q.get("created_at"))
                age_str = f" ({age}d old)" if age is not None else ""
                q_lines.append(f"{q.get('question', '')[:50]}{age_str}")
            parts.append(f"  Open questions: {'; '.join(q_lines)}")

        context_parts.append("\n".join(parts))

    if not context_parts:
        return None

    full_context = "\n\n".join(context_parts)

    # Truncate if too long
    if len(full_context) > _MAX_CONTEXT_CHARS:
        full_context = full_context[:_MAX_CONTEXT_CHARS] + "\n  ..."

    logger.info(
        f"Built meeting continuity context: {len(recent_meetings)} meetings, "
        f"{len(full_context)} chars"
    )

    return full_context


def _days_until_review(review_date_str: str | None) -> int | None:
    """Return days until a review date, or None if unparseable."""
    if not review_date_str:
        return None
    try:
        rd = datetime.fromisoformat(str(review_date_str).replace("Z", "+00:00"))
        if rd.tzinfo is None:
            rd = rd.replace(tzinfo=timezone.utc)
        delta = rd - datetime.now(timezone.utc)
        return delta.days
    except (ValueError, TypeError):
        return None


def build_daily_continuity_context() -> dict | None:
    """
    Build continuity context for the morning brief.

    Aggregates overnight changes, pending items, and today's focus areas.
    Designed for quick consumption — returns structured data, not LLM prose.

    Returns:
        Dict with sections for morning brief integration, or None if no data.
        Keys: task_summary, approaching_reviews, aging_questions, recent_completions
    """
    try:
        return _build_daily_context_inner()
    except Exception as e:
        logger.warning(f"Daily continuity context failed: {e}")
        return None


def _build_daily_context_inner() -> dict | None:
    """Inner implementation of daily context (non-catching, for testability)."""
    sections = {}

    # 1. Overall task summary: open, overdue, recently completed
    pending = supabase_client.get_tasks(status="pending", limit=100)
    in_progress = supabase_client.get_tasks(status="in_progress", limit=100)
    done_recent = supabase_client.get_tasks(status="done", limit=50)

    # Filter recently completed (last 24h)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    recent_completions = [
        t for t in done_recent
        if t.get("updated_at") and str(t["updated_at"]) >= yesterday
    ]

    open_tasks = pending + in_progress
    overdue = [t for t in open_tasks if t.get("status") == "overdue"]

    if open_tasks or recent_completions:
        sections["task_summary"] = {
            "open": len(open_tasks),
            "in_progress": len(in_progress),
            "overdue": len(overdue),
            "completed_24h": len(recent_completions),
            "recent_completions": [
                {
                    "title": t.get("title", "")[:60],
                    "assignee": t.get("assignee", ""),
                }
                for t in recent_completions[:5]
            ],
        }

    # 2. Decisions approaching review
    try:
        approaching_decisions = supabase_client.get_decisions_for_review(days_ahead=14)
        if approaching_decisions:
            sections["approaching_reviews"] = [
                {
                    "description": d.get("description", "")[:80],
                    "review_date": str(d.get("review_date", ""))[:10],
                    "days_until": _days_until_review(d.get("review_date")),
                    "meeting_title": (d.get("meetings") or {}).get("title", ""),
                }
                for d in approaching_decisions[:5]
            ]
    except Exception as e:
        logger.debug(f"Decision review check failed: {e}")

    # 3. Aging open questions (open > 7 days)
    try:
        open_questions = supabase_client.get_open_questions(status="open", limit=50)
        aging = []
        for q in open_questions:
            age = _days_ago(q.get("created_at"))
            if age is not None and age >= 7:
                aging.append({
                    "question": q.get("question", "")[:80],
                    "raised_by": q.get("raised_by", ""),
                    "days_open": age,
                    "meeting_title": (q.get("meetings") or {}).get("title", ""),
                })
        if aging:
            # Sort by age descending
            aging.sort(key=lambda x: x["days_open"], reverse=True)
            sections["aging_questions"] = aging[:5]
    except Exception as e:
        logger.debug(f"Question aging check failed: {e}")

    if not sections:
        return None

    logger.info(f"Built daily continuity context: {list(sections.keys())}")
    return sections


def format_daily_continuity_for_brief(context: dict) -> str:
    """
    Format daily continuity context as HTML for Telegram morning brief.

    Args:
        context: Output of build_daily_continuity_context().

    Returns:
        HTML-formatted string for inclusion in morning brief.
    """
    parts = []

    # Task summary
    ts = context.get("task_summary")
    if ts:
        line = f"Tasks: {ts['open']} open ({ts['in_progress']} in progress"
        if ts["overdue"]:
            line += f", {ts['overdue']} overdue"
        line += ")"
        if ts["completed_24h"]:
            line += f"\nCompleted yesterday: {ts['completed_24h']}"
            for c in ts.get("recent_completions", [])[:3]:
                line += f"\n  - {c['title']}"
        parts.append(line)

    # Approaching reviews
    reviews = context.get("approaching_reviews")
    if reviews:
        lines = ["Decisions up for review:"]
        for r in reviews:
            days = r.get("days_until")
            days_str = f"in {days}d" if days is not None and days > 0 else "today"
            lines.append(f"  - {r['description'][:60]} ({days_str})")
        parts.append("\n".join(lines))

    # Aging questions
    aging = context.get("aging_questions")
    if aging:
        lines = ["Aging open questions:"]
        for a in aging:
            lines.append(f"  - {a['question'][:60]} ({a['days_open']}d, {a['raised_by']})")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


async def build_pre_meeting_continuity_context(
    participants: list[str],
    meeting_title: str,
) -> dict | None:
    """
    Build rich continuity context for meeting prep.

    Uses Sonnet for synthesis — produces a narrative analysis of what
    matters for this specific meeting given the participants and topic.

    Args:
        participants: Expected meeting participants.
        meeting_title: Title of the upcoming meeting.

    Returns:
        Dict with raw data and synthesized narrative, or None if no data.
        Keys: meetings, task_stats, decisions, open_questions, narrative
    """
    if not participants and not meeting_title:
        return None

    try:
        return await _build_pre_meeting_context_inner(participants, meeting_title)
    except Exception as e:
        logger.warning(f"Pre-meeting continuity context failed: {e}")
        return None


async def _build_pre_meeting_context_inner(
    participants: list[str],
    meeting_title: str,
) -> dict | None:
    """Inner implementation (non-catching, for testability)."""
    context = {}

    # 1. Recent meetings with these participants
    if participants:
        try:
            recent_meetings = supabase_client.get_meetings_by_participant_overlap(
                participants=participants,
                limit=5,
            )
            if recent_meetings:
                meeting_summaries = []
                for m in recent_meetings[:5]:
                    mid = m.get("id", "")
                    # Get per-meeting task stats
                    try:
                        all_tasks = supabase_client.get_tasks(status=None)
                        mtasks = [t for t in all_tasks if t.get("meeting_id") == mid]
                    except Exception:
                        mtasks = []

                    meeting_summaries.append({
                        "title": m.get("title", ""),
                        "date": str(m.get("date", ""))[:10],
                        "participants": m.get("participants", []),
                        "task_stats": _format_task_stats(mtasks),
                    })
                context["meetings"] = meeting_summaries
        except Exception as e:
            logger.debug(f"Meeting overlap query failed: {e}")

    # 2. Participant task status
    if participants:
        participant_tasks = {}
        for p in participants:
            try:
                tasks = supabase_client.get_tasks(assignee=p, status=None, limit=20)
                open_tasks = [t for t in tasks if t.get("status") in ("pending", "in_progress", "overdue")]
                if open_tasks:
                    participant_tasks[p] = [
                        {
                            "title": t.get("title", "")[:60],
                            "status": t.get("status", ""),
                            "deadline": str(t.get("deadline", ""))[:10] if t.get("deadline") else None,
                            "priority": t.get("priority", "M"),
                        }
                        for t in open_tasks[:5]
                    ]
            except Exception:
                pass
        if participant_tasks:
            context["participant_tasks"] = participant_tasks

    # 3. Relevant decisions (active, from meetings with these participants)
    try:
        decisions = supabase_client.list_decisions(limit=50)
        active_decisions = [d for d in decisions if d.get("decision_status") == "active"]

        # Filter to decisions involving these participants
        participant_set = {p.lower() for p in participants} if participants else set()
        relevant = []
        for d in active_decisions:
            involved = d.get("participants_involved") or []
            if participant_set and any(
                p.lower() in participant_set for p in involved
            ):
                relevant.append(d)
            elif not participant_set:
                relevant.append(d)

        # Also include decisions approaching review
        for d in active_decisions:
            if d not in relevant and d.get("review_date"):
                days = _days_until_review(d["review_date"])
                if days is not None and 0 <= days <= 14:
                    relevant.append(d)

        if relevant:
            context["decisions"] = [
                {
                    "description": d.get("description", "")[:100],
                    "rationale": (d.get("rationale") or "")[:80],
                    "review_date": str(d.get("review_date", ""))[:10] if d.get("review_date") else None,
                    "days_until_review": _days_until_review(d.get("review_date")),
                    "meeting_title": (d.get("meetings") or {}).get("title", ""),
                }
                for d in relevant[:8]
            ]
    except Exception as e:
        logger.debug(f"Decision fetch for pre-meeting context failed: {e}")

    # 4. Open questions from recent meetings with these participants
    try:
        questions = supabase_client.get_open_questions(status="open", limit=50)
        relevant_qs = []
        for q in questions:
            raised_by = (q.get("raised_by") or "").lower()
            if participant_set and any(p in raised_by for p in participant_set):
                age = _days_ago(q.get("created_at"))
                relevant_qs.append({
                    "question": q.get("question", "")[:80],
                    "raised_by": q.get("raised_by", ""),
                    "days_open": age,
                })
        if relevant_qs:
            relevant_qs.sort(key=lambda x: x.get("days_open") or 0, reverse=True)
            context["open_questions"] = relevant_qs[:5]
    except Exception as e:
        logger.debug(f"Question fetch for pre-meeting context failed: {e}")

    if not context:
        return None

    # 5. Synthesize narrative using Sonnet
    narrative = await _synthesize_pre_meeting_narrative(
        meeting_title=meeting_title,
        participants=participants,
        context=context,
    )
    if narrative:
        context["narrative"] = narrative

    logger.info(
        f"Built pre-meeting continuity context: {list(context.keys())}, "
        f"for '{meeting_title}' with {len(participants)} participants"
    )
    return context


async def _synthesize_pre_meeting_narrative(
    meeting_title: str,
    participants: list[str],
    context: dict,
) -> str | None:
    """
    Use Sonnet to produce a short narrative synthesis for meeting prep.

    Returns 3-5 sentences highlighting what matters most for this meeting.
    """
    # Build a compact data block for the LLM
    data_parts = [f"Meeting: {meeting_title}", f"Participants: {', '.join(participants)}"]

    meetings = context.get("meetings", [])
    if meetings:
        data_parts.append("Recent meetings with these participants:")
        for m in meetings[:3]:
            stats = m.get("task_stats", {})
            data_parts.append(
                f"  - {m['title']} ({m['date']}): "
                f"{stats.get('done', 0)}/{stats.get('total', 0)} tasks done"
            )

    ptasks = context.get("participant_tasks", {})
    if ptasks:
        data_parts.append("Open tasks by participant:")
        for name, tasks in ptasks.items():
            for t in tasks[:2]:
                data_parts.append(f"  - {name}: {t['title']} [{t['status']}]")

    decisions = context.get("decisions", [])
    if decisions:
        data_parts.append("Active decisions:")
        for d in decisions[:4]:
            review_str = ""
            if d.get("days_until_review") is not None:
                review_str = f" (review in {d['days_until_review']}d)"
            data_parts.append(f"  - {d['description']}{review_str}")

    qs = context.get("open_questions", [])
    if qs:
        data_parts.append("Open questions:")
        for q in qs[:3]:
            data_parts.append(f"  - {q['question']} ({q.get('days_open', '?')}d old)")

    data_block = "\n".join(data_parts)

    prompt = f"""Based on this operational context, write 3-5 sentences summarizing what matters most going into "{meeting_title}". Focus on: unfinished work these participants own, decisions approaching review, and questions that need answers. Be specific and actionable.

{data_block}"""

    system = (
        "You are a concise operations analyst. Write brief, actionable summaries. "
        "No filler, no headers. Just clear sentences about what needs attention."
    )

    try:
        response, _ = call_llm(
            prompt=prompt,
            model=settings.model_background,
            max_tokens=500,
            system=system,
            call_site="pre_meeting_continuity_synthesis",
        )
        return response.strip() if response else None
    except Exception as e:
        logger.warning(f"Pre-meeting narrative synthesis failed: {e}")
        return None
