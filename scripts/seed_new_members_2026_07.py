#!/usr/bin/env python3
"""
CUTOVER seed (2026-07): add the 4 new internal members to the team_members roster
with their distribution tiers, for the distribution-groups feature.

Tiers (clearance ceiling; recipients_for_band selects members with tier-level >=
the band level): Matti=founders (CEO⊂Founders), Marco/Hadar/Ido=team (=Company band).

⚠️ DO NOT RUN until the distribution-groups code is DEPLOYED. Adding roster members
changes calendar/email/task behavior (all correct for INTERNAL staff) and only takes
effect on the next process restart (roster builds at import). Run at cutover, then
redeploy/restart. All 4 are is_admin=False (bot commands stay Eyal-only via _is_admin).

Names/roles below are best-effort — CONFIRM with Eyal before running.

Usage:
    python scripts/seed_new_members_2026_07.py            # dry-run (default)
    python scripts/seed_new_members_2026_07.py --apply    # execute (cutover)
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: F401  (ensures .env loads)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# member_key, name, role, tier, primary_email  (all internal; confirm names/roles)
NEW_MEMBERS = [
    ("matti", "Matti Sevitt", "Founder",   "founders", "mattisevitt@gmail.com"),
    ("marco", "Marco Sutter", "Marketing (Italy)", "team", "marcosutter@marcosutter.com"),
    ("hadar", "Hadar",        "Team",      "team", "Hadars1111@gmail.com"),
    ("ido",   "Ido",          "Team",      "team", "ido@kunst.co.il"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually upsert (default: dry-run)")
    args = parser.parse_args()

    from services.supabase_client import supabase_client

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info(f"=== seed_new_members_2026_07.py ({mode}) ===")
    for key, name, role, tier, email in NEW_MEMBERS:
        logger.info(f"  {key:6} tier={tier:9} {name!r} <{email}> role={role!r}")
        if args.apply:
            row = supabase_client.add_team_member(
                member_key=key, name=name, role=role, tier=tier,
                primary_email=email, identities=[email],
                telegram_id=None, is_admin=False,
            )
            logger.info(f"    -> {'OK' if row else 'FAILED'}")

    if not args.apply:
        logger.info("Dry run. Re-run with --apply at cutover (AFTER the code is deployed), then restart the service.")
    else:
        logger.info("Seeded. RESTART/redeploy the service so the roster rebuilds at import.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
