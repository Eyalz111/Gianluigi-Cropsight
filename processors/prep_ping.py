"""
Meeting-prep "Prep Ping" (v2.5 Phase 3, chunk 3).

Push-first, cheap-by-default meeting prep:
- Tier 1: a deterministic PING (no LLM) ~90 min before a meeting. Anchored on
  the PARTICIPANTS (their open/overdue tasks) first, with optional TOPIC
  enrichment (the live topic brief's current_status). Honest give-up nudge when
  there's nothing to say.
- Tier 2: an on-demand "Prepare me" brief (the only LLM — Haiku), re-gathered
  fresh on tap.

Eyal-only (no approval gate). Tasks are read at CEO tier — hide nothing from him.
Attendees are resolved BY EMAIL (stable) to a canonical first name, never by the
calendar displayName (which may be Hebrew). All DB reads are SYNC.
"""

import logging
from datetime import date, datetime, timezone
from html import escape as _esc

from config.team import TEAM_MEMBERS, _normalize_email, get_team_member_by_email
from models.schemas import filter_by_sensitivity
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_CEO_LEVEL = 4  # ping/brief go only to Eyal → read everything

# Eyal's identities (lowercased) — to exclude him from "the other people" and to
# gate "is Eyal attending".
EYAL_IDENTITIES = {
    (i or "").lower().strip()
    for i in (TEAM_MEMBERS.get("eyal", {}).get("identities") or [])
    if i
}


def _is_eyal_email(email: str) -> bool:
    return _normalize_email(email) in EYAL_IDENTITIES


def _att_email(a) -> str:
    """Extract an email from an attendee that may be a dict OR a plain string.

    Google Calendar normally returns `[{email, displayName, ...}, ...]`, but some
    events come back as `["someone@x.com", ...]` — calling `.get(...)` on the
    string crashes. This guard returns "" for anything unexpected.
    """
    if isinstance(a, dict):
        return (a.get("email") or "").strip()
    if isinstance(a, str):
        return a.strip()
    return ""


def _att_display(a) -> str:
    """Extract a display name from an attendee (dict or string)."""
    if isinstance(a, dict):
        return (a.get("displayName") or "").strip()
    return ""


def eyal_is_attendee(event: dict) -> bool:
    """True if Eyal is an attendee/organizer — only prep meetings he's actually in."""
    if _is_eyal_email(event.get("organizer", "") or ""):
        return True
    return any(_is_eyal_email(_att_email(a)) for a in (event.get("attendees") or []))


def _first_name(member: dict) -> str:
    """Canonical first name for task lookup (strips titles like 'Prof.')."""
    parts = [p for p in (member.get("name") or "").split() if not p.endswith(".")]
    return (parts[0] if parts else (member.get("name") or "").split()[0]).strip()


def _minutes_until(start_iso: str | None) -> float | None:
    if not start_iso:
        return None
    try:
        start = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return (start - datetime.now(timezone.utc)).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def _hhmm(start_iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(start_iso).replace("Z", "+00:00")).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _participant_open_tasks(first_name: str) -> list[dict]:
    """Open (pending + in_progress) tasks for a person, CEO-tier (Eyal sees all)."""
    try:
        tasks = supabase_client.get_tasks(assignee=first_name, status="pending", limit=50)
        tasks += supabase_client.get_tasks(assignee=first_name, status="in_progress", limit=50)
    except Exception as e:
        logger.warning(f"prep ping: task lookup for {first_name} failed: {e}")
        return []
    return filter_by_sensitivity(tasks, _CEO_LEVEL)


def anchor_event(event: dict) -> dict:
    """Resolve a calendar event to {participants:[{first,display}], topic:{...}|None}.

    Participants: each non-Eyal attendee resolved BY EMAIL → team member → first
    name (display kept only for rendering). External attendees (no member) skipped.
    Topic: title → canonical → topic thread brief (only if it has a current_status).
    """
    participants: list[dict] = []
    seen: set[str] = set()
    for a in (event.get("attendees") or []):
        email = _att_email(a)
        if not email or _is_eyal_email(email):
            continue
        member = get_team_member_by_email(email)
        if not member:
            continue  # external attendee — graceful skip
        first = _first_name(member)
        if not first or first.lower() in seen:
            continue
        seen.add(first.lower())
        display = _att_display(a) or first
        participants.append({"first": first, "display": display})

    topic = None
    try:
        from processors.topic_threading import _match_canonical_name, _find_thread_by_name
        title = event.get("title") or ""
        canonical = _match_canonical_name(title) or title
        thread = _find_thread_by_name(canonical) or _find_thread_by_name(title)
        brief = (thread or {}).get("brief_json") or {}
        if brief.get("current_status"):
            topic = {
                "name": thread.get("topic_name") or canonical,
                "status": brief.get("current_status"),
                "open_items": [
                    oi.get("description") for oi in (brief.get("open_items") or [])[:2]
                    if oi.get("description")
                ],
                "sensitivity": brief.get("sensitivity"),
            }
    except Exception as e:
        logger.debug(f"prep ping: topic anchor skipped: {e}")

    return {"participants": participants, "topic": topic}


def gather_ping_context(event: dict) -> dict:
    """Deterministic (NO LLM) ping context. Participant tasks + topic enrichment."""
    anchored = anchor_event(event)
    today = date.today().isoformat()

    people: list[dict] = []
    any_overdue = False
    for p in anchored["participants"]:
        tasks = _participant_open_tasks(p["first"])
        if not tasks:
            continue
        overdue = [t.get("title", "") for t in tasks if t.get("deadline") and t["deadline"] < today]
        if overdue:
            any_overdue = True
        people.append({"display": p["display"], "open": len(tasks), "overdue": overdue})

    topic = anchored["topic"]
    topic_blocked = bool(topic and topic.get("status") in ("blocked", "pending_decision"))

    return {
        "title": event.get("title") or "Meeting",
        "time": _hhmm(event.get("start")),
        "minutes_until": _minutes_until(event.get("start")),
        "people": people,
        "topic": topic,
        "change_flag": any_overdue or topic_blocked,
        "give_up": not people and topic is None,
        "recurring": bool(event.get("recurring_event_id")),
    }


def format_ping_text(ctx: dict) -> str:
    """Render the Tier-1 ping (HTML). Give-up degrades to a one-line nudge."""
    title = _esc(ctx["title"])
    when = ctx["time"]
    mins = ctx.get("minutes_until")
    when_str = f"{when}" + (f" (in ~{int(mins)} min)" if isinstance(mins, (int, float)) else "")
    head = f"🗓 <b>{title}</b> — {when_str}"

    if ctx["give_up"]:
        # Recurring standups get the quietest form.
        return head + "\nNothing flagged on this one."

    lines = [head]
    for p in ctx["people"]:
        name = _esc(p["display"])
        if p["overdue"]:
            od = _esc(p["overdue"][0])
            lines.append(f"{name}: {p['open']} open, {len(p['overdue'])} overdue — “{od}”.")
        else:
            lines.append(f"{name}: {p['open']} open.")
    if ctx["topic"]:
        t = ctx["topic"]
        oi = f" ({len(t['open_items'])} open items)" if t.get("open_items") else ""
        lines.append(f"Topic: {_esc(t['name'])} — {_esc(t['status'])}{oi}.")
    return "\n".join(lines)


async def synthesize_prepare_brief(event: dict) -> str:
    """Tier-2 on-demand brief (the only LLM — Haiku). Re-gathers fresh; never raises.

    Falls back to a deterministic assembly of the same gathered data on LLM failure.
    """
    from config.settings import settings

    anchored = anchor_event(event)
    title = event.get("title") or "Meeting"
    topic = anchored["topic"]
    topic_name = topic["name"] if topic else title

    # Reuse the existing gather fns (decisions/questions/continuity) — all bounded.
    decisions, questions, continuity = [], [], ""
    try:
        from processors.meeting_prep import find_relevant_decisions, _find_open_questions
        decisions = await find_relevant_decisions(topic_name, limit=5, max_sensitivity_level=_CEO_LEVEL)
        questions = _find_open_questions(topic_name, limit=5, max_sensitivity_level=_CEO_LEVEL)
    except Exception as e:
        logger.debug(f"prepare brief: decisions/questions gather skipped: {e}")
    try:
        from processors.meeting_continuity import build_meeting_continuity_context  # LLM-FREE (:67)
        participant_first = [p["first"] for p in anchored["participants"]]
        # signature is (participants, current_meeting_id, max_sensitivity_level) —
        # the old call passed (title_str, participants_list), so it raised and the
        # continuity block was ALWAYS silently empty. [audit P2-05]
        continuity = build_meeting_continuity_context(participant_first, None, _CEO_LEVEL) or ""
    except Exception as e:
        logger.debug(f"prepare brief: continuity skipped: {e}")

    people = []
    for p in anchored["participants"]:
        tasks = _participant_open_tasks(p["first"])
        people.append((p["display"], tasks))

    # Deterministic fallback body (also the input the LLM polishes).
    facts: list[str] = []
    for display, tasks in people:
        if tasks:
            od = [t.get("title", "") for t in tasks if t.get("deadline") and t["deadline"] < date.today().isoformat()]
            facts.append(f"{display}: {len(tasks)} open task(s)" + (f"; overdue: {', '.join(od[:2])}" if od else ""))
    if topic:
        facts.append(f"Topic '{topic['name']}' — {topic['status']}" + (f"; open: {'; '.join(topic['open_items'])}" if topic.get("open_items") else ""))
    for d in decisions[:3]:
        facts.append(f"Decision: {(d.get('description') or '')[:120]}")
    for q in questions[:3]:
        facts.append(f"Open question: {(q.get('question') or '')[:120]}")
    fallback = f"<b>Prep — {_esc(title)}</b>\n" + ("\n".join(f"• {_esc(f)}" for f in facts) if facts else "Nothing notable on file.")

    if not facts and not continuity:
        return fallback

    try:
        import asyncio
        from core.llm import call_llm

        system = (
            "You are Gianluigi, prepping the CEO 90 minutes before a meeting. Write a "
            "TIGHT brief (6–12 short lines): where things stand with these people, "
            "what's open or owed, what changed, and 1–2 things to push. Use ONLY the "
            "supplied facts — never invent. No greeting, no fluff."
        )
        prompt = (
            f"Meeting: {title}\nAttendees: {', '.join(p[0] for p in people) or 'n/a'}\n\n"
            f"FACTS:\n" + "\n".join(f"- {f}" for f in facts)
            + (f"\n\nCONTINUITY:\n{continuity[:1500]}" if continuity else "")
        )

        def _run() -> str:
            text, _usage = call_llm(
                prompt=prompt, model=settings.model_simple, max_tokens=600,
                system=system, call_site="prep_brief",
            )
            return text

        brief = (await asyncio.to_thread(_run)).strip()
        return brief or fallback
    except Exception as e:
        logger.warning(f"prepare brief synthesis failed, using fallback: {e}")
        return fallback
