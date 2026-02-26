"""
Multi-layer inbound message guardrail system.

Implements five layers of protection for all inbound messages:

1. Sender Verification — is this a known CropSight team member?
2. Topic Relevance — is the message about CropSight work?
3. Information Leak Prevention — scan outbound responses for sensitive data
4. Output Sanitization — clean outbound messages before sending
5. Audit Logging — log every interaction for accountability

Usage:
    from guardrails.inbound_filter import check_inbound_message

    result = await check_inbound_message(
        message="What's the status of the Moldova pilot?",
        sender_id="eyal",
        channel="telegram_dm",
        telegram_user_id=8190904141,
    )

    if result["allowed"]:
        # Process the message
        ...
    else:
        # Send result["deflection_message"] back to the sender
        ...
"""

import logging
import re
from typing import Any

from config.team import TEAM_TELEGRAM_IDS, TEAM_MEMBERS, is_team_email
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


# =============================================================================
# Deflection Messages
# =============================================================================

UNKNOWN_SENDER_DEFLECTION = (
    "I'm Gianluigi, CropSight's internal assistant. "
    "I only respond to CropSight team members."
)

OFF_TOPIC_DEFLECTION = (
    "I can only help with CropSight-related work topics."
)

SENSITIVE_REDACTION = "[Sensitive — contact Eyal]"


# =============================================================================
# Layer 2 — Topic Relevance Patterns
# =============================================================================

# Regex patterns for clearly off-topic content
OFF_TOPIC_PATTERNS = [
    r"\b(tell me a joke|joke)\b",
    r"\b(write (me )?a poem|poem)\b",
    r"\b(recipe|cook(ing)?|bake|baking)\b",
    r"\b(personal advice|relationship advice|dating)\b",
    r"\b(weather forecast|what's the weather)\b",
    r"\b(sports score|football|basketball|soccer score|world cup)\b",
    r"\b(trivia|fun fact|riddle)\b",
]

# Regex patterns that indicate work-related CropSight content
WORK_INDICATORS = [
    r"\b(cropsight|crop\s?sight)\b",
    r"\b(meeting|meetings)\b",
    r"\b(task|tasks)\b",
    r"\b(deadline|deadlines)\b",
    r"\b(investor|investors|investment)\b",
    r"\b(moldova|moldov)\b",
    r"\b(satellite|satellites|imagery)\b",
    r"\b(crop|crops|agriculture|agri)\b",
    r"\b(client|clients|customer)\b",
    r"\b(budget|budgets|cost)\b",
    r"\b(sprint|sprints)\b",
    r"\b(stakeholder|stakeholders)\b",
    r"\b(agenda|agendas)\b",
    r"\b(decision|decisions)\b",
    r"\b(follow[\s-]?up|followup)\b",
    r"\b(summary|summaries|recap)\b",
    r"\b(transcript|transcripts)\b",
    r"\b(pilot|pilots|demo)\b",
    r"\b(product|roadmap|feature)\b",
    r"\b(team|eyal|roye|paolo|yoram)\b",
    r"\b(email|calendar|schedule)\b",
    r"\b(update|status|progress)\b",
    r"\b(action item|action items)\b",
    r"\b(prep|preparation|brief)\b",
    r"\b(weekly|digest|report)\b",
]


# =============================================================================
# Layer 3 — Sensitive Patterns for Leak Prevention
# =============================================================================

SENSITIVE_PATTERNS = [
    r"\b(founders?\s*agreement)\b",
    r"\b(equity\s*split)\b",
    r"\b(salary|salaries|compensation\s*package)\b",
    r"\b(NDA|non[\s-]?disclosure)\b",
    r"\b(API[\s_]?key|api[\s_]?secret)\b",
    r"\b(bank\s*account|IBAN|routing\s*number)\b",
    r"\b(valuation)\b",
    r"\b(term\s*sheet)\b",
    r"\b(cap\s*table)\b",
    r"\b(option\s*pool)\b",
    r"\b(runway)\b",
    r"\b(burn\s*rate)\b",
    r"\b(seed\s*round)\b",
    r"\b(series\s*[A-D])\b",
]


# =============================================================================
# Layer 1 — Sender Verification
# =============================================================================

def verify_sender_telegram(user_id: int) -> dict:
    """
    Verify a Telegram user against the CropSight team whitelist.

    Checks the user_id against TEAM_TELEGRAM_IDS from config/team.py.
    The dict maps member names to their Telegram IDs, so we reverse-look
    up the member_id from the Telegram user_id.

    Args:
        user_id: Telegram user/chat ID to verify.

    Returns:
        {"verified": True, "member_id": "eyal"} if known, or
        {"verified": False, "member_id": None} if unknown.
    """
    # TEAM_TELEGRAM_IDS maps names like "eyal", "eyal zror" -> int
    # We want to find the short member_id (e.g., "eyal", "roye")
    for name, tid in TEAM_TELEGRAM_IDS.items():
        if tid == user_id:
            # Prefer the short name (no space) as the member_id
            if " " not in name:
                return {"verified": True, "member_id": name}

    # Second pass: if we only matched a full name, still return it
    for name, tid in TEAM_TELEGRAM_IDS.items():
        if tid == user_id:
            # Extract first name as member_id
            member_id = name.split()[0].lower()
            return {"verified": True, "member_id": member_id}

    logger.info(f"Unknown Telegram user_id: {user_id}")
    return {"verified": False, "member_id": None}


def verify_sender_email(email: str) -> dict:
    """
    Verify an email sender against the CropSight team whitelist.

    Uses is_team_email() from config/team.py to check membership,
    then looks up the member_id by matching the email in TEAM_MEMBERS.

    Args:
        email: Email address to verify.

    Returns:
        {"verified": True, "member_id": "eyal"} if known, or
        {"verified": False, "member_id": None} if unknown.
    """
    if not email:
        return {"verified": False, "member_id": None}

    if is_team_email(email):
        # Find the member_id by matching email in TEAM_MEMBERS
        email_lower = email.lower().strip()
        for member_id, info in TEAM_MEMBERS.items():
            if info["email"] and info["email"].lower() == email_lower:
                return {"verified": True, "member_id": member_id}

        # Email is in whitelist but no member_id match (shouldn't happen)
        logger.warning(f"Email {email} passed is_team_email but no TEAM_MEMBERS match")
        return {"verified": True, "member_id": None}

    logger.info(f"Unknown email sender: {email}")
    return {"verified": False, "member_id": None}


# =============================================================================
# Layer 2 — Topic Relevance
# =============================================================================

def check_topic_relevance(message: str) -> dict:
    """
    Check whether an inbound message is related to CropSight work.

    Three-way classification:
    - True: message matches work-related patterns
    - False: message matches off-topic patterns (jokes, recipes, etc.)
    - "uncertain": no strong signal either way (still allowed through)

    Args:
        message: The inbound message text to check.

    Returns:
        {"relevant": True/False/"uncertain", "reason": "explanation"}
    """
    if not message or not message.strip():
        return {"relevant": "uncertain", "reason": "Empty message"}

    message_lower = message.lower()

    # Check off-topic patterns first
    for pattern in OFF_TOPIC_PATTERNS:
        if re.search(pattern, message_lower):
            logger.info(f"Off-topic message detected: matched pattern '{pattern}'")
            return {
                "relevant": False,
                "reason": f"Off-topic: matched '{pattern}'",
            }

    # Check work indicators
    for pattern in WORK_INDICATORS:
        if re.search(pattern, message_lower):
            return {
                "relevant": True,
                "reason": f"Work-related: matched '{pattern}'",
            }

    # No strong signal — allow through but flag as uncertain
    return {
        "relevant": "uncertain",
        "reason": "No strong topic signal detected",
    }


# =============================================================================
# Layer 3 — Information Leak Prevention
# =============================================================================

def check_response_for_leaks(response: str, context: dict) -> dict:
    """
    Scan an OUTBOUND response for sensitive information leaks.

    Sensitive content (equity splits, salaries, term sheets, etc.) is
    only allowed in DMs to Eyal. In group chats or emails, sensitive
    text is replaced with a redaction marker.

    Args:
        response: The outbound response text to scan.
        context: Dict with keys:
            - channel: "telegram_dm", "telegram_group", or "email"
            - recipient: Name or email of the recipient

    Returns:
        {
            "leaked": True/False,
            "sanitized_response": str (with redactions if needed),
            "patterns_found": list of matched pattern descriptions,
        }
    """
    if not response:
        return {
            "leaked": False,
            "sanitized_response": response,
            "patterns_found": [],
        }

    channel = context.get("channel", "")
    recipient = context.get("recipient", "")

    # DMs to Eyal are unrestricted — sensitive content passes through
    is_eyal_dm = (
        channel == "telegram_dm"
        and str(recipient).lower() in ("eyal", "eyal zror")
    )

    if is_eyal_dm:
        return {
            "leaked": False,
            "sanitized_response": response,
            "patterns_found": [],
        }

    # Scan for sensitive patterns
    patterns_found = []
    sanitized = response

    for pattern in SENSITIVE_PATTERNS:
        matches = list(re.finditer(pattern, sanitized, re.IGNORECASE))
        if matches:
            for match in reversed(matches):
                # Log what was found
                patterns_found.append(match.group())
                # Replace the matched text with redaction marker
                sanitized = (
                    sanitized[:match.start()]
                    + SENSITIVE_REDACTION
                    + sanitized[match.end():]
                )

    leaked = len(patterns_found) > 0

    if leaked:
        logger.warning(
            f"Sensitive content detected in {channel} "
            f"(recipient: {recipient}): {patterns_found}"
        )

    return {
        "leaked": leaked,
        "sanitized_response": sanitized,
        "patterns_found": patterns_found,
    }


# =============================================================================
# Layer 4 — Output Sanitization
# =============================================================================

def sanitize_outbound_message(message: str, context: dict) -> str:
    """
    Sanitize an outbound message by running leak checks and basic cleanup.

    This is the main function to call before sending any response.
    It runs check_response_for_leaks() internally and returns the
    cleaned message string.

    Args:
        message: The outbound message to sanitize.
        context: Dict with channel and recipient info (same as Layer 3).

    Returns:
        The sanitized message string, ready to send.
    """
    if not message:
        return message

    # Run leak prevention
    leak_result = check_response_for_leaks(message, context)
    sanitized = leak_result["sanitized_response"]

    if leak_result["leaked"]:
        logger.info(
            f"Sanitized {len(leak_result['patterns_found'])} sensitive "
            f"pattern(s) from outbound message"
        )

    return sanitized


# =============================================================================
# Layer 5 — Audit Logging
# =============================================================================

def log_inbound_interaction(
    sender: str,
    channel: str,
    preview: str,
    verified: bool,
    relevant: str,
    action: str,
) -> None:
    """
    Log an inbound interaction to the audit trail via Supabase.

    Uses supabase_client.log_action() which is SYNC (never await).

    Args:
        sender: Sender identifier (member_id, email, or user_id).
        channel: "telegram_dm", "telegram_group", or "email".
        preview: First ~100 chars of the message for context.
        verified: Whether the sender was verified.
        relevant: Topic relevance result ("True", "False", "uncertain").
        action: What happened ("allowed", "deflected_unknown_sender",
                "deflected_off_topic", "uncertain_relevance").
    """
    try:
        # supabase_client.log_action() is SYNC — do not await
        supabase_client.log_action(
            action="inbound_interaction",
            details={
                "sender": sender,
                "channel": channel,
                "message_preview": preview[:100],
                "verified": verified,
                "relevant": relevant,
                "outcome": action,
            },
            triggered_by=sender if verified else "unknown",
        )
    except Exception as e:
        # Logging should never break the main flow
        logger.error(f"Failed to log inbound interaction: {e}")


# =============================================================================
# Main Entry Point
# =============================================================================

async def check_inbound_message(
    message: str,
    sender_id: str,
    channel: str,
    telegram_user_id: int | None = None,
    sender_email: str | None = None,
) -> dict:
    """
    Run all inbound checks on an incoming message.

    This is the main entry point for the inbound guardrail system.
    It runs through all five layers in order:

    1. Verify sender identity (Telegram ID or email)
    2. If not verified -> deflection
    3. Check topic relevance
    4. If off-topic -> deflection
    5. If uncertain -> allowed but flagged
    6. Log the interaction
    7. Return allowed=True

    Args:
        message: The inbound message text.
        sender_id: Identifier for the sender (e.g., "eyal", "user123").
        channel: "telegram_dm", "telegram_group", or "email".
        telegram_user_id: Telegram user ID (if channel is telegram_*).
        sender_email: Email address (if channel is email).

    Returns:
        {
            "allowed": True/False,
            "deflection_message": str or None,
            "member_id": str or None,
            "audit_logged": True,
        }
    """
    # Truncate message for log previews
    preview = message[:100] if message else ""

    # -----------------------------------------------------------------
    # Step 1: Verify sender
    # -----------------------------------------------------------------
    verification = {"verified": False, "member_id": None}

    if telegram_user_id is not None:
        verification = verify_sender_telegram(telegram_user_id)
    elif sender_email:
        verification = verify_sender_email(sender_email)

    member_id = verification["member_id"]

    # -----------------------------------------------------------------
    # Step 2: If not verified, deflect
    # -----------------------------------------------------------------
    if not verification["verified"]:
        logger.info(
            f"Deflecting unknown sender: sender_id={sender_id}, "
            f"channel={channel}"
        )
        log_inbound_interaction(
            sender=sender_id,
            channel=channel,
            preview=preview,
            verified=False,
            relevant="unknown",
            action="deflected_unknown_sender",
        )
        return {
            "allowed": False,
            "deflection_message": UNKNOWN_SENDER_DEFLECTION,
            "member_id": None,
            "audit_logged": True,
        }

    # -----------------------------------------------------------------
    # Step 3: Check topic relevance
    # -----------------------------------------------------------------
    relevance = check_topic_relevance(message)

    # -----------------------------------------------------------------
    # Step 4: If off-topic, deflect
    # -----------------------------------------------------------------
    if relevance["relevant"] is False:
        logger.info(
            f"Deflecting off-topic message from {member_id}: "
            f"{relevance['reason']}"
        )
        log_inbound_interaction(
            sender=member_id or sender_id,
            channel=channel,
            preview=preview,
            verified=True,
            relevant="False",
            action="deflected_off_topic",
        )
        return {
            "allowed": False,
            "deflection_message": OFF_TOPIC_DEFLECTION,
            "member_id": member_id,
            "audit_logged": True,
        }

    # -----------------------------------------------------------------
    # Step 5: If uncertain, allow but flag
    # -----------------------------------------------------------------
    if relevance["relevant"] == "uncertain":
        logger.info(
            f"Uncertain relevance from {member_id}: {relevance['reason']}"
        )
        log_inbound_interaction(
            sender=member_id or sender_id,
            channel=channel,
            preview=preview,
            verified=True,
            relevant="uncertain",
            action="uncertain_relevance",
        )
        return {
            "allowed": True,
            "deflection_message": None,
            "member_id": member_id,
            "audit_logged": True,
        }

    # -----------------------------------------------------------------
    # Step 6: Log the allowed interaction
    # -----------------------------------------------------------------
    log_inbound_interaction(
        sender=member_id or sender_id,
        channel=channel,
        preview=preview,
        verified=True,
        relevant="True",
        action="allowed",
    )

    # -----------------------------------------------------------------
    # Step 7: Return allowed
    # -----------------------------------------------------------------
    return {
        "allowed": True,
        "deflection_message": None,
        "member_id": member_id,
        "audit_logged": True,
    }
