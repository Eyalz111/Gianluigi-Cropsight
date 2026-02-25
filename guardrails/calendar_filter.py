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

from config.settings import settings
from config.team import (
    CROPSIGHT_TEAM_EMAILS,
    CROPSIGHT_PREFIXES,
    BLOCKED_KEYWORDS,
)


def is_cropsight_meeting(event: dict) -> bool | None:
    """
    Determine if a calendar event is a CropSight meeting.

    Applies the filter chain from Section 6 of the project plan.

    Args:
        event: Calendar event dict with keys:
            - title (str): Event summary/title
            - attendees (list[dict]): List of attendees with 'email' key
            - color_id (str | None): Google Calendar color ID

    Returns:
        - True: This is a CropSight meeting
        - False: This is a personal meeting (do not process)
        - None: Uncertain - ask Eyal before processing
    """
    title = event.get("title", "") or ""
    title_lower = title.lower().strip()
    attendees = event.get("attendees", []) or []
    color_id = event.get("color_id")

    # Layer 1 (BLOCKLIST) - Check first as a hard stop
    if _matches_blocklist(title_lower):
        return False

    # Layer 2 (COLOR) - Purple calendar color
    if _is_cropsight_color(color_id):
        return True

    # Layer 3 (PARTICIPANTS) - 2+ CropSight team members
    if _has_sufficient_team_members(attendees):
        return True

    # Layer 4 (TITLE PREFIX) - CropSight prefix patterns
    if _has_cropsight_prefix(title_lower):
        return True

    # UNCERTAIN - none of the positive signals matched
    return None


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
    team_attendees = [
        a for a in attendees
        if a.get("email", "").lower() in [e.lower() for e in CROPSIGHT_TEAM_EMAILS if e]
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
    telegram_bot: Any,
    timeout_seconds: int = 300
) -> bool | None:
    """
    Ask Eyal via Telegram if an uncertain meeting is CropSight-related.

    Args:
        event: The calendar event in question.
        telegram_bot: TelegramBot instance for sending the message.
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
    success = await telegram_bot.send_to_eyal(
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

    Args:
        title: The meeting title.
        is_cropsight: Whether it was classified as CropSight.
    """
    # TODO: Store in database for future reference
    # This could be a simple lookup table or ML model in v0.2+
    pass


def check_remembered_classification(title: str) -> bool | None:
    """
    Check if we've seen a similar meeting title before.

    Args:
        title: The meeting title to check.

    Returns:
        True/False if we have a remembered classification, None otherwise.
    """
    # TODO: Query database for similar titles
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

    attendee_names = [a.get("displayName") or a.get("email", "") for a in attendees[:3]]
    attendee_str = ", ".join(attendee_names)
    if len(attendees) > 3:
        attendee_str += f" and {len(attendees) - 3} others"

    return (
        f"I see a meeting '{title}' at {start_time} "
        f"with {attendee_str}. Is this CropSight-related?"
    )
