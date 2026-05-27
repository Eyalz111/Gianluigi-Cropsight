"""
Weekly Pulse — the deterministic Friday report (v2.5 Phase 3, chunk 4).

A *view* over the knowledge layer the brain already maintains — NO LLM in the
push. It assembles five sections from already-synthesized area/topic briefs:

  1. Recap line          — N meetings, N decisions this week (cheap counts)
  2. WHERE WE STAND      — all areas: health emoji + the area's strategic_state
  3. NEEDS YOUR CALL     — pending-decision + blocked topics (the alignment check)
  4. MOVED THIS WEEK     — topics touched in meetings this week (activity only)
  5. HOUSEKEEPING        — one line: N topics gone quiet 30+ days

The Eyal report is Eyal-only, so it is NOT sensitivity-filtered (he sees all
tiers). The team-facing copy is a separate, tier-filtered builder — see
processors/weekly_team_package.py. Keeping them apart is deliberate: never
filter a downstream-contaminated synthesized string, rebuild from safe
primitives at construction time.
"""

import logging
from datetime import datetime, timedelta, timezone

from models.schemas import TIER_LEVELS
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# A distinctive substring in the report header. The message-reply handler keys
# the "flag for next review" capture on this (reply-to + marker), so a stray
# "thanks" reply to some other message is never mistaken for a pulse note.
PULSE_REPLY_MARKER = "CropSight — week of"


# =============================================================================
# Small helpers (deterministic, no LLM)
# =============================================================================

def _load_brief(raw) -> dict:
    """brief_json may arrive as a dict (jsonb) or a JSON string — normalise to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        import json
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _brief_level(brief: dict) -> int:
    """Numeric sensitivity tier (1-4) of a brief; missing → founders (3)."""
    return TIER_LEVELS.get(brief.get("sensitivity", "founders"), 3)


def _area_health_emoji(child_statuses: list[str]) -> str:
    """Deterministic area health from child topic statuses.

    🔴 any child blocked · 🟡 else any pending_decision · 🟢 else · ⚪ no children
    (⚪ = no signal, which is not the same as healthy).
    """
    if not child_statuses:
        return "⚪"  # white circle — no signal
    if "blocked" in child_statuses:
        return "\U0001f534"  # red
    if "pending_decision" in child_statuses:
        return "\U0001f7e1"  # yellow
    return "\U0001f7e2"  # green


def _attention_detail(brief: dict) -> str:
    """The open question / blocker to show next to a topic in NEEDS YOUR CALL."""
    for it in (brief.get("open_items") or []):
        if it.get("kind") in ("question", "blocker") and it.get("description"):
            return str(it["description"]).strip()
    risks = brief.get("risks") or []
    if risks:
        return str(risks[0]).strip()
    items = brief.get("open_items") or []
    if items and items[0].get("description"):
        return str(items[0]["description"]).strip()
    return (brief.get("narrative") or "").strip()[:120]


def _strategic_line(brief: dict) -> str:
    """The one-line area headline; fall back to a narrative slice."""
    ss = (brief.get("strategic_state") or "").strip()
    if ss:
        return ss
    return (brief.get("narrative") or "").strip()[:120] or "—"


# =============================================================================
# Data gathering
# =============================================================================

def fetch_areas_with_health() -> list[dict]:
    """All active areas → {name, emoji, strategic_state, brief, child_statuses}.

    Mirrors the area→child-topics query shape from knowledge_synthesis.
    """
    out: list[dict] = []
    try:
        areas = supabase_client.get_areas(status="active") or []
    except Exception as e:
        logger.warning(f"[pulse] get_areas failed: {e}")
        return out

    for area in areas:
        try:
            children = (
                supabase_client.client.table("topic_threads")
                .select("topic_name, brief_json")
                .eq("area_id", area["id"])
                .execute()
                .data
                or []
            )
        except Exception:
            children = []
        child_statuses, child_briefs = [], []
        for c in children:
            cb = _load_brief(c.get("brief_json"))
            if cb:
                child_statuses.append(cb.get("current_status", "active"))
                child_briefs.append({"name": c.get("topic_name", ""), "brief": cb})
        area_brief = _load_brief(area.get("brief_json"))
        out.append({
            "name": area.get("name", ""),
            "emoji": _area_health_emoji(child_statuses),
            "strategic_state": _strategic_line(area_brief),
            "brief": area_brief,
            "children": child_briefs,
        })
    return out


def classify_attention_topics() -> dict:
    """All briefed topics → {pending_decision: [...], blocked: [...], stale_count}.

    Each surfaced topic carries {name, detail}. Mirrors _build_reflection's scan.
    """
    result = {"pending_decision": [], "blocked": [], "stale_count": 0}
    try:
        rows = (
            supabase_client.client.table("topic_threads")
            .select("topic_name, brief_json")
            .not_.is_("brief_json", "null")
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.warning(f"[pulse] attention scan failed: {e}")
        return result

    for r in rows:
        brief = _load_brief(r.get("brief_json"))
        status = brief.get("current_status")
        name = r.get("topic_name", "")
        if status == "blocked":
            result["blocked"].append({"name": name, "detail": _attention_detail(brief)})
        elif status == "pending_decision":
            result["pending_decision"].append({"name": name, "detail": _attention_detail(brief)})
        elif status == "stale":
            result["stale_count"] += 1
    return result


def fetch_moved_this_week(week_start: datetime, week_end: datetime) -> list[str]:
    """Topic names touched in meetings this week (activity only).

    Pure activity signal — carried blockers/decisions live in NEEDS YOUR CALL.
    """
    try:
        ws = week_start.replace(tzinfo=timezone.utc).isoformat() if week_start.tzinfo is None else week_start.isoformat()
        we = week_end.replace(tzinfo=timezone.utc).isoformat() if week_end.tzinfo is None else week_end.isoformat()
        mentions = (
            supabase_client.client.table("topic_thread_mentions")
            .select("topic_id")
            .gte("created_at", ws)
            .lte("created_at", we)
            .execute()
            .data
            or []
        )
        ids = list({m["topic_id"] for m in mentions if m.get("topic_id")})
        if not ids:
            return []
        rows = (
            supabase_client.client.table("topic_threads")
            .select("topic_name")
            .in_("id", ids)
            .execute()
            .data
            or []
        )
        return [r.get("topic_name", "") for r in rows if r.get("topic_name")]
    except Exception as e:
        logger.warning(f"[pulse] moved-this-week scan failed: {e}")
        return []


async def recap_counts(week_start: datetime, week_end: datetime) -> dict:
    """Cheap week counts for the Eyal report (no tier filter — Eyal sees all)."""
    from processors.weekly_digest import get_meetings_for_week, get_decisions_for_week
    try:
        meetings = await get_meetings_for_week(week_start, week_end)
        decisions = await get_decisions_for_week(week_start, week_end)
        return {"meetings": len(meetings), "decisions": len(decisions)}
    except Exception as e:
        logger.warning(f"[pulse] recap counts failed: {e}")
        return {"meetings": 0, "decisions": 0}


# =============================================================================
# Assembly + formatting (HTML for Telegram)
# =============================================================================

def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_pulse_text(
    week_of: str,
    recap: dict,
    areas: list[dict],
    attention: dict,
    moved: list[str],
) -> str:
    lines: list[str] = []
    lines.append(f"\U0001f4ca <b>CropSight — week of {week_of}</b>")
    lines.append(f"{recap.get('meetings', 0)} meetings · {recap.get('decisions', 0)} decisions this week")
    lines.append("")

    # WHERE WE STAND — all areas
    lines.append("<b>WHERE WE STAND</b>")
    if areas:
        for a in areas:
            lines.append(f"{a['emoji']} <b>{_esc(a['name'])}</b> — {_esc(a['strategic_state'])}")
    else:
        lines.append("<i>No area briefs yet.</i>")
    lines.append("")

    # NEEDS YOUR CALL — blocked first, then pending-decision (cap each at 5)
    blocked = attention.get("blocked", [])
    pending = attention.get("pending_decision", [])
    if blocked or pending:
        lines.append("\U0001f514 <b>NEEDS YOUR CALL</b>")
        for t in blocked[:5]:
            detail = f" — {_esc(t['detail'])}" if t.get("detail") else ""
            lines.append(f"• <b>Blocked:</b> {_esc(t['name'])}{detail}")
        for t in pending[:5]:
            detail = f" — {_esc(t['detail'])}" if t.get("detail") else ""
            lines.append(f"• <b>Decision:</b> {_esc(t['name'])}{detail}")
        extra = max(0, len(blocked) - 5) + max(0, len(pending) - 5)
        if extra:
            lines.append(f"<i>+{extra} more — open CropSight Ops in Claude.ai</i>")
        lines.append("<i>Reply to flag any of these for next review.</i>")
        lines.append("")

    # MOVED THIS WEEK — activity only
    lines.append("\U0001f525 <b>MOVED THIS WEEK</b>")
    if moved:
        lines.append(", ".join(_esc(m) for m in moved[:12]))
    else:
        lines.append("Quiet week — no topics moved.")
    lines.append("")

    # HOUSEKEEPING — one line
    stale = attention.get("stale_count", 0)
    if stale:
        lines.append(f"\U0001f9f9 {stale} topics quiet 30+ days — review to close in Claude.ai.")

    return "\n".join(lines).strip()


async def assemble_pulse(week_start: datetime | None = None) -> dict:
    """Build the full Eyal report. Deterministic — NEVER calls the LLM.

    Returns {week_of, text, stale_count, attention, areas} (extra fields aid tests).
    """
    if week_start is None:
        now = datetime.now()
        week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    week_of = week_start.strftime("%Y-%m-%d")

    areas = fetch_areas_with_health()
    attention = classify_attention_topics()
    moved = fetch_moved_this_week(week_start, week_end)
    recap = await recap_counts(week_start, week_end)

    text = format_pulse_text(week_of, recap, areas, attention, moved)
    return {
        "week_of": week_of,
        "text": text,
        "stale_count": attention.get("stale_count", 0),
        "attention": attention,
        "areas": areas,
    }
