"""
Deal intelligence processor (Phase 4).

Detects deal-related signals from meeting transcripts and emails,
generates deal pulse summaries for the morning brief,
and auto-creates interactions from meetings/emails.

Key design decisions:
- Start with 10-15 key contacts, not comprehensive DB
- ONE staleness rule: 7 days no contact -> flag as stale
- Zero-friction: auto-create deal_interactions from meetings/emails
- Deal Pulse capped at 3 items in morning brief (alert fatigue)
- Commitments Due capped at 3 items
- Auto-created deals from signals start as LEAD — require manual promotion
"""

import logging
from datetime import date, datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# ── Deal Signal Detection ─────────────────────────────────────

# Keywords that indicate deal-related conversation
DEAL_SIGNAL_KEYWORDS = [
    "pilot", "poc", "proof of concept", "proposal", "contract",
    "pricing", "budget", "partnership", "mou", "agreement",
    "deliverable", "timeline", "next steps", "follow up",
    "interested", "evaluate", "trial", "demo", "pitch",
    "collaborate", "engagement", "onboard",
]

COMMITMENT_SIGNAL_KEYWORDS = [
    "we'll send", "i'll send", "we will provide", "i will provide",
    "we commit", "we promise", "by next week", "by friday",
    "we'll deliver", "i'll deliver", "we'll share", "i'll share",
    "we'll prepare", "i'll prepare", "we'll have", "i'll have",
    "we'll get back", "i'll get back",
]


def detect_deal_signals(
    transcript_text: str,
    meeting_title: str,
    participants: list[str],
    meeting_date: str,
    meeting_id: str | None = None,
) -> dict:
    """
    Detect deal-related signals from meeting content.

    Returns:
        {
            "has_deal_signals": bool,
            "deal_keywords_found": list[str],
            "has_commitment_signals": bool,
            "commitment_keywords_found": list[str],
            "external_participants": list[str],
            "meeting_id": str | None,
        }
    """
    from config.team import get_team_member_names

    text_lower = transcript_text.lower()

    # Detect deal keywords
    deal_keywords_found = [kw for kw in DEAL_SIGNAL_KEYWORDS if kw in text_lower]

    # Detect commitment keywords
    commitment_keywords_found = [kw for kw in COMMITMENT_SIGNAL_KEYWORDS if kw in text_lower]

    # Identify external participants (not team members)
    team_names = {n.lower() for n in get_team_member_names()}
    external_participants = [
        p for p in participants
        if p.lower() not in team_names and p.lower() not in ("", "unknown")
    ]

    return {
        "has_deal_signals": len(deal_keywords_found) >= 2,
        "deal_keywords_found": deal_keywords_found,
        "has_commitment_signals": len(commitment_keywords_found) >= 1,
        "commitment_keywords_found": commitment_keywords_found,
        "external_participants": external_participants,
        "meeting_title": meeting_title,
        "meeting_date": meeting_date,
        "meeting_id": meeting_id,
    }


def auto_create_deal_interaction(
    deal_id: str,
    meeting_id: str,
    meeting_title: str,
    meeting_date: str,
) -> dict | None:
    """
    Auto-create a deal interaction from a meeting.

    Returns the created interaction or None if the deal doesn't exist.
    """
    deal = supabase_client.get_deal(deal_id)
    if not deal:
        return None

    return supabase_client.create_deal_interaction(
        deal_id=deal_id,
        interaction_type="meeting",
        summary=f"Meeting: {meeting_title}",
        interaction_date=meeting_date,
        source_id=meeting_id,
        source_type="meeting",
    )


# ── Deal Pulse (Morning Brief) ──────────────────────────────

def generate_deal_pulse(max_items: int = 3) -> list[dict]:
    """
    Generate deal pulse items for the morning brief.

    Returns up to max_items, prioritized:
    1. Overdue follow-ups (next_action_date past)
    2. Stale deals (no interaction in 7 days)

    Each item: {"type": "overdue"|"stale", "name": str, "organization": str, "detail": str}
    """
    items = []

    # 1. Overdue follow-ups
    overdue = supabase_client.get_overdue_deal_actions()
    for deal in overdue:
        if len(items) >= max_items:
            break
        days_overdue = (date.today() - date.fromisoformat(deal["next_action_date"])).days
        items.append({
            "type": "overdue",
            "name": deal["name"],
            "organization": deal["organization"],
            "detail": f"{deal.get('next_action', 'Follow up')} — {days_overdue}d overdue",
        })

    # 2. Stale deals (only if room left)
    if len(items) < max_items:
        stale = supabase_client.get_stale_deals(days=7)
        for deal in stale:
            if len(items) >= max_items:
                break
            last = deal.get("last_interaction_date", "unknown")
            if last and last != "unknown":
                days_stale = (date.today() - date.fromisoformat(last)).days
                detail = f"No contact in {days_stale} days"
            else:
                detail = "No recorded interactions"
            items.append({
                "type": "stale",
                "name": deal["name"],
                "organization": deal["organization"],
                "detail": detail,
            })

    return items


def generate_commitments_due(max_items: int = 3) -> list[dict]:
    """
    Generate overdue external commitments for the morning brief.

    Each item: {"organization": str, "commitment": str, "deadline": str, "days_overdue": int}
    """
    overdue = supabase_client.get_overdue_commitments()
    items = []
    for c in overdue[:max_items]:
        days_overdue = (date.today() - date.fromisoformat(c["deadline"])).days
        items.append({
            "organization": c["organization"],
            "commitment": c["commitment"][:80],
            "deadline": c["deadline"],
            "days_overdue": days_overdue,
            "promised_to": c.get("promised_to", ""),
        })
    return items


def format_deal_pulse_for_brief(pulse_items: list[dict]) -> str:
    """Format deal pulse items for Telegram morning brief."""
    if not pulse_items:
        return ""
    lines = []
    for item in pulse_items:
        icon = "!" if item["type"] == "overdue" else "~"
        lines.append(f"  {icon} {item['name']} ({item['organization']}): {item['detail']}")
    return "\n".join(lines)


def format_commitments_for_brief(commitment_items: list[dict]) -> str:
    """Format overdue commitments for Telegram morning brief."""
    if not commitment_items:
        return ""
    lines = []
    for item in commitment_items:
        to_str = f" to {item['promised_to']}" if item.get("promised_to") else ""
        lines.append(f"  ! {item['commitment']}{to_str} ({item['days_overdue']}d overdue)")
    return "\n".join(lines)
