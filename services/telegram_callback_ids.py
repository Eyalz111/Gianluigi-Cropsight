"""
Telegram inline-button callback_data: build it so it always fits, and resolve it
back on the way in.

WHY THIS EXISTS
Telegram hard-caps callback_data at 64 BYTES. Exceed it and the API rejects the
ENTIRE sendMessage with "Button_data_invalid" — the card never arrives, so the
item sits in the approval queue with no way to action it and only a one-line log
to show for it. Approval ids are deterministic and carry idempotency meaning
(`decprop-{old}-{new}`, `outline-{event_id}`, `prep-{event_id}`), so they must
NOT be shortened at rest; only the button payload is truncated.

Lives in its own module because BOTH services/telegram_bot.py and
guardrails/approval_flow.py build buttons, and approval_flow cannot import
telegram_bot (telegram_bot imports approval_flow — that would be a cycle).

TRUNCATION KEEPS BOTH ENDS. A plain prefix cut is not enough: recurring Google
Calendar instances share a long base id and differ only in a trailing
`_20260805T090000Z`, so a prefix-truncated id collides across every instance of
the series and the LIKE lookup goes ambiguous — bricking every button on both
cards. Keeping a head AND a tail preserves the discriminator, and the lookup
becomes `LIKE head%tail`.
"""

import logging

logger = logging.getLogger(__name__)

MAX_BYTES = 64

# Marks a truncated identifier. Deliberately a character that does not occur in
# the id formats we mint (uuids, base64url calendar ids, `outline-`/`prep-`/
# `decprop-` prefixes).
ELLIPSIS = "~"

# Actions whose payload carries more than the identifier, and WHERE the id sits.
# `sens_set` puts the band first and the id last; `prep_settype` is the reverse.
# Anything not listed here is treated as id-only — importantly this means we do
# NOT blindly split on ':', which would corrupt ids that legitimately contain one
# (approval_flow mints `stakeholder:{org_key}`).
ID_FIRST = "id_first"    # <action>:<id>:<extra>
ID_LAST = "id_last"      # <action>:<extra>:<id>
COMPOSITE_ACTIONS = {
    "prep_settype": ID_FIRST,
    "sens_set": ID_LAST,
}


def build_callback_data(action: str, ident: str, extra: str = "") -> str:
    """Build callback_data for `action` that always fits inside MAX_BYTES.

    `extra` is the non-identifier segment (band, chosen type). It is never
    truncated — it carries the user's actual choice, and cutting it would apply
    the wrong one silently. Only `ident` is shortened, keeping head and tail.
    """
    layout = COMPOSITE_ACTIONS.get(action)
    if extra and layout == ID_LAST:
        prefix, suffix = f"{action}:{extra}:", ""
    elif extra:
        prefix, suffix = f"{action}:", f":{extra}"
    else:
        prefix, suffix = f"{action}:", ""

    budget = MAX_BYTES - len(prefix.encode()) - len(suffix.encode())
    if budget <= 0:
        logger.error(
            f"callback action {action!r} (+extra {extra!r}) leaves no room for an id"
        )
        return (prefix + suffix).encode()[:MAX_BYTES].decode("utf-8", "ignore")

    raw = ident.encode()
    if len(raw) <= budget:
        return prefix + ident + suffix

    # Split the budget head/tail around the ellipsis so the trailing
    # discriminator (recurring-instance timestamp, second uuid) survives.
    inner = budget - len(ELLIPSIS.encode())
    if inner <= 1:
        return prefix + raw[:budget].decode("utf-8", "ignore") + suffix
    head_len = (inner + 1) // 2
    tail_len = inner - head_len
    head = raw[:head_len].decode("utf-8", "ignore")
    tail = raw[len(raw) - tail_len:].decode("utf-8", "ignore") if tail_len else ""
    logger.info(
        f"callback_data for {action!r} truncated {len(raw)}B -> fits {MAX_BYTES}B "
        f"(id {ident!r}); resolved by head/tail match on callback"
    )
    return prefix + head + ELLIPSIS + tail + suffix


def split_payload(action: str, payload: str) -> tuple[str, str, str]:
    """Split a dispatched payload into (before, ident, after).

    Only splits for actions declared in COMPOSITE_ACTIONS — everything else is
    id-only, so an id containing ':' stays intact.
    """
    layout = COMPOSITE_ACTIONS.get(action)
    if layout == ID_FIRST:
        ident, sep, after = payload.partition(":")
        return "", ident, after if sep else ""
    if layout == ID_LAST:
        before, sep, ident = payload.partition(":")
        return (before, ident, "") if sep else ("", payload, "")
    return "", payload, ""


def rejoin(before: str, ident: str, after: str) -> str:
    """Reassemble a payload after expanding its identifier."""
    parts = [p for p in (before, ident) if p != ""] if before else [ident]
    joined = ":".join(parts)
    return f"{joined}:{after}" if after else joined


def expand_ident(ident: str) -> str:
    """Resolve a possibly-truncated identifier to the full approval id.

    Returns `ident` unchanged when it was not truncated, when nothing matches, or
    when the match is ambiguous — the caller's own lookup then fails exactly as it
    would have, rather than us actioning the WRONG approval.
    """
    if not ident or ELLIPSIS not in ident:
        return ident

    head, _, tail = ident.partition(ELLIPSIS)
    try:
        from services.supabase_client import supabase_client

        def _esc(s: str) -> str:
            # Escape LIKE wildcards so an id containing them matches only itself.
            # '*' matters too: PostgREST translates '*' into '%' server-side.
            return (s.replace("\\", "\\\\").replace("%", "\\%")
                     .replace("_", "\\_").replace("*", "\\*"))

        pattern = f"{_esc(head)}%{_esc(tail)}"
        rows = (
            supabase_client.client.table("pending_approvals")
            .select("approval_id")
            .like("approval_id", pattern)
            .limit(5)
            .execute()
            .data
        ) or []
        if len(rows) == 1:
            full = rows[0]["approval_id"]
            logger.info(f"Expanded truncated callback id {ident!r} -> {full!r}")
            return full
        if len(rows) > 1:
            logger.warning(
                f"Ambiguous truncated callback id {ident!r} matches {len(rows)} "
                f"approvals — refusing to guess"
            )
        else:
            logger.warning(f"No approval matches truncated callback id {ident!r}")
    except Exception as e:
        logger.warning(f"callback id expansion failed for {ident!r} (non-fatal): {e}")
    return ident


def expand_payload(action: str, payload: str) -> str:
    """Expand the identifier inside a dispatched payload, preserving the rest."""
    before, ident, after = split_payload(action, payload)
    if ELLIPSIS not in ident:
        return payload
    return rejoin(before, expand_ident(ident), after)
