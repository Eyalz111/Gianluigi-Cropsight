"""Seed the team_members table from the hardcoded config/team roster so the DB
roster is byte-identical to today's. Dry-run by default; --apply to write.
Idempotent (upsert on member_key). Run once after applying migrate_phase_team_roster.sql.

Usage:
    python scripts/seed_team_members.py            # dry-run preview
    python scripts/seed_team_members.py --apply    # write rows
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.team import _HARDCODED_TEAM_MEMBERS, TEAM_TELEGRAM_IDS
from services.supabase_client import supabase_client

# Sensitivity tier per member (maps to the Sensitivity enum). Eyal sees all (ceo);
# the rest default to founders. New hires added later default to 'team'.
_TIER = {"eyal": "ceo"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write rows (default: dry-run)")
    args = ap.parse_args()

    rows = []
    for key, m in _HARDCODED_TEAM_MEMBERS.items():
        rows.append({
            "member_key": key,
            "name": m.get("name", ""),
            "role": m.get("role", ""),
            "role_description": m.get("role_description", ""),
            "primary_email": m.get("email", ""),
            "identities": [i for i in (m.get("identities") or []) if i],
            "tier": _TIER.get(key, "founders"),
            "telegram_id": TEAM_TELEGRAM_IDS.get(key),
            "is_admin": bool(m.get("is_admin", False)),
        })

    print(f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(rows)} team members")
    for r in rows:
        print(f"  {r['member_key']:8} {r['name']:20} tier={r['tier']:8} "
              f"admin={r['is_admin']} tg={r['telegram_id']}")

    if not args.apply:
        print("\nRe-run with --apply to write.")
        return

    for r in rows:
        supabase_client.add_team_member(
            member_key=r["member_key"], name=r["name"], role=r["role"],
            role_description=r["role_description"], primary_email=r["primary_email"],
            identities=r["identities"], tier=r["tier"],
            telegram_id=r["telegram_id"], is_admin=r["is_admin"],
        )
    print("Applied.")


if __name__ == "__main__":
    main()
