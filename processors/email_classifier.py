"""
Email classification and intelligence extraction.

Two-step pipeline:
1. Haiku classifies email relevance (cheap, fast)
2. Sonnet extracts structured items from relevant emails

Used by both the constant layer (email_watcher) and daily scan
(personal_email_scanner). Results queue for the morning brief.
"""

import json
import logging
from functools import lru_cache

from config.settings import settings
from core.llm import call_llm

logger = logging.getLogger(__name__)


# =========================================================================
# Classification (Haiku — ~$0.001/email)
# =========================================================================

async def classify_email(
    sender: str,
    subject: str,
    body_preview: str,
    filter_keywords: list[str] | None = None,
) -> str:
    """
    Classify an email as relevant, borderline, or false_positive.

    Uses Haiku for speed and cost. The filter_keywords list provides
    live context from the entity registry, Gantt, and active tasks.

    Args:
        sender: Sender email or name.
        subject: Email subject line.
        body_preview: First ~500 chars of the body.
        filter_keywords: Live keyword list from build_filter_keywords().

    Returns:
        Classification: 'relevant', 'borderline', or 'false_positive'.
    """
    keywords_str = ", ".join(filter_keywords[:50]) if filter_keywords else "cropsight, moldova, gagauzia"

    system = (
        "You are a classification assistant for CropSight, an Israeli AgTech startup. "
        "Classify emails by their relevance to CropSight business operations.\n"
        "Team members: Eyal Zror (CEO), Roye Tadmor (CTO), Paolo Vailetti (BD), Prof. Yoram Weiss (Advisor).\n"
        f"Relevant keywords: {keywords_str}\n\n"
        "Respond with EXACTLY one word: relevant, borderline, or false_positive.\n"
        "- relevant: clearly about CropSight business, team, projects, or stakeholders\n"
        "- borderline: might be related but unclear\n"
        "- false_positive: personal, spam, newsletters, or unrelated"
    )

    prompt = f"Sender: {sender}\nSubject: {subject}\nPreview: {body_preview[:500]}"

    try:
        text, _usage = call_llm(
            prompt=prompt,
            model=settings.model_simple,
            max_tokens=10,
            system=system,
            call_site="email_classify",
        )
        result = text.strip().lower()
        if result in ("relevant", "borderline", "false_positive"):
            return result
        # Default to borderline if unexpected output
        logger.warning(f"Unexpected classification: {result!r}, defaulting to borderline")
        return "borderline"
    except Exception as e:
        logger.error(f"Email classification failed: {e}")
        return "borderline"


# =========================================================================
# Extraction (Sonnet — only for relevant emails)
# =========================================================================

async def extract_email_intelligence(
    sender: str,
    subject: str,
    body: str,
) -> list[dict]:
    """
    Extract structured items from a relevant email.

    Uses Sonnet for accuracy. Only called on emails classified as
    relevant or borderline.

    Args:
        sender: Sender email or name.
        subject: Email subject line.
        body: Full email body.

    Returns:
        List of extracted items, each with:
        - type: task, decision, commitment, information, stakeholder_mention,
                deadline_change, gantt_relevant
        - text: Description
        - assignee/speaker/entity: Type-specific field
        - related_to: Cross-reference to known item (for decisions/info)
        - sensitive: True if investor/legal content
    """
    system = (
        "You are an intelligence extraction assistant for CropSight, an AgTech startup. "
        "Extract structured operational items from email correspondence.\n\n"
        "Team: Eyal Zror (CEO), Roye Tadmor (CTO), Paolo Vailetti (BD), Prof. Yoram Weiss (Advisor).\n\n"
        "RULES:\n"
        "- Summarize, don't quote raw email text\n"
        "- Attribute as 'from email correspondence'\n"
        "- Use first names only for team members\n"
        "- Flag sensitive content (investor, legal, financial) with sensitive: true\n"
        "- For decisions/information, include related_to field if topic matches known items\n"
        "- Return empty array if no actionable items\n\n"
        "Return ONLY a JSON array. Each item:\n"
        '{"type": "task|decision|commitment|information|stakeholder_mention|deadline_change|gantt_relevant", '
        '"text": "...", "assignee": "...", "speaker": "...", "entity": "...", '
        '"related_to": "...", "sensitive": false}'
    )

    prompt = f"Sender: {sender}\nSubject: {subject}\n\nBody:\n{body[:3000]}"

    try:
        text, _usage = call_llm(
            prompt=prompt,
            model=settings.model_agent,
            max_tokens=2048,
            system=system,
            call_site="email_extract",
        )

        # Parse JSON from response
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        items = json.loads(text)
        if isinstance(items, list):
            return items
        if isinstance(items, dict) and "items" in items:
            return items["items"]
        return []

    except json.JSONDecodeError:
        # Try to find array in the text
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        logger.warning(f"Could not parse email extraction JSON: {text[:200]}")
        return []
    except Exception as e:
        logger.error(f"Email intelligence extraction failed: {e}")
        return []


# =========================================================================
# Live Keyword Builder (cached per scan run)
# =========================================================================

_keyword_cache: list[str] | None = None
_keyword_cache_scan_id: str | None = None


def build_filter_keywords(scan_id: str | None = None) -> list[str]:
    """
    Build comprehensive live keyword list for email filtering.

    Sources:
    1. Hardcoded baseline (always available)
    2. Entity registry: names + aliases
    3. Open/in-progress task title keywords
    4. Recent decision topics (last 30 days)

    Cached per scan run (one batch of Supabase queries, not per-email).

    Args:
        scan_id: Optional scan identifier for cache invalidation.

    Returns:
        Deduplicated list of keywords (lowercase).
    """
    global _keyword_cache, _keyword_cache_scan_id

    # Return cached if same scan
    if _keyword_cache is not None and scan_id and scan_id == _keyword_cache_scan_id:
        return _keyword_cache

    keywords = set()

    # 1. Hardcoded baseline
    from config.team import CROPSIGHT_EMAIL_KEYWORDS_BASELINE
    for kw in CROPSIGHT_EMAIL_KEYWORDS_BASELINE:
        keywords.add(kw.lower())

    # 2. Entity registry
    try:
        from services.supabase_client import supabase_client
        entities = supabase_client.list_entities(limit=200)
        for entity in entities:
            name = entity.get("canonical_name", "")
            if name and len(name) > 2:
                keywords.add(name.lower())
            for alias in entity.get("aliases", []) or []:
                if alias and len(alias) > 2:
                    keywords.add(alias.lower())
    except Exception as e:
        logger.warning(f"Could not load entities for keyword list: {e}")

    # 3. Open/in-progress task keywords
    try:
        from services.supabase_client import supabase_client
        tasks = supabase_client.get_tasks(status="pending", limit=50)
        tasks += supabase_client.get_tasks(status="in_progress", limit=50)
        for task in tasks:
            title = task.get("title", "")
            # Extract significant words (>3 chars)
            for word in title.split():
                word_clean = word.strip(".,!?:;()[]\"'").lower()
                if len(word_clean) > 3:
                    keywords.add(word_clean)
    except Exception as e:
        logger.warning(f"Could not load tasks for keyword list: {e}")

    # 4. Recent decision topics
    try:
        from services.supabase_client import supabase_client
        decisions = supabase_client.list_decisions(limit=30)
        for d in decisions:
            desc = d.get("description", "")
            for word in desc.split():
                word_clean = word.strip(".,!?:;()[]\"'").lower()
                if len(word_clean) > 4:
                    keywords.add(word_clean)
    except Exception as e:
        logger.warning(f"Could not load decisions for keyword list: {e}")

    result = sorted(keywords)
    _keyword_cache = result
    _keyword_cache_scan_id = scan_id
    return result


def clear_keyword_cache():
    """Clear the keyword cache (for testing)."""
    global _keyword_cache, _keyword_cache_scan_id
    _keyword_cache = None
    _keyword_cache_scan_id = None
