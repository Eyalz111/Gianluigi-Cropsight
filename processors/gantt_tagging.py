"""
Gantt onboarding tagging (v3 chunk 2 — curated knowledge-view).

Propose a mapping of existing Gantt rows -> topics (by area, owner-prefix, and
label similarity), surfaced for Eyal's approval — NEVER auto-applied. On approval,
write the topic-id tag (services/gantt_rows) + upsert the gantt_rows record. The
system never creates or moves sheet rows (curated, not generated).
"""

import logging
import re

from services.gantt_rows import write_row_tag
from services.supabase_client import supabase_client
from guardrails.gantt_guard import _load_schema

logger = logging.getLogger(__name__)

_OWNER_RE = re.compile(r"^\s*\[([A-Za-z/]+)\]")
# Lower than the tasks dedup threshold (0.60): Gantt labels are short + owner-prefixed.
_TAG_THRESHOLD = 0.40
_AREA_BONUS = 0.20


def _tokens(s: str | None) -> set:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _overlap(a: str | None, b: str | None) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _owner_prefix(text: str | None) -> str | None:
    m = _OWNER_RE.match(text or "")
    return f"[{m.group(1).upper()}]" if m else None


def _load_topics() -> list[dict]:
    try:
        return (
            supabase_client.client.table("topic_threads")
            .select("id,topic_name,area_id")
            .is_("valid_to", "null")
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.error(f"load topics failed: {e}")
        return []


async def propose_row_tags(sheet_name: str) -> dict:
    """Build a row->topic mapping proposal (NOT applied); store as a pending approval."""
    schema = [
        r for r in _load_schema()
        if (r.get("sheet_name", "").lower() == sheet_name.lower()
            and not r.get("protected") and r.get("subsection"))
    ]
    areas = supabase_client.get_areas()
    topics = _load_topics()

    candidates = []
    for row in schema:
        section, subsection, rn = row.get("section", ""), row.get("subsection", ""), row.get("row_number")
        # area match: section <-> areas.gantt_section (or name)
        area_ids = {
            a["id"] for a in areas
            if _overlap(a.get("gantt_section") or a.get("name"), section) >= 0.4
        }
        best = None  # (topic, score)
        for t in topics:
            score = _overlap(t.get("topic_name"), subsection)
            if t.get("area_id") in area_ids:
                score += _AREA_BONUS
            if best is None or score > best[1]:
                best = (t, score)
        owner = _owner_prefix(subsection)
        if best and best[1] >= _TAG_THRESHOLD:
            t = best[0]
            candidates.append({
                "row": rn, "section": section, "subsection": subsection, "owner": owner,
                "topic_id": t["id"], "topic_name": t.get("topic_name"),
                "area_id": t.get("area_id"), "score": round(best[1], 2),
            })
        else:
            candidates.append({
                "row": rn, "section": section, "subsection": subsection, "owner": owner,
                "topic_id": None, "topic_name": None, "needs_tagging": True,
            })

    proposal_id = f"gtag-{sheet_name}"
    matched = sum(1 for c in candidates if c.get("topic_id"))
    try:
        supabase_client.upsert_pending_approval(
            approval_id=proposal_id,
            content_type="gantt_tag_mapping",
            content={"sheet_name": sheet_name, "candidates": candidates},
        )
    except Exception as e:
        logger.error(f"create gantt_tag_mapping proposal failed: {e}")
    return {"proposal_id": proposal_id, "rows": len(candidates), "matched": matched,
            "needs_tagging": len(candidates) - matched}


async def apply_row_tags(sheet_name: str, mapping: list[dict]) -> dict:
    """Apply an APPROVED (possibly edited) row->topic mapping: tag + upsert gantt_rows."""
    applied = 0
    for m in mapping:
        tid, rn = m.get("topic_id"), m.get("row")
        if not tid or not rn:
            continue
        if await write_row_tag(sheet_name, rn, tid):
            supabase_client.upsert_gantt_row({
                "sheet_name": sheet_name, "topic_id": tid, "area_id": m.get("area_id"),
                "owner": m.get("owner"), "display_order": rn,
            })
            applied += 1
    logger.info(f"[gantt_tagging] applied {applied} tags on {sheet_name}")
    return {"applied": applied}
