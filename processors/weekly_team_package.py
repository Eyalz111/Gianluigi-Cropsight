"""
Weekly team package — the on-demand, tier-filtered team email (v2.5 Phase 3, chunk 4).

Built ONLY when Eyal taps [📤 Send to team] and confirms. This is a SEPARATE
builder from the Eyal pulse (processors/weekly_pulse.py), not a filter over it —
the leak-critical move. Every content type has its own founders-cap (max_level=3)
rule so the 32 CEO-tier topics never reach the team copy:

  - Recap      — decisions/tasks filtered BEFORE counting+listing (meeting COUNT is
                 coarse metadata → counts all meetings).
  - Per-area   — include the synthesized strategic_state only if the AreaBrief is
                 founders-safe; a CEO-tier area shows only its founders-safe child
                 topics (rebuilt from safe primitives, never the contaminated blob);
                 an all-CEO area produces NO line (team copy is variable-length).
  - Signal     — bundled only if status == "distributed" AND fresh (<14 days).
  - Gantt      — a link (inherently safe).

Sections NEEDS YOUR CALL and MOVED THIS WEEK are OMITTED entirely — the framing
itself leaks CEO decision cadence.

The confirm dialog's contents list is derived from the SAME build() that produces
the email, so they can never drift.
"""

import logging
from datetime import datetime, timedelta, timezone

from config.settings import settings
from models.schemas import filter_by_sensitivity
from processors.weekly_pulse import fetch_areas_with_health, _brief_level, _esc
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_FOUNDERS = 3  # max_level cap for the founding team (drops CEO-tier only)
_SIGNAL_FRESH_DAYS = 14


def _team_recipients() -> list[str]:
    """The founding team, Eyal excluded (this is the team-facing copy).

    Roster-driven (Founders band minus Eyal) so new founders like Matti are
    included without a code change. [distribution-groups 2026-07-05] — was the
    hardcoded Roye/Paolo/Yoram env triple.
    """
    from guardrails.distribution import recipients_for_band

    return recipients_for_band("founders", exclude_eyal=True)


def _area_team_lines(areas: list[dict]) -> list[str]:
    """Per-area WHERE WE STAND for the team copy (founders-cap, CEO-safe).

    Variable-length: an all-CEO-tier area with no founders-safe children is omitted.
    """
    lines: list[str] = []
    for a in areas:
        if _brief_level(a.get("brief") or {}) <= _FOUNDERS:
            # Area brief is founders-safe → its synthesized headline is shareable.
            lines.append(f"<b>{_esc(a['name'])}</b> — {_esc(a['strategic_state'])}")
        else:
            # CEO-tier area: never reuse the contaminated narrative. Rebuild from
            # founders-safe child topics only.
            safe = [c for c in (a.get("children") or []) if _brief_level(c.get("brief") or {}) <= _FOUNDERS]
            if not safe:
                continue  # nothing safe to say about this area → omit it
            names = ", ".join(_esc(c.get("name", "")) for c in safe if c.get("name"))
            lines.append(f"<b>{_esc(a['name'])}</b>: {names}")
    return lines


def _signal_section(signal: dict | None) -> tuple[bool, str, str]:
    """(included, confirm_label, html) for the Intelligence Signal.

    Included only if it was already distributed (cleared by Eyal's own signal gate)
    AND it's fresh (<14 days) — a months-old signal labeled 'Week N' reads as stale.
    """
    if not signal:
        return (False, "", "")
    if signal.get("status") != "distributed":
        return (False, "", "")
    url = (signal.get("drive_doc_url") or "").strip()
    if not url:
        return (False, "", "")
    # Freshness guard.
    created = signal.get("created_at") or ""
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - dt) > timedelta(days=_SIGNAL_FRESH_DAYS):
            return (False, "", "")
    except (ValueError, TypeError):
        return (False, "", "")
    week = signal.get("week_number")
    label = f"Intelligence Signal (Week {week})" if week else "Intelligence Signal"
    html = f'<b>{_esc(label)}</b>: <a href="{_esc(url)}">read the latest signal</a>'
    return (True, label, html)


async def build_team_package(week_start: datetime) -> dict:
    """Build the tier-filtered team email. Returns subject/body/html_body/contents/recipients.

    `contents` is the single source of truth for the confirm dialog's "Includes:" list.
    """
    from processors.weekly_digest import (
        get_meetings_for_week, get_decisions_for_week, get_task_summary,
    )

    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    week_of = week_start.strftime("%Y-%m-%d")

    # --- Recap (founders-filtered decisions/tasks; meeting COUNT counts all) ---
    meetings = await get_meetings_for_week(week_start, week_end)
    decisions = filter_by_sensitivity(await get_decisions_for_week(week_start, week_end), _FOUNDERS)
    task_summary = await get_task_summary()
    done = filter_by_sensitivity(task_summary.get("completed_this_week", []), _FOUNDERS)
    overdue = filter_by_sensitivity(task_summary.get("overdue", []), _FOUNDERS)

    contents = ["recap", "area status"]

    parts: list[str] = []
    parts.append(f"<h2>CropSight — week of {week_of}</h2>")
    parts.append(
        f"<p>{len(meetings)} meetings · {len(decisions)} decisions · "
        f"{len(done)} tasks done · {len(overdue)} overdue this week.</p>"
    )
    if decisions:
        parts.append("<b>Decisions this week</b><ul>")
        for d in decisions[:10]:
            parts.append(f"<li>{_esc(d.get('description') or d.get('title') or '')}</li>")
        parts.append("</ul>")

    # --- Per-area WHERE WE STAND (founders-cap, variable-length) ---
    area_lines = _area_team_lines(fetch_areas_with_health())
    parts.append("<b>Where we stand</b>")
    if area_lines:
        parts.append("<ul>" + "".join(f"<li>{ln}</li>" for ln in area_lines) + "</ul>")
    else:
        parts.append("<p><i>No shareable area updates this week.</i></p>")

    # --- Intelligence Signal (status + freshness gated) ---
    try:
        signal = supabase_client.get_latest_intelligence_signal()
    except Exception:
        signal = None
    sig_included, sig_label, sig_html = _signal_section(signal)
    if sig_included:
        parts.append(f"<p>{sig_html}</p>")
        contents.append(sig_label)

    # --- Gantt link (always; inherently safe) ---
    if settings.GANTT_SHEET_ID:
        gantt_url = f"https://docs.google.com/spreadsheets/d/{settings.GANTT_SHEET_ID}/edit"
        parts.append(f'<p><b>Gantt</b>: <a href="{gantt_url}">open the board</a></p>')
        contents.append("Gantt link")

    html_body = "\n".join(parts)
    plain_body = (
        f"CropSight — week of {week_of}\n"
        f"{len(meetings)} meetings, {len(decisions)} decisions, "
        f"{len(done)} tasks done, {len(overdue)} overdue this week.\n"
        "(Open the HTML email for the full weekly package.)"
    )
    return {
        "subject": f"CropSight weekly — week of {week_of}",
        "html_body": html_body,
        "body": plain_body,
        "contents": contents,
        "recipients": _team_recipients(),
    }


async def team_package_contents(week_start: datetime) -> list[str]:
    """The 'Includes:' list for the confirm dialog — derived from the same build()."""
    pkg = await build_team_package(week_start)
    return pkg.get("contents", [])


async def send_team_package(week_start: datetime) -> bool:
    """Build + email the tier-filtered package to the founding team."""
    from services.gmail import gmail_service

    pkg = await build_team_package(week_start)
    recipients = pkg["recipients"]
    if not recipients:
        logger.warning("[team_package] no team recipients configured — not sending")
        return False
    ok = await gmail_service.send_email(
        to=recipients,
        subject=pkg["subject"],
        body=pkg["body"],
        html_body=pkg["html_body"],
    )
    try:
        supabase_client.log_action(
            action="weekly_team_package_sent",
            details={"week_of": week_start.strftime("%Y-%m-%d"), "recipients": recipients,
                     "contents": pkg["contents"], "ok": bool(ok)},
            triggered_by="eyal",
        )
    except Exception as e:
        logger.warning(f"[team_package] audit log failed: {e}")
    return bool(ok)
