"""
Meeting sensitivity classification.

This module classifies meetings as 'normal' or 'sensitive' based on
keywords in the title, content analysis, and optional LLM classification.
Sensitive meetings get restricted distribution (Eyal-only).

Sensitive categories (from Section 7):
- Legal: lawyer, legal, fischer, fbc, zohar
- Investor: investor, investment, funding, vc
- Confidential: nda, confidential, founders agreement
- HR/Equity: personal, hr, compensation, equity

Usage:
    from guardrails.sensitivity_classifier import classify_sensitivity

    sensitivity = classify_sensitivity(calendar_event)
    # Returns: 'normal' or 'sensitive'
"""

import logging

from config.team import SENSITIVE_KEYWORDS

logger = logging.getLogger(__name__)


def classify_sensitivity(event: dict) -> str:
    """
    Classify a meeting's sensitivity level.

    Args:
        event: Calendar event dict with 'title' key.

    Returns:
        'normal' for team-wide distribution
        'sensitive' for Eyal-only distribution
    """
    title = event.get("title", "") or ""
    title_lower = title.lower()

    if _contains_sensitive_keyword(title_lower):
        return "sensitive"

    return "normal"


def classify_sensitivity_from_content(content: str) -> str:
    """
    Classify sensitivity based on transcript/document content.

    This is a secondary check applied to content after initial
    processing, in case sensitive topics arise during the meeting
    but weren't in the title.

    Args:
        content: Text content to analyze.

    Returns:
        'normal' or 'sensitive'
    """
    content_lower = content.lower()

    # Check for explicit mentions of sensitive topics in content
    sensitive_content_patterns = [
        "founders agreement",
        "equity split",
        "salary discussion",
        "compensation",
        "investor meeting",
        "term sheet",
        "valuation",
        "legal review",
    ]

    for pattern in sensitive_content_patterns:
        if pattern in content_lower:
            return "sensitive"

    return "normal"


def _contains_sensitive_keyword(title_lower: str) -> bool:
    """
    Check if the title contains any sensitive keywords.

    Args:
        title_lower: Lowercase meeting title.

    Returns:
        True if any sensitive keyword is found.
    """
    return any(keyword in title_lower for keyword in SENSITIVE_KEYWORDS)


def get_sensitivity_reason(event: dict) -> str | None:
    """
    Get the reason why a meeting was classified as sensitive.

    Useful for audit logging and explaining to Eyal why distribution
    was restricted.

    Args:
        event: Calendar event dict with 'title' key.

    Returns:
        Description of why it's sensitive, or None if normal.
    """
    title = event.get("title", "") or ""
    title_lower = title.lower()

    for keyword in SENSITIVE_KEYWORDS:
        if keyword in title_lower:
            return f"Contains sensitive keyword: '{keyword}'"

    return None


def get_distribution_list(sensitivity: str, team_emails: list[str]) -> list[str]:
    """
    Get the email distribution list based on sensitivity and environment.

    In development mode (ENVIRONMENT != 'production'), all emails go to
    Eyal only — no team members are contacted.

    Args:
        sensitivity: 'normal' or 'sensitive'
        team_emails: Full list of team member emails.

    Returns:
        List of emails to distribute to.
    """
    from config.settings import settings

    # Development mode: always Eyal-only
    if settings.ENVIRONMENT != "production":
        return [settings.EYAL_EMAIL] if settings.EYAL_EMAIL else []

    if sensitivity == "sensitive":
        # Eyal only
        return [settings.EYAL_EMAIL] if settings.EYAL_EMAIL else []
    else:
        # Full team
        return [e for e in team_emails if e]


def classify_attendees_sensitivity(attendees: list[dict]) -> str:
    """
    Check if any attendees indicate a sensitive meeting.

    External lawyers, investors, or NDA-covered contacts
    trigger sensitive classification.

    Args:
        attendees: List of attendee dicts with 'email' and optional 'displayName'.

    Returns:
        'normal' or 'sensitive'
    """
    from config.team import CROPSIGHT_TEAM_EMAILS

    # Sensitive external domains
    sensitive_domains = [
        "law", "legal", "advocate", "attorney",
        "vc", "capital", "ventures", "invest",
    ]

    for attendee in attendees:
        email = attendee.get("email", "").lower()

        # Skip team members
        if email in [e.lower() for e in CROPSIGHT_TEAM_EMAILS if e]:
            continue

        # Check domain
        domain = email.split("@")[-1] if "@" in email else ""
        for sensitive_domain in sensitive_domains:
            if sensitive_domain in domain:
                return "sensitive"

        # Check display name
        display_name = attendee.get("displayName", "").lower()
        for keyword in ["lawyer", "attorney", "investor", "partner"]:
            if keyword in display_name:
                return "sensitive"

    return "normal"


def get_combined_sensitivity(
    event: dict,
    content: str | None = None
) -> tuple[str, list[str]]:
    """
    Get combined sensitivity classification from all sources.

    Checks:
    1. Event title
    2. Event attendees
    3. Content (if provided)

    Args:
        event: Calendar event dict.
        content: Optional transcript/document content.

    Returns:
        Tuple of (sensitivity, list of reasons).
    """
    reasons = []

    # Check title
    title_reason = get_sensitivity_reason(event)
    if title_reason:
        reasons.append(f"Title: {title_reason}")

    # Check attendees
    attendees = event.get("attendees", [])
    if attendees:
        attendee_sensitivity = classify_attendees_sensitivity(attendees)
        if attendee_sensitivity == "sensitive":
            reasons.append("Attendees include potentially sensitive contacts")

    # Check content
    if content:
        content_sensitivity = classify_sensitivity_from_content(content)
        if content_sensitivity == "sensitive":
            reasons.append("Content contains sensitive topics")

    sensitivity = "sensitive" if reasons else "normal"
    return sensitivity, reasons


def classify_sensitivity_llm(content: str) -> str:
    """
    Use Haiku for nuanced sensitivity classification beyond keywords.

    Runs as a fallback when keyword matching returns 'normal' but content
    is substantial (>500 chars). Catches things like "give him a bigger share"
    or "competitor X's pricing" that keywords miss.

    Args:
        content: Text content to classify.

    Returns:
        'normal' or 'sensitive'
    """
    if len(content) < 500:
        return "normal"

    from core.llm import call_llm
    from config.settings import settings

    # Use first 3000 chars to keep cost low
    excerpt = content[:3000]

    prompt = (
        "Classify this meeting content as 'sensitive' or 'normal'.\n\n"
        "SENSITIVE means it discusses ANY of:\n"
        "- Investor terms, fundraising, valuations, term sheets\n"
        "- Equity, vesting, compensation, salary\n"
        "- Legal disputes, contracts, NDAs, compliance\n"
        "- Competitive intelligence, competitor pricing/strategy\n"
        "- HR issues, hiring negotiations, personnel conflicts\n"
        "- Confidential partnerships not yet announced\n\n"
        "NORMAL means standard operational discussion.\n\n"
        f"CONTENT:\n{excerpt}\n\n"
        "Respond with exactly one word: sensitive or normal"
    )

    try:
        response, _ = call_llm(
            prompt=prompt,
            model=settings.model_simple,  # Haiku
            max_tokens=10,
            call_site="sensitivity_llm",
        )
        result = response.strip().lower()
        if result in ("sensitive", "normal"):
            return result
        return "normal"
    except Exception as e:
        logger.warning(f"LLM sensitivity classification failed: {e}")
        return "normal"


def propagate_meeting_sensitivity(meeting_id: str, sensitivity: str) -> dict:
    """
    Propagate meeting-level sensitivity to all child items.

    Sets the sensitivity field on tasks, decisions, and open questions
    belonging to this meeting. Called after extraction and on manual toggle.

    Args:
        meeting_id: UUID of the meeting.
        sensitivity: 'normal' or 'sensitive'.

    Returns:
        Dict with counts of updated items.
    """
    from services.supabase_client import supabase_client

    counts = {"tasks": 0, "decisions": 0, "open_questions": 0}

    try:
        result = (
            supabase_client.client.table("tasks")
            .update({"sensitivity": sensitivity})
            .eq("meeting_id", meeting_id)
            .execute()
        )
        counts["tasks"] = len(result.data) if result.data else 0
    except Exception as e:
        logger.error(f"Failed to propagate sensitivity to tasks: {e}")

    try:
        result = (
            supabase_client.client.table("decisions")
            .update({"sensitivity": sensitivity})
            .eq("meeting_id", meeting_id)
            .execute()
        )
        counts["decisions"] = len(result.data) if result.data else 0
    except Exception as e:
        logger.error(f"Failed to propagate sensitivity to decisions: {e}")

    try:
        result = (
            supabase_client.client.table("open_questions")
            .update({"sensitivity": sensitivity})
            .eq("meeting_id", meeting_id)
            .execute()
        )
        counts["open_questions"] = len(result.data) if result.data else 0
    except Exception as e:
        logger.error(f"Failed to propagate sensitivity to open_questions: {e}")

    total = sum(counts.values())
    if total > 0:
        logger.info(
            f"Propagated sensitivity={sensitivity} to {total} items for meeting {meeting_id}: {counts}"
        )
    return counts
