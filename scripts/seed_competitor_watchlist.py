"""
Seed the competitor_watchlist table with known CropSight competitors.
Run once after migrate_intelligence_signal.sql is applied.

Usage:
    python scripts/seed_competitor_watchlist.py
"""

from services.supabase_client import supabase_client

KNOWN_COMPETITORS = [
    {
        "name": "EOSDA",
        "category": "known",
        "funding": "~$355K (grants only)",
        "target_customer": "1.17M farmers globally, Eastern Europe focus",
        "key_limitation": "Farmer-focused platform; no commodity trading customers; self-reported 80-90% accuracy",
        "notes": "$18.3M revenue 2025 (Getlatka). CBInsights, Crunchbase, LeadIQ sources.",
        "added_by": "system",
    },
    {
        "name": "SatYield",
        "category": "known",
        "funding": "$6M seed (2B.VC, Katao Venture Partners)",
        "target_customer": "Hedge funds, commodity traders (targeting)",
        "key_limitation": "Pre-revenue; grains only (corn, soy, wheat); accuracy claims unvalidated by third parties",
        "notes": "Founded 2024. Direct competitor in commodity-trader segment.",
        "added_by": "system",
    },
    {
        "name": "CropProphet",
        "category": "known",
        "funding": "Bootstrapped (operating since 2009)",
        "target_customer": "Grain traders, hedge funds, CTAs",
        "key_limitation": "100% grains after 16 years; no specialty crops; accuracy metrics undisclosed",
        "notes": "Most established bootstrapped competitor. Long track record but zero specialty crop coverage.",
        "added_by": "system",
    },
    {
        "name": "SeeTree",
        "category": "known",
        "funding": "$65.2M total (Series C, January 2024)",
        "target_customer": "Tree growers (Citrosuco ~50% revenue)",
        "key_limitation": "Per-farm model cannot provide independent market intelligence; coffee/cocoa listed but not deployed after 8 years",
        "notes": "400M trees managed. Heavily customer-concentration risk. Cannot do regional intelligence without farm contracts.",
        "added_by": "system",
    },
    {
        "name": "Gro Intelligence",
        "category": "known",
        "funding": "Raised significant VC (ceased operations 2023, monitoring for revival/acqui-hire)",
        "target_customer": "Commodity traders, food companies, governments",
        "key_limitation": "Ceased operations. Legacy — monitor for acqui-hire or relaunch signals.",
        "notes": "Was the primary premium competitor. Shutdown creates market gap CropSight can fill.",
        "added_by": "system",
    },
    {
        "name": "aWhere/DTN",
        "category": "known",
        "funding": "Part of DTN (private, large enterprise)",
        "target_customer": "Agricultural insurers, large agribusinesses",
        "key_limitation": "Legacy data provider; not ML-native; weak on specialty crops",
        "notes": "Incumbent data layer. Enterprise relationships are their moat.",
        "added_by": "system",
    },
    {
        "name": "Cropin",
        "category": "watching",
        "funding": "Series C (significant, India-based)",
        "target_customer": "Agribusinesses, governments, lenders (emerging markets)",
        "key_limitation": "Satellites + AI for traceability/risk scoring; moving toward EUDR compliance — adjacent threat",
        "notes": "Most active in emerging markets. Walmart partnership. Watch for coffee/cocoa move.",
        "added_by": "system",
    },
]


def seed():
    """Seed competitor watchlist. Safe to re-run (upsert on name)."""
    count = 0
    for competitor in KNOWN_COMPETITORS:
        try:
            supabase_client.client.table("competitor_watchlist").upsert(
                competitor, on_conflict="name"
            ).execute()
            count += 1
            print(f"  Seeded: {competitor['name']}")
        except Exception as e:
            print(f"  Failed: {competitor['name']} -- {e}")
    print(f"\nSeeded {count}/{len(KNOWN_COMPETITORS)} competitors.")


if __name__ == "__main__":
    print("Seeding competitor watchlist...")
    seed()
