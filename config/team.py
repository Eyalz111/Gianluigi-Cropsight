"""
Team configuration for CropSight.

Contains team member information, email whitelists, calendar filters,
and blocklists used throughout the application.

This is the single source of truth for team-related configuration.
"""

from config.settings import settings


# =============================================================================
# CropSight Team Members
# =============================================================================

TEAM_MEMBERS = {
    "eyal": {
        "name": "Eyal Zror",
        "role": "CEO",
        "email": settings.EYAL_EMAIL,
        "is_admin": True,  # Has full access to all features
    },
    "roye": {
        "name": "Roye Tadmor",
        "role": "CTO",
        "email": settings.ROYE_EMAIL,
        "is_admin": False,
    },
    "paolo": {
        "name": "Paolo Vailetti",
        "role": "BD",
        "email": settings.PAOLO_EMAIL,
        "is_admin": False,
    },
    "yoram": {
        "name": "Prof. Yoram Weiss",
        "role": "Senior Advisor",
        "email": settings.YORAM_EMAIL,
        "is_admin": False,
    },
}


# =============================================================================
# Email Whitelist for Calendar Filtering
# =============================================================================

CROPSIGHT_TEAM_EMAILS = [
    settings.EYAL_EMAIL,
    settings.ROYE_EMAIL,
    settings.PAOLO_EMAIL,
    settings.YORAM_EMAIL,
]


# =============================================================================
# Calendar Filtering Configuration
# =============================================================================

# Title prefixes that indicate a CropSight meeting (case-insensitive)
CROPSIGHT_PREFIXES = [
    "cropsight",
    "cs:",
    "cs ",
    "crop sight",
    "cropsigh",  # Typo tolerance
    "crop-sight",
    "crop_sight",
]

# Keywords that indicate a personal/non-CropSight meeting (blocklist)
BLOCKED_KEYWORDS = [
    "ma ",          # Hebrew University MA program
    "seminar",
    "personal",
    "doctor",
    "dentist",
    "university",
    "hebrew university",
    "thesis",
    "birthday",
    "lunch",
    "dinner",
]


# =============================================================================
# Sensitivity Classification
# =============================================================================

# Keywords that indicate a sensitive meeting (Eyal-only distribution)
SENSITIVE_KEYWORDS = [
    # Legal
    "lawyer",
    "legal",
    "fischer",
    "fbc",
    "zohar",
    # Investor
    "investor",
    "investment",
    "funding",
    "vc",
    # Confidential
    "nda",
    "confidential",
    "founders agreement",
    # HR/Equity
    "personal",
    "hr",
    "compensation",
    "equity",
]


# =============================================================================
# Telegram Configuration
# =============================================================================

# Mapping of team member names to their Telegram chat IDs
# These are set via environment variables for privacy
TEAM_TELEGRAM_IDS: dict[str, int] = {}

# Populate from settings if available
if settings.EYAL_TELEGRAM_ID:
    TEAM_TELEGRAM_IDS["eyal"] = settings.EYAL_TELEGRAM_ID
    TEAM_TELEGRAM_IDS["eyal zror"] = settings.EYAL_TELEGRAM_ID

if settings.ROYE_TELEGRAM_ID:
    TEAM_TELEGRAM_IDS["roye"] = settings.ROYE_TELEGRAM_ID
    TEAM_TELEGRAM_IDS["roye tadmor"] = settings.ROYE_TELEGRAM_ID

if settings.PAOLO_TELEGRAM_ID:
    TEAM_TELEGRAM_IDS["paolo"] = settings.PAOLO_TELEGRAM_ID
    TEAM_TELEGRAM_IDS["paolo vailetti"] = settings.PAOLO_TELEGRAM_ID

if settings.YORAM_TELEGRAM_ID:
    TEAM_TELEGRAM_IDS["yoram"] = settings.YORAM_TELEGRAM_ID
    TEAM_TELEGRAM_IDS["yoram weiss"] = settings.YORAM_TELEGRAM_ID


# =============================================================================
# Helper Functions
# =============================================================================

def get_team_member(member_id: str) -> dict | None:
    """
    Look up a team member by their ID (e.g., 'eyal', 'roye').

    Args:
        member_id: The member key in TEAM_MEMBERS dict.

    Returns:
        Team member dict if found, None otherwise.
    """
    return TEAM_MEMBERS.get(member_id.lower().strip())


def get_team_member_by_email(email: str) -> dict | None:
    """
    Look up a team member by their email address.

    Args:
        email: The email address to look up.

    Returns:
        Team member dict if found, None otherwise.
    """
    email_lower = email.lower().strip()
    for member in TEAM_MEMBERS.values():
        if member["email"].lower() == email_lower:
            return member
    return None


def get_team_member_names() -> list[str]:
    """
    Get a list of all team member names.

    Returns:
        List of full names (e.g., ["Eyal Zror", "Roye Tadmor", ...])
    """
    return [member["name"] for member in TEAM_MEMBERS.values()]


def is_team_email(email: str) -> bool:
    """
    Check if an email belongs to a CropSight team member.

    Args:
        email: The email address to check.

    Returns:
        True if the email belongs to a team member, False otherwise.
    """
    return email.lower().strip() in [e.lower() for e in CROPSIGHT_TEAM_EMAILS if e]
