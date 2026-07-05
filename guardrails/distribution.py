"""Central recipient-selection + tier-capping for content distribution.

Distribution BANDS (nested): CEO ⊂ Founders ⊂ Company. Each band maps to a
clearance LEVEL from models.schemas.TIER_LEVELS:

    CEO      -> level 4  (Eyal only)
    Founders -> level 3  (founding team)
    Company  -> level 2  (all staff; the schema's level-2 "team" tier, surfaced
                          to Eyal as the "Company" band)

Recipients for a band = every ACTIVE roster member whose OWN tier-level is >= the
band level (nested clearance). Content sent to a band is capped to the band level
(items above are stripped via filter_by_sensitivity) so no recipient ever sees an
item above their tier. This is per-SEND but leak-safe because recipients are
exactly the band's members and the content ceiling matches the band.

Replaces the old 2-outcome get_distribution_list (ceo->Eyal / else->the four
EYAL/ROYE/PAOLO/YORAM env emails). Until the roster gains members ABOVE the
current four (Matti=founders, Marco/Hadar/Ido=team), every function here returns
byte-identical results to the old behavior — the rewire is safe to ship before
the roster cutover. See project_distribution_groups_2026_07_05.
"""

import logging

from models.schemas import TIER_LEVELS, filter_by_sensitivity

logger = logging.getLogger(__name__)

# Distribution-band name -> clearance level (mirrors TIER_LEVELS values).
BAND_LEVEL = {"ceo": 4, "founders": 3, "company": 2}

# Any stored/legacy sensitivity value -> the band it distributes as.
_SENSITIVITY_TO_BAND = {
    "ceo": "ceo", "ceo_only": "ceo", "restricted": "ceo", "sensitive": "ceo", "legal": "ceo",
    "founders": "founders", "normal": "founders",
    "team": "company", "company": "company", "public": "company",
}


def band_for_sensitivity(sensitivity: str | None) -> str:
    """Map a meeting/item sensitivity value to its distribution band name."""
    return _SENSITIVITY_TO_BAND.get((sensitivity or "founders").lower(), "founders")


def level_for_band(band: str) -> int:
    """Clearance level for a band name (defaults to Founders=3)."""
    return BAND_LEVEL.get((band or "founders").lower(), 3)


def level_for_sensitivity(sensitivity: str | None) -> int:
    """Content ceiling (level) for a send at this sensitivity/band."""
    return level_for_band(band_for_sensitivity(sensitivity))


def recipients_for_band(band: str, *, exclude_eyal: bool = False) -> list[str]:
    """Active roster emails cleared for `band` (nested: member tier-level >= band level).

    Dev/non-production -> Eyal only (preserves the old get_distribution_list dev
    guard). `exclude_eyal` supports team-only packages that deliberately drop the
    CEO (e.g. weekly_team_package). Falls back to Eyal's email if the roster can't
    be read, so a send is never silently addressed to nobody.
    """
    from config.settings import settings

    eyal = (settings.EYAL_EMAIL or "").strip()

    if settings.ENVIRONMENT != "production":
        return [] if exclude_eyal else ([eyal] if eyal else [])

    need = level_for_band(band)
    out: list[str] = []
    seen: set[str] = set()
    try:
        from config.team import TEAM_MEMBERS

        for m in TEAM_MEMBERS.values():
            if (m.get("status") or "active") != "active":
                continue
            tier = (m.get("tier") or "founders").lower()
            if TIER_LEVELS.get(tier, 3) < need:
                continue
            email = (m.get("email") or "").strip()
            key = email.lower()
            if not email or key in seen:
                continue
            if exclude_eyal and eyal and key == eyal.lower():
                continue
            seen.add(key)
            out.append(email)
    except Exception as e:  # never let a roster hiccup blackhole a send
        logger.error(f"recipients_for_band({band}) roster read failed: {e}")

    if not out and not exclude_eyal and eyal:
        return [eyal]
    return out


def cap_items_for_band(items: list[dict], band: str) -> list[dict]:
    """Strip items whose sensitivity is above the band's clearance level."""
    return filter_by_sensitivity(items or [], level_for_band(band))


def member_keys_for_band(band: str) -> list[str]:
    """Active roster member_keys cleared for `band` (for the Custom picker default)."""
    try:
        from config.team import TEAM_MEMBERS

        need = level_for_band(band)
        return [
            k
            for k, m in TEAM_MEMBERS.items()
            if (m.get("status") or "active") == "active"
            and TIER_LEVELS.get((m.get("tier") or "founders").lower(), 3) >= need
        ]
    except Exception as e:
        logger.error(f"member_keys_for_band({band}) failed: {e}")
        return []


def resolve_custom_recipients(
    member_keys: list[str] | None, *, override: bool = False
) -> tuple[list[str], int]:
    """Emails + content ceiling for an explicit CUSTOM recipient set (the picker).

    Leak-safe by default: the content cap is the LOWEST clearance among the
    selected people, so nobody in a mixed-tier custom send sees above their own
    tier. `override=True` (Eyal's deliberate "send full to all selected") lifts
    the cap to CEO(4) — everyone selected gets the complete summary.

    Dev/non-production -> Eyal only (same guard as recipients_for_band).

    Returns (emails, cap_level). Empty emails if nothing resolvable.
    """
    from config.settings import settings

    eyal = (settings.EYAL_EMAIL or "").strip()
    if settings.ENVIRONMENT != "production":
        return ([eyal] if eyal else [], 4)

    keys = {(k or "").lower().strip() for k in (member_keys or []) if k}
    emails: list[str] = []
    levels: list[int] = []
    seen: set[str] = set()
    try:
        from config.team import TEAM_MEMBERS

        for key, m in TEAM_MEMBERS.items():
            if key.lower() not in keys:
                continue
            if (m.get("status") or "active") != "active":
                continue
            email = (m.get("email") or "").strip()
            if not email or email.lower() in seen:
                continue
            seen.add(email.lower())
            emails.append(email)
            levels.append(TIER_LEVELS.get((m.get("tier") or "founders").lower(), 3))
    except Exception as e:
        logger.error(f"resolve_custom_recipients failed: {e}")

    if not emails:
        return ([], 2)
    cap = 4 if override else min(levels)
    return (emails, cap)
