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
        "role_description": (
            "Strategy, fundraising, investor relations, overall leadership. "
            "Owns company direction, key partnerships, and board communication."
        ),
        "email": settings.EYAL_EMAIL,
        "is_admin": True,  # Has full access to all features
    },
    "roye": {
        "name": "Roye Tadmor",
        "role": "CTO",
        "role_description": (
            "All technical execution: ML models, data pipeline, cloud infrastructure, "
            "accuracy metrics, platform development. Owns the product roadmap."
        ),
        "email": settings.ROYE_EMAIL,
        "is_admin": False,
    },
    "paolo": {
        "name": "Paolo Vailetti",
        "role": "BD",
        "role_description": (
            "Business development, partnerships, Italy/Europe markets, client outreach. "
            "Owns the Moldova pilot relationship and partner pipeline."
        ),
        "email": settings.PAOLO_EMAIL,
        "is_admin": False,
    },
    "yoram": {
        "name": "Prof. Yoram Weiss",
        "role": "Senior Advisor",
        "role_description": (
            "Academic guidance, agronomy expertise, research methodology. "
            "Advisory role only — does not execute operational tasks. "
            "Assigns tasks TO others, not assigned tasks himself."
        ),
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
# Fall back to TELEGRAM_*_CHAT_ID if *_TELEGRAM_ID is not set
# (in Telegram DMs, user ID == chat ID)
_eyal_tid = settings.EYAL_TELEGRAM_ID or (
    int(settings.TELEGRAM_EYAL_CHAT_ID) if settings.TELEGRAM_EYAL_CHAT_ID else None
)
if _eyal_tid:
    TEAM_TELEGRAM_IDS["eyal"] = _eyal_tid
    TEAM_TELEGRAM_IDS["eyal zror"] = _eyal_tid

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


# =============================================================================
# Email Intelligence — Filter Chain (Phase 4)
# =============================================================================

CROPSIGHT_EMAIL_KEYWORDS_BASELINE = [
    "cropsight", "crop sight", "moldova", "gagauzia", "wheat", "yield",
    "satellite", "iia", "tnufa", "pre-seed", "agtech",
]


def passes_email_filter_chain(
    sender: str,
    recipient: str,
    subject: str,
    tracked_thread_ids: set[str] | None = None,
    thread_id: str | None = None,
    filter_keywords: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Whitelist filter chain for the daily email scan. Any match = passes.

    Rules applied in order:
    1. Blocklist check (rejects immediately)
    2. Sender/recipient is team member
    3. Sender domain matches known stakeholder from entity registry
    4. Subject contains CropSight keywords (live list)
    5. Thread is already being tracked

    Args:
        sender: Sender email address.
        recipient: Recipient email address.
        subject: Email subject line.
        tracked_thread_ids: Set of thread IDs from recent email_scans.
        thread_id: This email's thread ID.
        filter_keywords: Pre-built live keyword list.

    Returns:
        (passes: bool, reason: str)
    """
    sender_lower = sender.lower().strip()
    recipient_lower = recipient.lower().strip()
    subject_lower = subject.lower()

    # Blocklist — reject immediately
    if is_personal_contact_blocked(sender_lower):
        return (False, "blocked_contact")

    # Rule 1: Team member
    if is_team_email(sender_lower) or is_team_email(recipient_lower):
        return (True, "team_member")

    # Rule 2: Known stakeholder domain
    if is_known_stakeholder_domain(sender_lower):
        return (True, "stakeholder_domain")

    # Rule 3: Subject contains keywords
    keywords = filter_keywords or CROPSIGHT_EMAIL_KEYWORDS_BASELINE
    for keyword in keywords:
        if keyword.lower() in subject_lower:
            return (True, f"keyword:{keyword}")

    # Rule 4: Thread already tracked
    if tracked_thread_ids and thread_id and thread_id in tracked_thread_ids:
        return (True, "tracked_thread")

    return (False, "no_match")


def is_known_stakeholder_domain(email: str) -> bool:
    """
    Check sender domain against entity registry organizations.

    Extracts domain from email and checks if any organization entity
    has a matching domain in its aliases or name.
    """
    if "@" not in email:
        return False

    domain = email.split("@")[1].lower()
    # Skip generic providers
    generic_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "live.com", "icloud.com", "mail.com", "protonmail.com",
    }
    if domain in generic_domains:
        return False

    try:
        from services.supabase_client import supabase_client
        entities = supabase_client.list_entities(entity_type="organization", limit=100)
        for entity in entities:
            name = entity.get("canonical_name", "").lower()
            # Check if domain contains organization name or vice versa
            domain_base = domain.split(".")[0]
            if domain_base and len(domain_base) > 2:
                if domain_base in name or name in domain_base:
                    return True
            # Check aliases
            for alias in entity.get("aliases", []) or []:
                alias_lower = alias.lower()
                if domain_base in alias_lower or alias_lower in domain_base:
                    return True
    except Exception:
        pass

    return False


def is_personal_contact_blocked(email: str) -> bool:
    """Check against PERSONAL_CONTACTS_BLOCKLIST from settings."""
    blocklist = settings.personal_contacts_blocklist_list
    return email.lower().strip() in [e.lower() for e in blocklist]
