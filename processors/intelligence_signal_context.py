"""
Intelligence Signal context builder.

Assembles operational context from Supabase and constructs research queries
for Perplexity. Pulls recent tasks, decisions, competitor watchlist, and
generates exploration queries that rotate weekly to prevent filter-bubble effects.

Usage:
    from processors.intelligence_signal_context import (
        build_context_packet, build_research_queries, build_exploration_queries
    )

    context = build_context_packet()
    queries = build_research_queries(context)
    exploration = build_exploration_queries(context["week_number"])
    all_queries = queries + exploration
"""

import logging
from datetime import datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# ── Defaults (used when Supabase is sparse) ────────────────────────────

DEFAULT_ACTIVE_CROPS = ["wheat", "corn", "soybeans", "coffee", "cocoa", "grapes"]
DEFAULT_ACTIVE_REGIONS = ["EU", "USA", "Israel", "Brazil", "India", "Black Sea"]
DEFAULT_TECH_KEYWORDS = [
    "satellite crop monitoring",
    "ML yield prediction",
    "hyperspectral imaging",
    "SAR agriculture",
    "weather-crop models",
    "foundation models for agriculture",
]

# ── Exploration query pools ────────────────────────────────────────────
# Rotate via week_number % len(pool). Each pool targets a different
# out-of-scope dimension to prevent the signal becoming a filter bubble.

_ADJACENT_MARKETS = [
    "aquaculture yield prediction and satellite monitoring",
    "forestry biomass estimation with satellite and ML",
    "livestock health monitoring with remote sensing",
    "palm oil plantation monitoring and sustainability tracking",
]

_WILD_CARD_CROPS = [
    "rice paddy monitoring with SAR satellite imagery",
    "avocado and tropical fruit yield forecasting",
    "saffron and high-value spice crop technology",
    "vanilla supply chain and Madagascar crop forecasting",
    "cotton yield prediction and water stress monitoring",
]

_UNEXPLORED_GEOGRAPHIES = [
    "Indonesia agricultural technology and government programs",
    "Kenya and East Africa precision agriculture investments",
    "Peru and Latin America AgTech startup ecosystem",
    "Vietnam and Southeast Asia crop monitoring technology",
    "Thailand smart farming and digital agriculture policy",
]


def build_context_packet() -> dict:
    """
    Build the operational context packet from Supabase.

    Gathers recent tasks, decisions, competitor watchlist, and last signal
    flags to provide the intelligence signal agent with current CropSight state.
    All Supabase calls are SYNC.

    Returns:
        Dict with week_number, year, signal_id, active_crops, active_regions,
        active_bd_pipeline, technical_focus, known_competitors,
        last_signal_flags, open_tasks_summary.
    """
    now = datetime.now(timezone.utc)
    iso_cal = now.isocalendar()
    week_number = iso_cal[1]
    year = iso_cal[0]
    signal_id = f"signal-w{week_number}-{year}"

    # Gather operational data (all SYNC, all fault-tolerant)
    competitors = _get_active_competitors()
    bd_tasks = _get_bd_pipeline()
    tech_tasks = _get_technical_focus()
    active_crops = _extract_active_crops()
    active_regions = _extract_active_regions()
    last_flags = _get_last_signal_flags()
    open_tasks = _get_open_tasks_summary()

    context = {
        "week_number": week_number,
        "year": year,
        "signal_id": signal_id,
        "active_crops": active_crops or DEFAULT_ACTIVE_CROPS,
        "active_regions": active_regions or DEFAULT_ACTIVE_REGIONS,
        "active_bd_pipeline": bd_tasks,
        "technical_focus": tech_tasks,
        "known_competitors": competitors,
        "last_signal_flags": last_flags,
        "open_tasks_summary": open_tasks,
    }

    logger.info(
        f"Context packet built for {signal_id}: "
        f"{len(competitors)} competitors, {len(active_crops or DEFAULT_ACTIVE_CROPS)} crops"
    )
    return context


def build_research_queries(context: dict) -> list[dict]:
    """
    Generate Perplexity research queries from the context packet.

    Produces ~7-8 queries covering market overview, competitors, science/tech,
    regulation, customer segment, and AgTech funding.

    Args:
        context: Output of build_context_packet().

    Returns:
        list[dict] with section, query, system_prompt keys — ready for
        perplexity_client.search_batch().
    """
    crops = ", ".join(context.get("active_crops", DEFAULT_ACTIVE_CROPS))
    regions = ", ".join(context.get("active_regions", DEFAULT_ACTIVE_REGIONS))
    competitors = context.get("known_competitors", [])
    competitor_names = ", ".join(c["name"] for c in competitors) if competitors else "AgTech startups"

    queries = [
        {
            "section": "market_overview",
            "query": (
                f"What are the most significant developments in agricultural commodity "
                f"markets this week? Focus on price movements, supply disruptions, and "
                f"policy changes affecting {crops}."
            ),
            "system_prompt": (
                "You are a commodity market analyst. Provide factual, concise updates "
                "with specific numbers and dates. No opinions or recommendations."
            ),
        },
        {
            "section": "competitor_landscape",
            "query": (
                f"What is the latest news about these AgTech companies: {competitor_names}? "
                f"Include funding rounds, product launches, partnerships, and hiring signals."
            ),
            "system_prompt": (
                "You are a competitive intelligence analyst covering AgTech. Report "
                "only verifiable facts from the past 2 weeks. Flag unconfirmed rumors."
            ),
        },
        {
            "section": "science_tech",
            "query": (
                "What are the latest advances in satellite crop monitoring, ML yield "
                "prediction, hyperspectral imaging, SAR agriculture, and weather-crop "
                "models? Focus on developments relevant to crop yield forecasting."
            ),
            "system_prompt": (
                "You are a science journalist covering AgTech and remote sensing. "
                "Prioritize peer-reviewed or credible industry sources."
            ),
        },
        {
            "section": "regulation_policy",
            "query": (
                f"What are recent regulatory or policy changes affecting agricultural "
                f"technology companies, satellite data providers, or commodity trading? "
                f"Focus on {regions}."
            ),
            "system_prompt": (
                "You are a regulatory affairs analyst. Cite specific legislation, "
                "directives, or official announcements with dates."
            ),
        },
        {
            "section": "customer_segment",
            "query": (
                "What are commodity traders, hedge funds, and agricultural insurers "
                "saying about crop forecasting and yield prediction technology? "
                "Any recent adoption signals or RFPs?"
            ),
            "system_prompt": (
                "You are a B2B market researcher focused on financial services and "
                "commodity trading. Report demand signals and buying behavior."
            ),
        },
        {
            "section": "agtech_funding",
            "query": (
                "What AgTech and agricultural AI companies received funding, were acquired, "
                "or announced significant partnerships in the last two weeks?"
            ),
            "system_prompt": (
                "You are a venture capital analyst covering AgTech. Report deal size, "
                "investors, and strategic relevance. Cite sources."
            ),
        },
        {
            "section": "regional_watch",
            "query": (
                f"What are the latest crop production reports, weather events, or "
                f"agricultural policy changes in {regions} this week? Focus on events "
                f"that could affect {crops} supply or prices."
            ),
            "system_prompt": (
                "You are an agricultural market correspondent. Report region-specific "
                "events with quantitative impact estimates where possible."
            ),
        },
    ]

    # Add continuity query if we have flags from last signal
    last_flags = context.get("last_signal_flags", [])
    if last_flags:
        flag_texts = ", ".join(
            f.get("flag", "") for f in last_flags if isinstance(f, dict)
        )
        if flag_texts:
            queries.append({
                "section": "continuity",
                "query": (
                    f"What has happened since last week regarding these topics: {flag_texts}? "
                    f"Any updates, resolutions, or escalations?"
                ),
                "system_prompt": (
                    "You are following up on flagged stories from last week. "
                    "Report only new developments since last week."
                ),
            })

    logger.info(f"Built {len(queries)} research queries")
    return queries


def build_exploration_queries(week_number: int) -> list[dict]:
    """
    Generate 2-3 rotating out-of-scope exploration queries.

    Uses week_number modulo to cycle through pools of adjacent markets,
    wild card crops, and unexplored geographies. This prevents the
    intelligence signal from becoming a filter bubble.

    Args:
        week_number: ISO week number for rotation.

    Returns:
        list[dict] with section, query, system_prompt keys.
    """
    queries = []

    # Pick one from each pool, rotating weekly
    adj_idx = week_number % len(_ADJACENT_MARKETS)
    crop_idx = week_number % len(_WILD_CARD_CROPS)
    geo_idx = week_number % len(_UNEXPLORED_GEOGRAPHIES)

    queries.append({
        "section": "exploration_adjacent",
        "query": (
            f"What are the most interesting recent developments in "
            f"{_ADJACENT_MARKETS[adj_idx]}?"
        ),
        "system_prompt": (
            "Briefly cover emerging tech and market demand. "
            "Focus on what's transferable to crop yield forecasting."
        ),
    })

    queries.append({
        "section": "exploration_crop",
        "query": (
            f"What are the latest developments in {_WILD_CARD_CROPS[crop_idx]}? "
            f"Include market size, technology adoption, and key players."
        ),
        "system_prompt": (
            "Report on crops outside CropSight's current coverage. "
            "Highlight market opportunity and technology gaps."
        ),
    })

    # Every 3rd week, add a geography exploration query
    if week_number % 3 == 0:
        queries.append({
            "section": "exploration_geography",
            "query": (
                f"What is happening in {_UNEXPLORED_GEOGRAPHIES[geo_idx]}? "
                f"Include government programs, startup activity, and adoption trends."
            ),
            "system_prompt": (
                "Focus on market entry opportunities and technology gaps "
                "in emerging agricultural markets."
            ),
        })

    logger.info(
        f"Exploration queries for W{week_number}: "
        f"{[q['section'] for q in queries]}"
    )
    return queries


# ── Internal helpers (all SYNC, all fault-tolerant) ────────────────────


def _get_active_competitors() -> list[dict]:
    """Get active competitors from the watchlist."""
    try:
        result = (
            supabase_client.client.table("competitor_watchlist")
            .select("name, category, funding, target_customer, key_limitation")
            .eq("is_active", True)
            .order("category")
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Failed to fetch competitor watchlist: {e}")
        return []


def _get_bd_pipeline() -> list[dict]:
    """Get Paolo's active BD tasks for business context."""
    try:
        tasks = supabase_client.get_tasks(assignee="Paolo", status="pending", limit=10)
        return [
            {"title": t.get("title", ""), "priority": t.get("priority", "")}
            for t in (tasks or [])
        ]
    except Exception as e:
        logger.warning(f"Could not fetch BD pipeline: {e}")
        return []


def _get_technical_focus() -> list[dict]:
    """Get Roye's active tasks for technical context."""
    try:
        tasks = supabase_client.get_tasks(assignee="Roye", status="in_progress", limit=10)
        return [
            {"title": t.get("title", ""), "priority": t.get("priority", "")}
            for t in (tasks or [])
        ]
    except Exception as e:
        logger.warning(f"Could not fetch technical focus: {e}")
        return []


def _extract_active_crops() -> list[str]:
    """
    Extract crop names from recent meeting summaries and tasks.

    Falls back to DEFAULT_ACTIVE_CROPS if insufficient data.
    """
    known_crops = [
        "wheat", "corn", "soybeans", "coffee", "cocoa", "grapes",
        "rice", "cotton", "sugar", "barley", "oats", "canola",
        "avocado", "citrus", "almonds", "olive",
    ]

    try:
        # Check recent task titles and decisions for crop mentions
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        result = (
            supabase_client.client.table("action_items")
            .select("title")
            .gte("created_at", recent_cutoff)
            .limit(50)
            .execute()
        )
        texts = " ".join(
            t.get("title", "").lower() for t in (result.data or [])
        )

        found = [c for c in known_crops if c in texts]
        return found if len(found) >= 3 else []

    except Exception as e:
        logger.warning(f"Could not extract active crops: {e}")
        return []


def _extract_active_regions() -> list[str]:
    """
    Extract active regions from recent operational data.

    Falls back to DEFAULT_ACTIVE_REGIONS if insufficient data.
    """
    known_regions = [
        "Moldova", "Black Sea", "Brazil", "India", "EU", "USA",
        "Israel", "Italy", "Ukraine", "Argentina", "Vietnam",
        "Colombia", "Ethiopia", "Indonesia", "Kenya",
    ]

    try:
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        result = (
            supabase_client.client.table("action_items")
            .select("title")
            .gte("created_at", recent_cutoff)
            .limit(50)
            .execute()
        )
        texts = " ".join(
            t.get("title", "").lower() for t in (result.data or [])
        )

        found = [r for r in known_regions if r.lower() in texts]
        return found if len(found) >= 2 else []

    except Exception as e:
        logger.warning(f"Could not extract active regions: {e}")
        return []


def _get_last_signal_flags() -> list[dict]:
    """Get flags from the last 3-4 signals for continuity."""
    try:
        result = (
            supabase_client.client.table("intelligence_signals")
            .select("flags, week_number, year")
            .not_.is_("flags", "null")
            .order("created_at", desc=True)
            .limit(4)
            .execute()
        )
        all_flags = []
        for row in (result.data or []):
            flags = row.get("flags")
            if isinstance(flags, list):
                all_flags.extend(flags)
        return all_flags

    except Exception as e:
        logger.warning(f"Could not fetch last signal flags: {e}")
        return []


def _get_open_tasks_summary() -> dict:
    """Get summary counts of open tasks by assignee."""
    try:
        tasks = supabase_client.get_tasks(limit=100)
        if not tasks:
            return {}

        open_tasks = [
            t for t in tasks
            if t.get("status") in ("pending", "in_progress")
        ]

        by_assignee: dict[str, int] = {}
        for t in open_tasks:
            assignee = t.get("assignee", "unassigned")
            by_assignee[assignee] = by_assignee.get(assignee, 0) + 1

        return {
            "total_open": len(open_tasks),
            "by_assignee": by_assignee,
        }

    except Exception as e:
        logger.warning(f"Could not summarize open tasks: {e}")
        return {}
