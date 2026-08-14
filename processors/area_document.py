"""The area document — where we stand, what we don't know, what changed.

Eyal, 2026-08-14: *"a knowledge system, not just a CRM."* The difference is one
thing: a CRM stores records and lets you query them; a knowledge system holds a
POSITION — what we currently believe, what is still unknown, and what moved.

`areas.brief_json` has held that position for all six areas since 2026-08-09,
refreshed weekly by knowledge_weekly_scheduler. It has never been surfaced
anywhere: no doc, no tab, no link. This module is the surface.

THE LOAD-BEARING CONSTRAINT
---------------------------
Eyal: *"i dont want assertion just analysis of given information … if a meeting
dosent discuss funding so the system will assert that we finished with funding —
thats a bad assertion!"*

Absence of evidence is not evidence of absence. So this module never asks a model
whether something is still true. It renders two kinds of line, and keeps them
visibly apart:

  OBSERVATION — a date, a count, a row that exists. Computed here, never written
                by an LLM. "last discussed 65 days ago" is a fact about the
                record, not a claim about the company.
  JUDGEMENT   — the synthesised narrative, clearly attributed as an assessment
                and carrying the date it was made.

That split is intelligence tradecraft standard #3 (distinguish information from
assumption), and it is the same instinct as the Gantt legacy overlay: show the
evidence, never infer the link.

WHY AGEING IS RENDERED, NOT RESOLVED
------------------------------------
A point nobody has touched for six weeks renders with its age attached and its
content unchanged. It is not deleted (that loses knowledge), not refreshed (that
invents it), and not re-judged (that is the question which produces bad
assertions). Tradecraft standard #7 — explain change OR consistency — is why
silence gets a number instead of disappearing.
"""

import logging
from datetime import datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

ISRAEL_TZ = timezone(timedelta(hours=3))

# Age at which an untouched line gets a visible marker. Not a cutoff — nothing is
# hidden at any age. The marker exists so that "we haven't looked at this since
# June" is readable at a glance rather than requiring arithmetic.
_AGEING_DAYS = 30

_RULE = "─" * 72


def _age_days(iso) -> "int | None":
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).days
    except (ValueError, TypeError):
        return None


def _ago(iso) -> str:
    """A human age, or "" when there is no date to report.

    Never guesses. A missing date renders as nothing rather than as "unknown",
    which would read as a finding.
    """
    d = _age_days(iso)
    if d is None:
        return ""
    if d == 0:
        return "today"
    if d == 1:
        return "yesterday"
    if d < 60:
        return f"{d} days ago"
    return f"{d // 30} months ago"


def _ddmmyyyy(iso) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return ""


def gather(area_id: str) -> dict:
    """Everything this area's document is built from. Read-only.

    Deliberately wider than `synthesize_area_brief`, which sees only topic
    briefs: a position on an area that ignores its decisions and its open
    questions is a summary of conversations, not of the work.
    """
    c = supabase_client.client
    area = (c.table("areas").select("*").eq("id", area_id)
            .limit(1).execute().data or [{}])[0]

    topics = (c.table("topic_threads")
              .select("topic_name,status,brief_json,last_updated,meeting_count")
              .eq("area_id", area_id).execute().data or [])

    # Decisions and questions carry no area_id yet (migrate_area_knowledge.sql),
    # so they are reached through the project and topic names that DO map. Any
    # that cannot be reached are simply absent — never guessed into this area.
    projects = (c.table("canonical_projects").select("name")
                .eq("area_id", area_id).execute().data or [])
    names = {str(p["name"]).strip().lower() for p in projects}
    names |= {str(t["topic_name"]).strip().lower() for t in topics if t.get("topic_name")}

    decisions = [d for d in (c.table("decisions")
                 .select("id,description,rationale,label,created_at,valid_to,"
                         "superseded_by,decision_status,meeting_id")
                 .is_("valid_to", "null").eq("approval_status", "approved")
                 .limit(2000).execute().data or [])
                 if str(d.get("label") or "").strip().lower() in names]

    superseded = [d for d in (c.table("decisions")
                  .select("id,description,label,created_at,superseded_at")
                  .not_.is_("valid_to", "null")
                  .limit(2000).execute().data or [])
                  if str(d.get("label") or "").strip().lower() in names]

    questions = [q for q in (c.table("open_questions")
                 .select("id,question,raised_by,created_at,status,status_reason,label")
                 .eq("approval_status", "approved")
                 .limit(2000).execute().data or [])
                 if str(q.get("label") or "").strip().lower() in names
                 and q.get("status") not in ("resolved", "closed", "answered")]

    return {"area": area, "topics": topics, "decisions": decisions,
            "superseded": superseded, "questions": questions}


def render(data: dict) -> str:
    """The document, as plain text for Drive's Doc conversion.

    Plain text on purpose: `google_drive.upload_text_as_doc` converts text/plain,
    so markdown would arrive as literal asterisks. Structure comes from spacing
    and caps, which survive the conversion intact.
    """
    area = data["area"]
    brief = area.get("brief_json") or {}
    topics = data["topics"]
    out: list[str] = []

    name = area.get("name") or "(area)"
    owner = (area.get("owner") or "").strip()
    out.append(name.upper())
    line = f"Area knowledge · generated {datetime.now(ISRAEL_TZ):%d/%m/%Y %H:%M}"
    if owner:
        line += f" · owner {owner}"
    out.append(line)
    out.append(_RULE)
    out.append("")

    # ---- OBSERVED ---------------------------------------------------------
    # Facts about the record, computed here. No model wrote any of this, which
    # is why it can be trusted as a floor under everything below it.
    ages = [a for a in (_age_days(t.get("last_updated")) for t in topics)
            if a is not None]
    out.append("OBSERVED")
    out.append(f"  {len(topics)} topics · {len(data['decisions'])} live decisions · "
               f"{len(data['questions'])} open questions")
    if ages:
        newest = min(ages)
        out.append(f"  Most recent activity on any topic: {_ago_days(newest)}")
        if newest > _AGEING_DAYS:
            out.append(f"  ⚠ Nothing in this area has been touched for {newest} days.")
    out.append("")

    # ---- ASSESSMENT -------------------------------------------------------
    # The synthesised position. Labelled as an assessment and carrying the date
    # it was made, so it is never mistaken for something observed today.
    narrative = (brief.get("narrative") or "").strip()
    state = (brief.get("strategic_state") or "").strip()
    if narrative or state:
        made = brief.get("last_synthesized_at") or area.get("brief_updated_at")
        stamp = f" (assessed {_ddmmyyyy(made)}" + (f", {_ago(made)})" if _ago(made) else ")")
        out.append("ASSESSMENT" + stamp)
        if state:
            out.append(f"  {state}")
            out.append("")
        for para in _wrap(narrative):
            out.append(f"  {para}")
        out.append("")

    # ---- WHERE WE STAND ---------------------------------------------------
    facts = brief.get("facts") or []
    if facts:
        out.append("WHERE WE STAND")
        for f in facts:
            text = (f.get("text") or "").strip() if isinstance(f, dict) else str(f)
            if not text:
                continue
            cite = (f.get("citation") if isinstance(f, dict) else None) or ""
            # A fact with no citation is still shown, but marked. Dropping it
            # would hide something a person wrote; presenting it as sourced
            # would be the assertion this whole design refuses.
            out.append(f"  • {text}" + (f"   [{cite}]" if cite else "   [uncited]"))
        out.append("")

    # ---- SETTLED ----------------------------------------------------------
    if data["decisions"] or data["superseded"]:
        out.append("SETTLED")
        for d in sorted(data["decisions"],
                        key=lambda x: str(x.get("created_at") or ""), reverse=True)[:25]:
            desc = (d.get("description") or "").strip()
            out.append(f"  ✓ {desc}")
            out.append(f"      {_ddmmyyyy(d.get('created_at'))} · decision {str(d['id'])[:8]}")
            why = (d.get("rationale") or "").strip()
            # A decision without a WHY cannot be revisited — 123 of 309 have
            # none, so the gap is named rather than quietly left blank.
            out.append(f"      why: {why}" if why else "      why: — not recorded —")
        for d in sorted(data["superseded"],
                        key=lambda x: str(x.get("superseded_at") or ""), reverse=True)[:10]:
            out.append(f"  ~ superseded: {(d.get('description') or '').strip()[:90]}")
            out.append(f"      {_ddmmyyyy(d.get('superseded_at'))}")
        out.append("")

    # ---- OPEN -------------------------------------------------------------
    qs = data["questions"]
    out.append(f"OPEN — {len(qs)}")
    if not qs:
        out.append("  (none recorded against this area)")
    for q in sorted(qs, key=lambda x: str(x.get("created_at") or "")):
        text = (q.get("question") or "").strip()
        who = (q.get("raised_by") or "").strip()
        age = _ago(q.get("created_at"))
        tail = " · ".join(x for x in (who, f"raised {age}" if age else "") if x)
        out.append(f"  ? {text}")
        if tail:
            out.append(f"      {tail}")
        if q.get("status") == "stale":
            # The honest word for what the system did. `stale` is not a finding
            # about the question — it is a record of us not returning to it.
            out.append("      ⚠ aged out of the open list — not answered, just not revisited")
    out.append("")

    # ---- TOPIC ACTIVITY ---------------------------------------------------
    # Dates only. The judgement about what the dates MEAN belongs in ASSESSMENT
    # above, attributed and stamped; here they are only ever numbers.
    if topics:
        out.append("TOPIC ACTIVITY (observed)")
        for t in sorted(topics, key=lambda x: _age_days(x.get("last_updated")) or 9999):
            a = _age_days(t.get("last_updated"))
            mark = "  ⚠" if (a or 0) > _AGEING_DAYS else "   "
            out.append(f" {mark} {str(t.get('topic_name') or '')[:44]:44} "
                       f"last discussed {_ago(t.get('last_updated')) or '—'}")
        out.append("")

    out.append(_RULE)
    out.append("Generated by Gianluigi. Observations are computed from the record;")
    out.append("the assessment is a synthesis and carries the date it was made.")
    return "\n".join(out)


def _ago_days(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago" if days < 60 else f"{days // 30} months ago"


def _wrap(text: str, width: int = 88) -> list[str]:
    """Soft-wrap a paragraph. Docs re-flows, but long lines are unreadable in
    the plain-text intermediate and in any log that echoes it."""
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def build_area_document(area_id: str) -> str:
    """Gather + render. The one entry point."""
    return render(gather(area_id))
