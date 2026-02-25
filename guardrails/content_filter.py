"""
Content filtering for personal and inappropriate content.

This module implements the content filtering rules from Section 8
of the project plan:
- Remove personal discussions (health, family, social banter)
- Remove emotional characterizations
- Remove interpersonal judgments
- Handle personal circumstances that affect timelines appropriately

Usage:
    from guardrails.content_filter import filter_personal_content

    filtered = filter_personal_content(transcript_text)
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Patterns indicating personal content to filter
PERSONAL_CONTENT_PATTERNS = [
    # Health-related
    r"\b(doctor|hospital|sick|illness|medical|surgery|appointment)\b",
    # Family/personal events
    r"\b(wedding|birthday|anniversary|funeral|vacation|holiday)\b",
    # Social banter indicators
    r"\b(how was your weekend|did you see the game|nice weather)\b",
    # Personal compliments
    r"\b(nice (shirt|suit|dress|haircut)|looking good)\b",
]

# Patterns indicating emotional characterization (to flag, not auto-remove)
EMOTIONAL_PATTERNS = [
    r"\b(frustrated|annoyed|angry|upset|concerned|worried|anxious|defensive)\b",
    r"\b(seemed|appeared|looked)\s+(frustrated|annoyed|angry|upset|concerned)\b",
    r"\b(tension between|disagreed sharply|dominated the discussion)\b",
]

# Mapping of emotional language to professional alternatives
EMOTIONAL_REFRAMES = {
    "frustrated": "raised concerns about",
    "annoyed": "expressed dissatisfaction with",
    "angry": "strongly disagreed with",
    "upset": "expressed concern about",
    "concerned": "noted potential issues with",
    "worried": "highlighted risks regarding",
    "anxious": "raised questions about",
    "defensive": "clarified their position on",
    "tension between": "differing views between",
    "disagreed sharply": "had different perspectives on",
    "dominated the discussion": "led the discussion on",
}

# Personal availability reframes
AVAILABILITY_REFRAMES = {
    "wedding": "personal commitment",
    "vacation": "planned leave",
    "holiday": "time off",
    "doctor": "appointment",
    "medical": "personal matter",
    "sick": "unavailable",
}


def filter_personal_content(text: str) -> str:
    """
    Remove personal content from text.

    Identifies and removes sentences containing personal discussions,
    social banter, and inappropriate content while preserving
    business-relevant information.

    Args:
        text: Input text (transcript or summary).

    Returns:
        Filtered text with personal content removed.
    """
    if not text:
        return text

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', text)
    filtered_sentences = []

    for sentence in sentences:
        # Check if sentence contains personal content
        is_personal = False
        for pattern in PERSONAL_CONTENT_PATTERNS:
            if re.search(pattern, sentence, re.IGNORECASE):
                # Check if it has business relevance (availability impact)
                if _has_business_relevance(sentence):
                    # Reframe instead of removing
                    sentence = _reframe_to_business_impact(sentence)
                else:
                    is_personal = True
                break

        if not is_personal:
            filtered_sentences.append(sentence)

    return " ".join(filtered_sentences)


def _has_business_relevance(sentence: str) -> bool:
    """
    Check if a personal mention has business relevance.

    E.g., "Roye's wedding in April" affects availability.
    """
    business_indicators = [
        r"\b(deadline|timeline|availability|schedule|delay|postpone)\b",
        r"\b(won't be|can't|unable to|not available)\b",
        r"\b(next week|next month|in \w+)\b",  # Time references
    ]

    for pattern in business_indicators:
        if re.search(pattern, sentence, re.IGNORECASE):
            return True

    return False


def _reframe_to_business_impact(sentence: str) -> str:
    """
    Reframe a personal reference to focus on business impact.
    """
    result = sentence

    for personal_word, business_word in AVAILABILITY_REFRAMES.items():
        pattern = rf"\b{personal_word}\b"
        result = re.sub(pattern, business_word, result, flags=re.IGNORECASE)

    return result


def identify_personal_sections(text: str) -> list[dict]:
    """
    Identify sections of text that contain personal content.

    Args:
        text: Input text to analyze.

    Returns:
        List of dicts with:
        - start: Start position in text
        - end: End position in text
        - reason: Why it was flagged
        - text: The flagged content
    """
    flagged = []

    for pattern in PERSONAL_CONTENT_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            # Get surrounding context (the sentence)
            sentence_start = text.rfind('.', 0, match.start()) + 1
            if sentence_start < 0:
                sentence_start = 0
            sentence_end = text.find('.', match.end())
            if sentence_end < 0:
                sentence_end = len(text)

            flagged.append({
                "start": match.start(),
                "end": match.end(),
                "reason": f"Personal content: {match.group()}",
                "text": text[sentence_start:sentence_end].strip(),
                "has_business_relevance": _has_business_relevance(
                    text[sentence_start:sentence_end]
                ),
            })

    return flagged


def identify_emotional_language(text: str) -> list[dict]:
    """
    Identify emotional characterizations in text.

    These should be reframed, not removed.

    Args:
        text: Input text to analyze.

    Returns:
        List of dicts with problematic phrases and suggestions.
    """
    flagged = []

    for pattern in EMOTIONAL_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            original = match.group().lower()
            suggestion = EMOTIONAL_REFRAMES.get(
                original,
                "expressed a position on"
            )

            flagged.append({
                "start": match.start(),
                "end": match.end(),
                "original": match.group(),
                "reason": "Emotional characterization - should be reframed",
                "suggestion": suggestion,
            })

    return flagged


def reframe_emotional_language(text: str) -> str:
    """
    Automatically reframe emotional language to professional alternatives.

    Args:
        text: Text containing emotional language.

    Returns:
        Text with emotional language reframed.
    """
    result = text

    for emotional, professional in EMOTIONAL_REFRAMES.items():
        # Pattern to match "X was emotional" or "X seemed emotional"
        patterns = [
            rf"\b(\w+)\s+was\s+{emotional}\b",
            rf"\b(\w+)\s+seemed\s+{emotional}\b",
            rf"\b(\w+)\s+appeared\s+{emotional}\b",
            rf"\b(\w+)\s+looked\s+{emotional}\b",
        ]

        for pattern in patterns:
            result = re.sub(
                pattern,
                rf"\1 {professional}",
                result,
                flags=re.IGNORECASE
            )

    return result


def reframe_personal_circumstance(
    personal_context: str,
    business_impact: str
) -> str:
    """
    Reframe a personal circumstance in terms of business impact only.

    Example:
    - Input: "Roye mentioned his wedding in April"
    - Output: "Roye noted potential availability constraints in April"

    Args:
        personal_context: The personal situation mentioned.
        business_impact: The business-relevant impact.

    Returns:
        Professionally reframed statement.
    """
    if business_impact:
        return business_impact

    # Try to extract and reframe
    result = personal_context

    # Replace personal event with generic "commitment"
    for personal_word, business_word in AVAILABILITY_REFRAMES.items():
        pattern = rf"\b{personal_word}\b"
        if re.search(pattern, result, re.IGNORECASE):
            result = re.sub(pattern, business_word, result, flags=re.IGNORECASE)
            # Add availability framing
            result = result.replace("mentioned", "noted availability constraints due to")
            break

    return result


def validate_summary_tone(summary: str) -> list[dict]:
    """
    Validate that a summary follows the professional tone guidelines.

    Checks for:
    - Emotional characterizations
    - Personal content
    - Inappropriate judgments
    - Missing citations

    Args:
        summary: The summary text to validate.

    Returns:
        List of issues found, each with location and suggestion.
    """
    issues = []

    # Check for emotional language
    emotional_flags = identify_emotional_language(summary)
    for flag in emotional_flags:
        issues.append({
            "type": "emotional_language",
            "location": flag["start"],
            "text": flag["original"],
            "suggestion": f"Reframe to: '{flag.get('suggestion', 'attribute position, not emotion')}'",
            "severity": "warning",
        })

    # Check for personal content
    personal_flags = identify_personal_sections(summary)
    for flag in personal_flags:
        if flag.get("has_business_relevance"):
            issues.append({
                "type": "personal_content_with_impact",
                "location": flag["start"],
                "text": flag["text"],
                "suggestion": "Reframe to focus on business impact only",
                "severity": "info",
            })
        else:
            issues.append({
                "type": "personal_content",
                "location": flag["start"],
                "text": flag["text"],
                "suggestion": "Remove this personal content",
                "severity": "warning",
            })

    # Check for missing citations in key sections
    if "decided" in summary.lower() and "(ref:" not in summary:
        issues.append({
            "type": "missing_citation",
            "location": 0,
            "text": "Decision mentioned",
            "suggestion": "Add timestamp citation (ref: ~MM:SS)",
            "severity": "info",
        })

    return issues


def apply_external_participant_rules(
    text: str,
    external_names: list[str],
    external_roles: dict[str, str] | None = None
) -> str:
    """
    Apply attribution rules for external (non-CropSight) participants.

    Replaces specific name attributions with role/organization references.

    Args:
        text: Text containing attributions.
        external_names: List of external participant names.
        external_roles: Optional mapping of names to roles/orgs.

    Returns:
        Text with external names replaced by role references.
    """
    if not external_names:
        return text

    result = text

    for name in external_names:
        if not name:
            continue

        # Get role if available, otherwise use generic
        role = "the external contact"
        if external_roles and name in external_roles:
            role = external_roles[name]

        # Replace patterns like "Name said", "Name mentioned", "Name's view"
        patterns = [
            (rf"\b{re.escape(name)}\s+said\b", f"{role} said"),
            (rf"\b{re.escape(name)}\s+mentioned\b", f"{role} mentioned"),
            (rf"\b{re.escape(name)}\s+noted\b", f"{role} noted"),
            (rf"\b{re.escape(name)}\s+suggested\b", f"{role} suggested"),
            (rf"\b{re.escape(name)}'s\s+", f"{role}'s "),
            (rf"\b{re.escape(name)}\b", role),
        ]

        for pattern, replacement in patterns:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    return result


def clean_summary_for_distribution(
    summary: str,
    external_participants: list[str] | None = None,
    external_roles: dict[str, str] | None = None
) -> str:
    """
    Clean a summary for distribution by applying all content filters.

    This is the main entry point for preparing summaries.

    Args:
        summary: Raw summary text.
        external_participants: List of non-CropSight participant names.
        external_roles: Mapping of external names to roles/orgs.

    Returns:
        Cleaned summary ready for distribution.
    """
    result = summary

    # Step 1: Filter personal content
    result = filter_personal_content(result)

    # Step 2: Reframe emotional language
    result = reframe_emotional_language(result)

    # Step 3: Apply external participant rules
    if external_participants:
        result = apply_external_participant_rules(
            result,
            external_participants,
            external_roles
        )

    # Step 4: Validate and log any remaining issues
    issues = validate_summary_tone(result)
    if issues:
        logger.warning(f"Summary has {len(issues)} tone issues after filtering")
        for issue in issues:
            if issue.get("severity") == "warning":
                logger.warning(f"  - {issue['type']}: {issue['text'][:50]}...")

    return result
