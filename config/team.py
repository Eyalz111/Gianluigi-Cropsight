"""
Team configuration for CropSight.

Contains team member information, email whitelists, calendar filters,
and blocklists used throughout the application.

This is the single source of truth for team-related configuration.
"""

import logging
import re

from config.settings import settings

logger = logging.getLogger(__name__)


# =============================================================================
# CropSight Team Members
# =============================================================================
#
# Each member carries:
#   - "email": their primary address (the historical single-email field; many
#     call sites read it, so it stays untouched).
#   - "identities": every address that is the SAME person (personal + work).
#     Pre-Workspace the team uses personal gmails; as work addresses come online
#     (e.g. Eyal's @cropsight.io) they are added here so calendar/email
#     recognition resolves them to the right person. Additive only.

_HARDCODED_TEAM_MEMBERS = {
    "eyal": {
        "name": "Eyal Zror",
        "role": "CEO",
        "role_description": (
            "Strategy, fundraising, investor relations, overall leadership. "
            "Owns company direction, key partnerships, and board communication."
        ),
        "email": settings.EYAL_EMAIL,
        "identities": [settings.EYAL_EMAIL, "eyal.zror@cropsight.io"],
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
        "identities": [settings.ROYE_EMAIL],
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
        "identities": [settings.PAOLO_EMAIL],
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
        "identities": [settings.YORAM_EMAIL],
        "is_admin": False,
    },
}


def _load_team_members() -> dict:
    """Roster source: the team_members DB table when TEAM_ROSTER_DB_ENABLED, else
    the hardcoded literal. Falls back to the hardcoded literal on ANY error or an
    empty result, so the roster can never come back empty. Built once at import."""
    if not getattr(settings, "TEAM_ROSTER_DB_ENABLED", False):
        return _HARDCODED_TEAM_MEMBERS
    try:
        from services.supabase_client import supabase_client

        rows = supabase_client.list_team_members(status="active")
        out: dict = {}
        for r in rows or []:
            key = (r.get("member_key") or "").lower().strip()
            if not key:
                continue
            email = r.get("primary_email") or ""
            idents = [i for i in (r.get("identities") or []) if i] or (
                [email] if email else []
            )
            out[key] = {
                "name": r.get("name", ""),
                "role": r.get("role", ""),
                "role_description": r.get("role_description", ""),
                "email": email,
                "identities": idents,
                "is_admin": bool(r.get("is_admin", False)),
                "tier": r.get("tier", "founders"),
                "telegram_id": r.get("telegram_id"),
            }
        return out or _HARDCODED_TEAM_MEMBERS
    except Exception as e:
        logger.warning(f"[team] DB roster load failed; using hardcoded roster: {e}")
        return _HARDCODED_TEAM_MEMBERS


def refresh_team_roster() -> None:
    """Re-load the module-level roster + derived constants from the current source.
    A seam for a future 'add teammate' admin/MCP path (otherwise the roster is
    built once at import, so a flag flip needs a restart)."""
    global TEAM_MEMBERS, CROPSIGHT_TEAM_EMAILS, CROPSIGHT_WORK_IDENTITIES, TEAM_TELEGRAM_IDS
    TEAM_MEMBERS = _load_team_members()
    CROPSIGHT_TEAM_EMAILS = [m.get("email", "") for m in TEAM_MEMBERS.values()]
    CROPSIGHT_WORK_IDENTITIES = {
        ident.lower().strip()
        for member in TEAM_MEMBERS.values()
        for ident in member.get("identities", [])
        if ident
    }
    TEAM_TELEGRAM_IDS = _build_telegram_ids(TEAM_MEMBERS)


TEAM_MEMBERS = _load_team_members()


# =============================================================================
# Email Whitelist for Calendar Filtering
# =============================================================================

# Derived from the (possibly DB-backed) roster — byte-identical to the historical
# [EYAL, ROYE, PAOLO, YORAM] list when the roster is the hardcoded default.
CROPSIGHT_TEAM_EMAILS = [m.get("email", "") for m in TEAM_MEMBERS.values()]


# =============================================================================
# Business Identity (CropSight work domains + every team member identity)
# =============================================================================

# Domains that unambiguously mean "CropSight business" (org email).
# Pre-Workspace the team still uses personal gmails — those are recognized via
# CROPSIGHT_WORK_IDENTITIES below, NOT by domain, so a personal-gmail event
# does not get auto-classified as business.
CROPSIGHT_BUSINESS_DOMAINS = ["cropsight.io", "cropsight.com"]

# Flat, lowercased set of every registered team identity (personal + work).
# Superset of CROPSIGHT_TEAM_EMAILS (which is only the primary addresses).
CROPSIGHT_WORK_IDENTITIES = {
    ident.lower().strip()
    for member in TEAM_MEMBERS.values()
    for ident in member.get("identities", [])
    if ident
}


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

# Mapping of team member keys/names to their Telegram chat IDs. When the roster
# is DB-backed, IDs come from the team_members rows; otherwise from the per-person
# settings env vars (byte-identical to the historical block).
def _build_telegram_ids(members: dict) -> dict[str, int]:
    ids: dict[str, int] = {}
    # Eyal keeps the historical TELEGRAM_EYAL_CHAT_ID fallback in both modes.
    _eyal_tid = settings.EYAL_TELEGRAM_ID or (
        int(settings.TELEGRAM_EYAL_CHAT_ID) if settings.TELEGRAM_EYAL_CHAT_ID else None
    )
    if getattr(settings, "TEAM_ROSTER_DB_ENABLED", False):
        for key, m in members.items():
            tid = m.get("telegram_id")
            if key == "eyal" and not tid:
                tid = _eyal_tid
            if tid:
                ids[key] = int(tid)
                if m.get("name"):
                    ids[m["name"].lower()] = int(tid)
        return ids
    # Hardcoded mode — byte-identical to the historical settings-based block.
    if _eyal_tid:
        ids["eyal"] = _eyal_tid
        ids["eyal zror"] = _eyal_tid
    if settings.ROYE_TELEGRAM_ID:
        ids["roye"] = settings.ROYE_TELEGRAM_ID
        ids["roye tadmor"] = settings.ROYE_TELEGRAM_ID
    if settings.PAOLO_TELEGRAM_ID:
        ids["paolo"] = settings.PAOLO_TELEGRAM_ID
        ids["paolo vailetti"] = settings.PAOLO_TELEGRAM_ID
    if settings.YORAM_TELEGRAM_ID:
        ids["yoram"] = settings.YORAM_TELEGRAM_ID
        ids["yoram weiss"] = settings.YORAM_TELEGRAM_ID
    return ids


TEAM_TELEGRAM_IDS: dict[str, int] = _build_telegram_ids(TEAM_MEMBERS)


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


def _normalize_email(email: str) -> str:
    """
    Extract a bare, lowercased email address from a possibly-formatted string.

    Handles both "addr@x.com" and "Display Name <addr@x.com>" forms.

    Args:
        email: Raw email string (may include a display name).

    Returns:
        Lowercased bare address, or "" if none could be extracted.
    """
    if not email:
        return ""
    match = re.search(r"[\w.+-]+@[\w.-]+", email)
    return (match.group(0) if match else email).lower().strip()


def is_business_identity(email: str) -> bool:
    """
    Check if an email is on a CropSight business domain (@cropsight.io / .com).

    This is the calendar/email *business signal*. It deliberately does NOT
    include the team's personal gmails: a personal-gmail attendee must not
    auto-classify an event as CropSight (that was the old "2+ team members"
    leak). Personal team identities are recognized separately for email-routing
    purposes via is_team_email().

    Args:
        email: The email address (or "Name <addr>" form) to check.

    Returns:
        True if the email's domain is a CropSight business domain.
    """
    addr = _normalize_email(email)
    if not addr or "@" not in addr:
        return False
    domain = addr.split("@", 1)[1]
    return domain in CROPSIGHT_BUSINESS_DOMAINS


def get_team_member_by_email(email: str) -> dict | None:
    """
    Look up a team member by any of their identities (primary or work).

    Args:
        email: The email address to look up.

    Returns:
        Team member dict if found, None otherwise.
    """
    addr = _normalize_email(email)
    if not addr:
        return None
    for member in TEAM_MEMBERS.values():
        if member["email"].lower().strip() == addr:
            return member
        for ident in member.get("identities", []) or []:
            if ident and ident.lower().strip() == addr:
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

    Matches any registered identity (personal or work), so a work address such
    as eyal.zror@cropsight.io now resolves as a team email. Superset of the
    historical primary-email check — never narrows it.

    Args:
        email: The email address to check.

    Returns:
        True if the email belongs to a team member, False otherwise.
    """
    addr = _normalize_email(email)
    if not addr:
        return False
    if addr in CROPSIGHT_WORK_IDENTITIES:
        return True
    return addr in [e.lower() for e in CROPSIGHT_TEAM_EMAILS if e]


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
    2. Sender/recipient is a team member (any registered identity)
    3. Sender/recipient is on a CropSight business domain (org mail not yet
       registered as a person)
    4. Sender domain matches known stakeholder from entity registry
    5. Subject contains CropSight keywords (live list)
    6. Thread is already being tracked

    The chain stays recall-friendly (a keyword match still passes, so cold
    first-contact business inbound is not dropped); personal-vs-business
    precision is the classifier's job (see processors/email_classifier.py).

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

    # Rule 1: Team member (any registered identity, incl. personal gmails) —
    # person-level attribution for known people.
    if is_team_email(sender_lower) or is_team_email(recipient_lower):
        return (True, "team_member")

    # Rule 2: CropSight business domain (@cropsight.io / .com) — unambiguous org
    # mail, recognized even for addresses not yet registered as a team identity.
    if is_business_identity(sender_lower) or is_business_identity(recipient_lower):
        return (True, "business_domain")

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
