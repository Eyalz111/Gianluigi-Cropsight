"""
Decision review processor — surfaces decisions due for periodic review.

Decisions have a review_date field (default 30 days from creation).
This processor queries upcoming reviews and formats them for the weekly
review attention section.

Usage:
    from processors.decision_review import get_decisions_due_for_review
    decisions = get_decisions_due_for_review(days_ahead=7)
"""

import logging

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


def get_decisions_due_for_review(days_ahead: int = 7) -> list[dict]:
    """
    Get active decisions with review dates within the next N days.

    Args:
        days_ahead: Look-ahead window in days.

    Returns:
        List of decision dicts with meeting context, sorted by review_date.
    """
    try:
        decisions = supabase_client.get_decisions_for_review(days_ahead=days_ahead)

        # Enrich with readable format
        enriched = []
        for d in decisions:
            meeting_info = d.get("meetings", {}) or {}
            enriched.append({
                "decision_id": d.get("id"),
                "label": d.get("label", ""),
                "description": d.get("description", "")[:100],
                "confidence": d.get("confidence", 3),
                "review_date": d.get("review_date"),
                "source_meeting": meeting_info.get("title", "Unknown"),
                "meeting_date": meeting_info.get("date", ""),
                "rationale": (d.get("rationale") or "")[:80],
            })

        logger.info(f"Found {len(enriched)} decisions due for review (next {days_ahead} days)")
        return enriched

    except Exception as e:
        logger.error(f"Error fetching decisions for review: {e}")
        return []
