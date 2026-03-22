"""
LLM cost calculator for Gianluigi.

Computes dollar costs from token_usage records using Anthropic's pricing.
Supports per-model breakdown, per-call-site attribution, and daily trends.

Pricing as of March 2026. Verify against Anthropic's pricing page if numbers seem off.
https://docs.anthropic.com/en/docs/about-claude/models

Includes prompt caching multipliers:
- cache_write (creation): 1.25x base input price
- cache_read (hit): 0.10x base input price
"""

import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

# Per 1M tokens pricing
MODEL_PRICING = {
    # Claude 4.6 family (current)
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,       # input * 0.10
        "cache_write": 18.75,    # input * 1.25
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.0,
        "cache_read": 0.08,
        "cache_write": 1.0,
    },
    # Aliases for model name variations
    "claude-sonnet-4-20250514": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "claude-haiku-3-5-20241022": {
        "input": 0.80,
        "output": 4.0,
        "cache_read": 0.08,
        "cache_write": 1.0,
    },
}

# Default pricing for unknown models (use Sonnet as safe middle ground)
_DEFAULT_PRICING = {
    "input": 3.0,
    "output": 15.0,
    "cache_read": 0.3,
    "cache_write": 3.75,
}


def _get_pricing(model: str) -> dict:
    """Get pricing for a model, with fallback for unknown models."""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # Try partial match (e.g., "opus" in model name)
    model_lower = model.lower()
    if "opus" in model_lower:
        return MODEL_PRICING["claude-opus-4-6"]
    if "sonnet" in model_lower:
        return MODEL_PRICING["claude-sonnet-4-6"]
    if "haiku" in model_lower:
        return MODEL_PRICING["claude-haiku-4-5"]
    logger.debug(f"Unknown model '{model}', using default pricing")
    return _DEFAULT_PRICING


def _calc_record_cost(record: dict) -> float:
    """Calculate dollar cost for a single token_usage record."""
    model = record.get("model", "")
    pricing = _get_pricing(model)

    input_tokens = record.get("input_tokens", 0) or 0
    output_tokens = record.get("output_tokens", 0) or 0
    cache_read = record.get("cache_read_tokens", 0) or 0
    cache_write = record.get("cache_creation_tokens", 0) or 0

    cost = (
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"]
        + (cache_read / 1_000_000) * pricing["cache_read"]
        + (cache_write / 1_000_000) * pricing["cache_write"]
    )
    return cost


def compute_cost_summary(records: list[dict]) -> dict:
    """
    Aggregate token usage records into a cost summary.

    Args:
        records: List of token_usage records from Supabase.

    Returns:
        Dict with total_cost, by_model, by_call_site, daily_trend.
    """
    total_cost = 0.0
    by_model: dict[str, dict] = defaultdict(lambda: {
        "cost": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0,
    })
    by_call_site: dict[str, dict] = defaultdict(lambda: {
        "cost": 0.0, "calls": 0,
    })
    by_day: dict[str, float] = defaultdict(float)

    for record in records:
        cost = _calc_record_cost(record)
        total_cost += cost

        model = record.get("model", "unknown")
        by_model[model]["cost"] += cost
        by_model[model]["input_tokens"] += (record.get("input_tokens", 0) or 0)
        by_model[model]["output_tokens"] += (record.get("output_tokens", 0) or 0)
        by_model[model]["calls"] += 1

        call_site = record.get("call_site", "unknown")
        by_call_site[call_site]["cost"] += cost
        by_call_site[call_site]["calls"] += 1

        created_at = record.get("created_at", "")
        if created_at:
            day = created_at[:10]  # YYYY-MM-DD
            by_day[day] += cost

    # Round costs
    total_cost = round(total_cost, 4)
    for v in by_model.values():
        v["cost"] = round(v["cost"], 4)
    for v in by_call_site.values():
        v["cost"] = round(v["cost"], 4)

    # Sort daily trend chronologically
    daily_trend = [
        {"date": day, "cost": round(cost, 4)}
        for day, cost in sorted(by_day.items())
    ]

    # Sort by_call_site by cost descending
    sorted_sites = dict(
        sorted(by_call_site.items(), key=lambda x: x[1]["cost"], reverse=True)
    )

    return {
        "total_cost": total_cost,
        "currency": "USD",
        "record_count": len(records),
        "by_model": dict(by_model),
        "by_call_site": sorted_sites,
        "daily_trend": daily_trend,
    }
