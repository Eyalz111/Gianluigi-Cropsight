"""
Executive-context clause builders for the meeting summary (v2.5 Phase 3, chunk 2).

Two surgical, exception-based clauses appended to the EXISTING summary template:
- decision supersession: "(reverses the <Mon DD, YYYY> decision: <short>)"
- topic "where this fits": one line drawn from the live topic brief.

Tier-safe: a clause is omitted whenever the prior item/topic it references is
ABOVE the meeting's distribution tier (a clause must never reveal more than the
summary itself already exposes — the team email reuses the one stored string).

All SYNC (supabase_client is sync — never await). Never raises: a failed fetch
or bad input drops the individual clause, never the summary.
"""

import logging
from datetime import datetime

from models.schemas import TIER_LEVELS

logger = logging.getLogger(__name__)

# Legacy-tolerant tier map — mirrors models/schemas.filter_by_sensitivity.
_LEVEL_MAP = {**TIER_LEVELS, "ceo_only": 4, "restricted": 4, "sensitive": 4, "normal": 3}


def _normalize_level(sensitivity: str | None) -> int:
    """Sensitivity → level (1 public … 4 ceo). Unknown/missing ⇒ founders (3)."""
    return _LEVEL_MAP.get(sensitivity or "founders", 3)


def _fmt_date(raw) -> str:
    """Format a stored date/timestamp as 'Mon DD, YYYY'; fall back to the raw date."""
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return str(raw)[:10]


def _short_desc(desc: str, cap: int = 60) -> str:
    """First sentence of a decision description, capped at `cap` chars + ellipsis."""
    text = (desc or "").strip()
    for sep in (". ", ".\n", "? ", "! "):
        idx = text.find(sep)
        if 0 < idx < len(text):
            text = text[:idx]
            break
    text = text.rstrip(" .").strip()
    if len(text) > cap:
        text = text[:cap].rstrip() + "…"
    return text


def build_supersession_clauses(
    decisions: list[dict],
    supersessions: list[dict],
    meeting_sensitivity: str | None,
) -> dict[int, str]:
    """Map 1-based decision index → '(reverses the <date> decision: <short>)'.

    `supersessions` items are `{new_index (1-based), old_id, reason}` from
    cross_reference. Tier-gated (omit when the parent is above the meeting tier)
    and bounds-guarded (skip an out-of-range new_index). Never raises.
    """
    if not supersessions:
        return {}
    meeting_level = _normalize_level(meeting_sensitivity)
    n = len(decisions or [])

    old_ids = [s.get("old_id") for s in supersessions if s.get("old_id")]
    if not old_ids:
        return {}
    try:
        from services.supabase_client import supabase_client
        parents = supabase_client.get_decisions_by_ids(old_ids)  # one round-trip
    except Exception as e:
        logger.warning(f"supersession clause: parent fetch failed (non-fatal): {e}")
        return {}

    clauses: dict[int, str] = {}
    for s in supersessions:
        try:
            idx = s.get("new_index")
            old_id = s.get("old_id")
            if idx is None or old_id is None:
                continue
            if not (1 <= idx <= n):
                logger.warning(
                    f"supersession new_index {idx} out of range (decisions={n}); skipping"
                )
                continue
            parent = parents.get(old_id)
            if not parent:
                continue
            if _normalize_level(parent.get("sensitivity")) > meeting_level:
                continue  # would reveal a higher-tier prior decision — omit
            short = _short_desc(parent.get("description", ""))
            if not short:
                continue
            date = _fmt_date(parent.get("date"))
            clauses[idx] = (
                f"(reverses the {date} decision: {short})" if date
                else f"(reverses a prior decision: {short})"
            )
        except Exception as e:
            logger.warning(f"supersession clause build skipped one (non-fatal): {e}")
    return clauses


def build_topic_context(
    linked_threads: list[dict],
    meeting_sensitivity: str | None,
) -> str | None:
    r"""One tier-safe '\n**Where this fits:** ...' line from up to 2 topic briefs.

    Deterministic: only topics with a non-empty `brief_json.current_status`,
    ordered by `last_updated` desc, capped at 2. Returns None when nothing safe
    to show. The leading newline places the line directly under the Sensitivity
    header (format_summary renders `{sensitivity}{topic_context}`).
    """
    if not linked_threads:
        return None
    meeting_level = _normalize_level(meeting_sensitivity)
    candidates: list[tuple[str, str, str]] = []  # (last_updated, name, status)
    for t in linked_threads:
        try:
            brief = t.get("brief_json") or {}
            # Topic tier lives INSIDE the brief (brief_json.sensitivity), not a
            # topic_threads column — same field knowledge_readback gates on.
            sens = brief.get("sensitivity")
            if sens is None or _normalize_level(sens) > meeting_level:
                continue  # untiered or above the meeting tier — omit (no leak)
            status = (brief.get("current_status") or "").strip()
            name = (t.get("topic_name") or "").strip()
            if status and name:
                candidates.append((str(t.get("last_updated") or ""), name, status))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)  # last_updated desc, deterministic
    parts = [f"{name} — {status}" for _, name, status in candidates[:2]]
    return "\n**Where this fits:** " + "; ".join(parts)
