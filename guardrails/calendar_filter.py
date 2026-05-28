"""
Calendar filtering for CropSight vs personal meetings.

This module implements the multi-layered filter chain from Section 6
of the project plan to determine if a meeting is CropSight-related.

Filter Chain (in order of evaluation):
1. Blocklist check - Personal keywords = NOT CropSight (hard stop)
2. Calendar color - Purple = CropSight
3. Participants - 2+ team members = CropSight
4. Title prefix - "CropSight", "CS:", etc. = CropSight
5. Uncertain - Ask Eyal

Usage:
    from guardrails.calendar_filter import is_cropsight_meeting

    result = is_cropsight_meeting(calendar_event)
    # Returns: True, False, or None (uncertain - ask Eyal)
"""

from typing import Any

import logging

from config.settings import settings
from config.team import (
    CROPSIGHT_TEAM_EMAILS,
    CROPSIGHT_PREFIXES,
    BLOCKED_KEYWORDS,
    is_business_identity,
    is_known_stakeholder_domain,
)

logger = logging.getLogger(__name__)


def is_cropsight_meeting(event: dict) -> bool | None:
    """
    Determine if a calendar event is a CropSight meeting.

    Dispatches between the legacy OR-chain and the strict chain based on
    ``STRICT_CALENDAR_FILTER``. While ``INPUT_HYGIENE_SHADOW_MODE`` is on, the
    strict decision is computed and the delta logged, but the LEGACY decision is
    returned so live behavior is unchanged during the observation window.

    Args:
        event: Calendar event dict with keys:
            - title (str): Event summary/title
            - attendees (list[dict]): List of attendees with 'email' key
            - organizer (dict): {'email': ...}
            - color_id (str | None): Google Calendar color ID

    Returns:
        - True: This is a CropSight meeting
        - False: This is a personal meeting (do not process)
        - None: Uncertain - ask Eyal before processing
    """
    legacy = _is_cropsight_meeting_legacy(event)

    if not settings.STRICT_CALENDAR_FILTER:
        return legacy

    strict_decision, strict_reason = _classify_strict(event)

    if settings.INPUT_HYGIENE_SHADOW_MODE:
        if strict_decision != legacy:
            _log_calendar_shadow(event, legacy, strict_decision, strict_reason)
        return legacy

    return strict_decision


def should_include_meeting(event: dict) -> bool:
    """Consumer helper: should this event appear in business outputs?

    Honors ``STRICT_UNCERTAIN_EXCLUSION`` — when enforcing (flag on and NOT in
    shadow), uncertain (None) events are EXCLUDED. Otherwise legacy behavior:
    include unless explicitly classified personal (False). Coupling to the
    shadow flag keeps the observation window fully non-disruptive.
    """
    result = is_cropsight_meeting(event)
    if settings.STRICT_UNCERTAIN_EXCLUSION and not settings.INPUT_HYGIENE_SHADOW_MODE:
        return result is True
    return result is not False


def _is_cropsight_meeting_legacy(event: dict) -> bool | None:
    """Legacy OR-chain (retained behind STRICT_CALENDAR_FILTER for rollback).

    Purple color OR 2+ team members (by personal-gmail email) OR title prefix.
    The 2+ branch is the personal-event leak source the strict chain removes.
    """
    title_lower = (event.get("title", "") or "").lower().strip()
    attendees = event.get("attendees", []) or []
    color_id = event.get("color_id")

    if _matches_blocklist(title_lower):
        return False
    if _is_cropsight_color(color_id):
        return True
    if _has_sufficient_team_members(attendees):
        return True
    if _has_cropsight_prefix(title_lower):
        return True
    return None


def _classify_strict(event: dict) -> tuple[bool | None, str]:
    """Strict chain: the CEO's explicit signal is authoritative; exclude when uncertain.

    Order: blocklist -> purple color -> business-domain participant ->
    known-stakeholder-domain participant -> title prefix -> uncertain.
    The personal-gmail "2+ team members" branch is intentionally absent.

    Returns:
        (decision, reason) where reason names the branch that fired.
    """
    title_lower = (event.get("title", "") or "").lower().strip()
    color_id = event.get("color_id")

    if _matches_blocklist(title_lower):
        return False, "blocklist"
    if _is_cropsight_color(color_id):
        return True, "purple_color"
    if _has_business_participant(event):
        return True, "business_identity"
    if _has_stakeholder_participant(event):
        return True, "stakeholder_domain"
    if _has_cropsight_prefix(title_lower):
        return True, "title_prefix"
    return None, "uncertain"


def _is_cropsight_meeting_strict(event: dict) -> bool | None:
    """The strict decision alone (used by tests / direct callers)."""
    return _classify_strict(event)[0]


def _attendee_email(a) -> str:
    """Extract email from an attendee that may be a dict OR a plain string.

    Google Calendar normally returns attendees as `[{email, ...}, ...]`, but
    some events come back as `["x@y.com", ...]` — calling `.get(...)` on the
    string crashed prep_ping_scheduler (May 2026). Defensive across the file.
    """
    if isinstance(a, dict):
        return (a.get("email") or "").strip()
    if isinstance(a, str):
        return a.strip()
    return ""


def _organizer_email(event: dict) -> str:
    """Extract organizer email regardless of dict-or-string shape."""
    org = event.get("organizer")
    if isinstance(org, dict):
        return (org.get("email") or "").strip()
    if isinstance(org, str):
        return org.strip()
    return ""


def _participant_emails(event: dict) -> list[str]:
    """Collect organizer + attendee emails from an event (bare addresses)."""
    emails: list[str] = []
    org = _organizer_email(event)
    if org:
        emails.append(org)
    for attendee in event.get("attendees", []) or []:
        email = _attendee_email(attendee)
        if email:
            emails.append(email)
    return emails


def _has_business_participant(event: dict) -> bool:
    """True if any participant is on a CropSight business domain (not personal gmail)."""
    return any(is_business_identity(email) for email in _participant_emails(event))


def _has_stakeholder_participant(event: dict) -> bool:
    """True if any participant is on a known-stakeholder domain (entity registry).

    Catches the ad-hoc call with a known Moldova client / Italian consortium
    contact on their own domain that wasn't colored purple.
    """
    return any(is_known_stakeholder_domain(email) for email in _participant_emails(event))


def _log_calendar_shadow(
    event: dict, legacy: bool | None, strict_decision: bool | None, strict_reason: str
) -> None:
    """Log a human-scannable calendar shadow delta to audit_log (never raises)."""
    try:
        from services.supabase_client import supabase_client

        organizer = _organizer_email(event)
        attendee_emails = [_attendee_email(a) for a in (event.get("attendees") or [])]
        supabase_client.log_action(
            "input_hygiene_shadow",
            {
                "surface": "calendar",
                "title": event.get("title", ""),
                "organizer": organizer,
                "attendees": attendee_emails,
                "old_decision": legacy,
                "new_decision": strict_decision,
                "branch": strict_reason,
            },
        )
    except Exception:  # monitoring must never break the filter
        logger.debug("calendar shadow log failed", exc_info=True)


def _matches_blocklist(title_lower: str) -> bool:
    """
    Check if the title contains blocked keywords.

    Args:
        title_lower: Lowercase meeting title.

    Returns:
        True if any blocked keyword is found.
    """
    return any(keyword in title_lower for keyword in BLOCKED_KEYWORDS)


def _is_cropsight_color(color_id: str | None) -> bool:
    """
    Check if the calendar color indicates CropSight.

    Args:
        color_id: Google Calendar color ID.

    Returns:
        True if the color is the designated CropSight color (purple).
    """
    if not color_id:
        return False
    return color_id == settings.CROPSIGHT_CALENDAR_COLOR_ID


def _has_sufficient_team_members(attendees: list[dict]) -> bool:
    """
    Check if 2+ CropSight team members are attending.

    Args:
        attendees: List of attendee dicts with 'email' key.

    Returns:
        True if at least 2 team members are present.
    """
    team_lower = [e.lower() for e in CROPSIGHT_TEAM_EMAILS if e]
    team_attendees = [
        a for a in attendees
        if _attendee_email(a).lower() in team_lower
    ]
    return len(team_attendees) >= 2


def _has_cropsight_prefix(title_lower: str) -> bool:
    """
    Check if the title starts with a CropSight prefix.

    Args:
        title_lower: Lowercase meeting title.

    Returns:
        True if title starts with a CropSight prefix.
    """
    return any(title_lower.startswith(prefix) for prefix in CROPSIGHT_PREFIXES)


async def ask_eyal_about_meeting(
    event: dict,
    messenger: Any,
    timeout_seconds: int = 300
) -> bool | None:
    """
    Ask Eyal via Telegram if an uncertain meeting is CropSight-related.

    Args:
        event: The calendar event in question.
        messenger: Object exposing send_to_eyal (the comms spine, or any messenger).
        timeout_seconds: How long to wait for response (default 5 minutes).

    Returns:
        True if Eyal confirms it's CropSight, False if not, None if no response.
    """
    import asyncio
    import logging
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    logger = logging.getLogger(__name__)

    # Format the question
    question = format_uncertain_meeting_question(event)
    event_id = event.get("id", "unknown")

    # Create inline keyboard for easy response
    keyboard = [
        [
            InlineKeyboardButton(
                "Yes, CropSight",
                callback_data=f"meeting_yes:{event_id}"
            ),
            InlineKeyboardButton(
                "No, Personal",
                callback_data=f"meeting_no:{event_id}"
            ),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send to Eyal (HTML format for consistency)
    message = f"<b>Meeting Classification Needed</b>\n\n{question}"
    success = await messenger.send_to_eyal(
        message, reply_markup=reply_markup, parse_mode="HTML"
    )

    if not success:
        logger.error("Failed to send meeting question to Eyal")
        return None

    # Store pending question for callback handling
    # The actual response will be handled by the callback handler in telegram_bot
    # For now, we'll use a simple polling approach with the stored response

    # In a production system, you'd use a proper async waiting mechanism
    # For simplicity, return None and let the callback handler update the decision
    logger.info(f"Asked Eyal about meeting: {event.get('title', 'Unknown')}")
    return None  # Async - response handled via callback


def remember_meeting_classification(title: str, is_cropsight: bool) -> None:
    """
    Remember a meeting classification for future similar titles.

    This helps avoid asking Eyal about similar meetings repeatedly.
    Stores in Supabase for persistence across restarts.

    Args:
        title: The meeting title.
        is_cropsight: Whether it was classified as CropSight.
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        from services.supabase_client import supabase_client
        supabase_client.remember_classification(title, is_cropsight)
    except Exception as e:
        logger.warning(f"Failed to remember classification: {e}")


def check_remembered_classification(title: str) -> bool | None:
    """
    Check if we've seen a similar meeting title before.

    Two-pass approach:
    1. Exact match (case-insensitive) — fast and reliable
    2. Fuzzy match (word overlap) — catches renamed/similar meetings

    Args:
        title: The meeting title to check.

    Returns:
        True/False if we have a remembered classification, None otherwise.
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        from services.supabase_client import supabase_client

        # Pass 1: Exact match (case-insensitive via title_lower column)
        exact = supabase_client.get_classification_by_title(title)
        if exact is not None:
            logger.info(
                f"Exact classification match for '{title}': "
                f"{'CropSight' if exact['is_cropsight'] else 'Personal'}"
            )
            return exact["is_cropsight"]

        # Pass 2: Fuzzy match against all past classifications
        all_classifications = supabase_client.get_all_classifications()
        if all_classifications:
            fuzzy_result = _find_fuzzy_match(title, all_classifications)
            if fuzzy_result is not None:
                logger.info(
                    f"Fuzzy classification match for '{title}': "
                    f"{'CropSight' if fuzzy_result else 'Personal'}"
                )
                return fuzzy_result

    except Exception as e:
        logger.warning(f"Failed to check remembered classification: {e}")

    return None


# Stop words for fuzzy matching — common meeting words that don't help distinguish
_STOP_WORDS = frozenset({
    "meeting", "call", "sync", "weekly", "daily", "standup", "stand-up",
    "biweekly", "monthly", "review", "update", "check-in", "checkin",
    "chat", "discussion", "session", "catchup", "catch-up", "followup",
    "follow-up", "with", "and", "the", "for", "about", "on", "re",
})


def _extract_significant_words(title: str) -> set[str]:
    """
    Extract significant words from a meeting title.

    Strips punctuation, lowercases, removes stop words and very short words.

    Args:
        title: The meeting title.

    Returns:
        Set of significant lowercase words.
    """
    import re
    # Lowercase and strip punctuation (keep alphanumeric + spaces)
    cleaned = re.sub(r"[^\w\s]", " ", title.lower())
    words = cleaned.split()
    # Remove stop words and words with 1-2 characters
    return {w for w in words if w not in _STOP_WORDS and len(w) > 2}


def _find_fuzzy_match(
    title: str,
    classifications: list[dict],
    threshold: float = 0.6,
) -> bool | None:
    """
    Find a fuzzy match for a title against past classifications.

    Uses word-overlap scoring: if 60%+ of the new title's significant
    words match a past title's significant words, return that classification.

    Args:
        title: The new meeting title.
        classifications: List of past classification records.
        threshold: Minimum overlap ratio to consider a match (default 0.6).

    Returns:
        True/False if a match is found above threshold, None otherwise.
    """
    new_words = _extract_significant_words(title)
    if not new_words:
        return None

    best_score = 0.0
    best_match = None

    for record in classifications:
        past_words = _extract_significant_words(record.get("title", ""))
        if not past_words:
            continue

        # Overlap = intersection / new_words count
        overlap = len(new_words & past_words) / len(new_words)
        if overlap > best_score:
            best_score = overlap
            best_match = record

    if best_score >= threshold and best_match is not None:
        return best_match["is_cropsight"]

    return None


def format_uncertain_meeting_question(event: dict) -> str:
    """
    Format the question to ask Eyal about an uncertain meeting.

    Args:
        event: The calendar event in question.

    Returns:
        Formatted question string.
    """
    title = event.get("title", "Unknown")
    start_time = event.get("start", "Unknown time")
    attendees = event.get("attendees", [])

    attendee_names = [
        (a.get("displayName") or a.get("email", "")) if isinstance(a, dict) else (a if isinstance(a, str) else "")
        for a in attendees[:3]
    ]
    attendee_str = ", ".join(attendee_names)
    if len(attendees) > 3:
        attendee_str += f" and {len(attendees) - 3} others"

    return (
        f"I see a meeting '{title}' at {start_time} "
        f"with {attendee_str}. Is this CropSight-related?"
    )
