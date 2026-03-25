"""
Gantt intelligence — computed metrics from existing Gantt data.

Read-only analytics: velocity, slippage ratio, milestone risk score,
and Now-Next-Later view. Does NOT change the Gantt sheet structure.

Usage:
    from processors.gantt_intelligence import compute_gantt_metrics, generate_now_next_later
    metrics = await compute_gantt_metrics()
    nnl = await generate_now_next_later()
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def compute_gantt_metrics() -> dict:
    """
    Compute operational metrics from current Gantt data.

    Uses the parsed items from get_gantt_status() which already have
    status derived from cell background colors (via _color_to_status).

    Returns:
        Dict with velocity, slippage_ratio, milestone_risks, and summary.
    """
    from services.gantt_manager import gantt_manager

    metrics = {
        "velocity": None,
        "slippage_ratio": None,
        "milestone_risks": [],
        "summary": "",
    }

    try:
        # Get current week and next 4 weeks of Gantt data
        current_status = await gantt_manager.get_gantt_status()
        horizon = await gantt_manager.get_gantt_horizon(weeks_ahead=4)

        if "error" in current_status:
            metrics["summary"] = f"Gantt unavailable: {current_status['error']}"
            return metrics

        # Compute velocity from parsed items (status already derived from colors)
        items = current_status.get("items", [])
        active_cells = 0
        completed_cells = 0
        blocked_cells = 0
        total_cells = 0

        for item in items:
            total_cells += 1
            status = item.get("status", "unknown")
            if status == "completed":
                completed_cells += 1
            elif status in ("blocked", "at_risk"):
                blocked_cells += 1
            elif status in ("active", "planned"):
                active_cells += 1
            # "unknown" and other statuses count toward total only

        metrics["velocity"] = {
            "total_cells": total_cells,
            "active": active_cells,
            "completed": completed_cells,
            "blocked": blocked_cells,
        }

        # Slippage ratio
        if total_cells > 0:
            metrics["slippage_ratio"] = round(blocked_cells / total_cells, 2)
        else:
            metrics["slippage_ratio"] = 0.0

        # Milestone risk from horizon
        current_week = horizon.get("current_week", 0)
        milestones = horizon.get("milestones", [])
        for ms in milestones[:5]:
            weeks_away = ms.get("week", current_week) - current_week
            if weeks_away <= 4:
                metrics["milestone_risks"].append({
                    "milestone": ms.get("text", ms.get("subsection", "Unknown")),
                    "weeks_away": weeks_away,
                    "section": ms.get("section", ""),
                })

        # Summary
        parts = [f"Gantt: {total_cells} cells tracked"]
        if completed_cells:
            parts.append(f"{completed_cells} completed")
        if blocked_cells:
            parts.append(f"{blocked_cells} blocked/at risk")
        if active_cells:
            parts.append(f"{active_cells} active")
        if metrics["milestone_risks"]:
            parts.append(f"{len(metrics['milestone_risks'])} milestones in next 4 weeks")
        metrics["summary"] = ", ".join(parts)

    except Exception as e:
        logger.error(f"Gantt metrics computation failed: {e}")
        metrics["summary"] = f"Metrics unavailable: {e}"

    return metrics


async def generate_now_next_later() -> dict:
    """
    Auto-generate a Now-Next-Later view from Gantt data.

    - Now (this week + next 2): Active items with assigned owners
    - Next (weeks 3-6): Upcoming items
    - Later (weeks 7+): Planned items, less detail

    Returns:
        Dict with now, next, later lists.
    """
    from services.gantt_manager import gantt_manager

    result = {
        "now": [],
        "next": [],
        "later": [],
    }

    try:
        horizon = await gantt_manager.get_gantt_horizon(weeks_ahead=12)
        current_week = horizon.get("current_week", 0)
        milestones = horizon.get("milestones", [])

        for ms in milestones:
            weeks_away = ms.get("week", current_week) - current_week
            item = {
                "label": ms.get("text", ms.get("subsection", "")),
                "section": ms.get("section", ""),
                "weeks_away": weeks_away,
                "owner": ms.get("owner", ""),
            }

            if weeks_away <= 2:
                result["now"].append(item)
            elif weeks_away <= 6:
                result["next"].append(item)
            else:
                result["later"].append(item)

    except Exception as e:
        logger.error(f"Now-Next-Later generation failed: {e}")

    return result
