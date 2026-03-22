"""
Escalation configuration for task overdue handling.

Priority-aware thresholds: high-priority tasks escalate faster than low-priority ones.
Values represent days overdue before reaching each tier.
"""

# Priority-aware escalation tiers
# {priority: {tier: days_overdue_threshold}}
ESCALATION_TIERS = {
    "H": {"low": 2, "medium": 5, "high": 10, "critical": 11},
    "M": {"low": 3, "medium": 7, "high": 14, "critical": 15},
    "L": {"low": 7, "medium": 14, "high": 21, "critical": 22},
}

# Tier descriptions for display
TIER_LABELS = {
    "low": "Mention in weekly review",
    "medium": "Flag as attention needed",
    "high": "Proactive alert to Eyal",
    "critical": "Escalation alert — requires immediate attention",
}

# Recurring discussion threshold (meetings before pattern alert)
RECURRING_TOPIC_THRESHOLD = 3


def classify_overdue_tier(days_overdue: int, priority: str = "M") -> str | None:
    """
    Classify an overdue task into an escalation tier based on priority and days overdue.

    Args:
        days_overdue: Number of days the task is overdue.
        priority: Task priority (H, M, L).

    Returns:
        Tier name ('low', 'medium', 'high', 'critical') or None if not overdue.
    """
    if days_overdue <= 0:
        return None

    tiers = ESCALATION_TIERS.get(priority, ESCALATION_TIERS["M"])

    if days_overdue >= tiers["critical"]:
        return "critical"
    if days_overdue >= tiers["high"]:
        return "high"
    if days_overdue >= tiers["medium"]:
        return "medium"
    if days_overdue >= tiers["low"]:
        return "low"

    return None
