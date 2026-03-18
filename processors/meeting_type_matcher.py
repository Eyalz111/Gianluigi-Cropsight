"""
Meeting type matcher — classifies calendar events to meeting prep templates.

Uses a scoring-based approach with multiple signals:
- Title fuzzy match against template patterns
- Participant overlap with expected attendees
- Day-of-week match
- Previously matched (persistent memory via calendar_classifications)

Usage:
    from processors.meeting_type_matcher import classify_meeting_type, remember_meeting_type

    meeting_type, confidence, signals = classify_meeting_type(event)
"""

import logging
from datetime import datetime

from config.meeting_prep_templates import MEETING_PREP_TEMPLATES, get_template
from guardrails.calendar_filter import _extract_significant_words
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


def score_meeting_type(event: dict) -> list[tuple[str, int, list[str]]]:
    """
    Score an event against all meeting type templates.

    Args:
        event: Calendar event dict with 'title', 'attendees', 'start'.

    Returns:
        List of (meeting_type, score, signals) sorted by score descending.
    """
    title = event.get("title", "")
    attendees = event.get("attendees", [])
    start = event.get("start", "")

    # Normalize attendee names for matching
    attendee_names_lower = set()
    for a in attendees:
        name = (a.get("displayName") or a.get("email", "").split("@")[0]).lower()
        # Also add first name only for partial matching
        first_name = name.split()[0] if name else ""
        attendee_names_lower.add(name)
        if first_name:
            attendee_names_lower.add(first_name)

    # Get day of week from start time
    event_day = _parse_day_of_week(start)

    # Check previously matched type from calendar_classifications
    previously_matched = _get_previously_matched_type(title)

    results = []
    for meeting_type, template in MEETING_PREP_TEMPLATES.items():
        score = 0
        signals = []

        # Signal 1: Title fuzzy match (+3)
        if _title_matches_template(title, template.get("match_titles", [])):
            score += 3
            signals.append("title_match")

        # Signal 2: Participant match
        expected = set(template.get("expected_participants", []))
        if expected:
            matched_participants = expected & attendee_names_lower
            if matched_participants == expected:
                # Exact match — all expected participants present
                score += 2
                signals.append("exact_participants")
            elif matched_participants:
                # Partial — at least some expected are present
                score += 1
                signals.append("partial_participants")

        # Signal 3: Day-of-week match (+1)
        expected_day = template.get("expected_day")
        if expected_day and event_day and event_day.lower() == expected_day.lower():
            score += 1
            signals.append("day_match")

        # Signal 4: Previously matched in DB (+2)
        if previously_matched and previously_matched == meeting_type:
            score += 2
            signals.append("previously_matched")

        results.append((meeting_type, score, signals))

    # Sort by score descending
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def classify_meeting_type(event: dict) -> tuple[str, str, list[str]]:
    """
    Classify a calendar event to a meeting type.

    Args:
        event: Calendar event dict.

    Returns:
        (meeting_type, confidence, signals) where:
        - confidence is "auto" (score >= 3), "ask" (score == 2), or "none" (score < 2)
    """
    scores = score_meeting_type(event)

    if not scores:
        return ("generic", "none", [])

    best_type, best_score, best_signals = scores[0]

    if best_score >= 3:
        return (best_type, "auto", best_signals)
    elif best_score == 2:
        return (best_type, "ask", best_signals)
    else:
        # Low score — try LLM classification as fallback (handles Hebrew, etc.)
        llm_type = _classify_with_llm(event)
        if llm_type and llm_type != "generic":
            return (llm_type, "ask", best_signals + ["llm_classification"])
        return ("generic", "none", best_signals)


def remember_meeting_type(title: str, meeting_type: str) -> None:
    """
    Persist a meeting type classification for future matching.

    Args:
        title: Calendar event title.
        meeting_type: Template key to associate.
    """
    try:
        supabase_client.update_classification_meeting_type(title, meeting_type)
        logger.info(f"Remembered meeting type: '{title}' → {meeting_type}")
    except Exception as e:
        logger.warning(f"Failed to remember meeting type: {e}")


def _classify_with_llm(event: dict) -> str | None:
    """
    Use Haiku to classify a meeting title that couldn't be matched by word overlap.
    Handles Hebrew, abbreviations, and other non-English titles.
    Cost: ~$0.0005 per call. Only triggered when fuzzy matching fails.
    """
    title = event.get("title", "")
    attendees = event.get("attendees", [])
    attendee_names = [
        a.get("displayName") or a.get("email", "").split("@")[0]
        for a in attendees
    ]

    from config.meeting_prep_templates import MEETING_PREP_TEMPLATES
    template_descriptions = []
    for key, tmpl in MEETING_PREP_TEMPLATES.items():
        if key == "generic":
            continue
        template_descriptions.append(
            f"- {key}: {tmpl['display_name']} (participants: {', '.join(tmpl.get('expected_participants', []))})"
        )

    prompt = f"""Classify this calendar event into one of these meeting types, or "generic" if it doesn't match any.

Meeting types:
{chr(10).join(template_descriptions)}

Event title: {title}
Participants: {', '.join(attendee_names)}

Reply with ONLY the meeting type key (e.g. "founders_technical" or "generic"). Nothing else."""

    try:
        from core.llm import call_llm
        from config.settings import settings
        response_text, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,  # Haiku
            max_tokens=30,
            call_site="meeting_type_llm_classification",
        )
        result = response_text.strip().lower().strip('"').strip("'")
        # Validate it's a real template key
        if result in MEETING_PREP_TEMPLATES:
            logger.info(f"LLM classified '{title}' as {result}")
            return result
        return None
    except Exception as e:
        logger.warning(f"LLM meeting type classification failed: {e}")
        return None


def _title_matches_template(title: str, match_titles: list[str]) -> bool:
    """
    Check if event title fuzzy-matches any template title pattern.

    Uses significant word overlap (60%+ threshold) from calendar_filter.
    """
    if not match_titles:
        return False

    title_words = _extract_significant_words(title)
    if not title_words:
        return False

    for pattern in match_titles:
        pattern_words = _extract_significant_words(pattern)
        if not pattern_words:
            continue

        # Check overlap in both directions — if either has 60%+ overlap, match
        overlap = title_words & pattern_words
        if not overlap:
            continue

        title_ratio = len(overlap) / len(title_words) if title_words else 0
        pattern_ratio = len(overlap) / len(pattern_words) if pattern_words else 0

        if title_ratio >= 0.6 or pattern_ratio >= 0.6:
            return True

    return False


def _parse_day_of_week(start: str) -> str | None:
    """Parse day of week from an ISO datetime string."""
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        return dt.strftime("%A")
    except (ValueError, TypeError):
        return None


def _get_previously_matched_type(title: str) -> str | None:
    """Look up a previously stored meeting_type for this title."""
    try:
        record = supabase_client.get_classification_by_title(title)
        if record:
            return record.get("meeting_type")
    except Exception:
        pass
    return None
