"""
PR-E: weekly Gantt nudges (brief ↔ board divergence). DB-only, never writes the board.

For each lane linked to topics (gantt_covers), compares the topics' brief
intelligence (current_status / recent_decisions / open_items) against the board
view (gantt_rows). Emits a short, ranked, deduped list of nudges into
pending_approvals (content_type 'gantt_nudge'); Eyal acts, the system never edits
the board.

Pinned: severity 1-3 (surface ≥2); global cap 5/cycle; per-(lane,kind) dedupe vs
last cycle unless the board changed. CEO-tier topics suppressed (team-visible Gantt).
"""

import logging

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_CAP = 5
_SURFACE_MIN = 2  # severity threshold


def _topic_status(t):
    for s in (t.get("brief_json"), t.get("state_json")):
        if isinstance(s, dict) and s.get("current_status"):
            return str(s["current_status"]).lower()
    return "unknown"


def _is_ceo_only(t):
    for s in (t.get("brief_json"), t.get("state_json")):
        if isinstance(s, dict) and (s.get("sensitivity") or "").lower() == "ceo":
            return True
    return (t.get("sensitivity") or "").lower() == "ceo"


def _existing_nudge_keys() -> set:
    """(lane, kind) keys already pending — for dedupe vs last cycle."""
    try:
        rows = supabase_client.get_pending_approvals_by_status("pending") or []
        return {(r.get("content") or {}).get("dedupe_key")
                for r in rows if r.get("content_type") == "gantt_nudge"}
    except Exception:
        return set()


def compute_gantt_nudges(sheet=None, shadow: bool | None = None) -> dict:
    sheet = sheet or settings.GANTT_MAIN_TAB
    if shadow is None:
        shadow = getattr(settings, "GANTT_SHADOW_MODE", True)

    lanes = [r for r in supabase_client.get_gantt_rows(sheet) if r.get("lane_type")]
    existing = _existing_nudge_keys()
    candidates = []  # (severity, nudge dict)

    for ln in lanes:
        links = supabase_client.get_knowledge_links(
            from_type="gantt_row", from_id=ln["id"], link_type="gantt_covers") or []
        topic_ids = [l["to_id"] for l in links if l.get("to_id")]
        if not topic_ids:
            continue
        lane_label = f"{ln['lane_type']}#{ln['lane_index']}"
        board_status = (ln.get("status") or "").lower()

        for tid in topic_ids:
            t = supabase_client.get_topic_thread(tid)
            if not t or _is_ceo_only(t):
                continue
            bj = t.get("brief_json") or {}
            tstatus = _topic_status(t)
            name = t.get("topic_name", "")

            # severity 3: a blocker open_item with no board signal
            blockers = [o for o in (bj.get("open_items") or [])
                        if isinstance(o, dict) and str(o.get("kind", "")).lower() == "blocker"]
            if blockers and board_status != "blocked":
                candidates.append((3, _mk(ln, lane_label, name, "blocker_unflagged",
                                          f"'{name}' has a blocker ({blockers[0].get('description','')[:60]}) but the board isn't flagged blocked")))
            # severity 2: brief blocked/stale while the bar reads active
            elif tstatus in ("blocked", "stale") and board_status == "active":
                candidates.append((2, _mk(ln, lane_label, name, f"status_{tstatus}",
                                          f"brief says '{name}' is {tstatus}, but the board shows this lane active")))
            # severity 2: a recent decision likely not reflected on the board
            recent = bj.get("recent_decisions") or []
            if recent and isinstance(recent[0], dict):
                candidates.append((2, _mk(ln, lane_label, name, "recent_decision",
                                          f"recent decision on '{name}': {str(recent[0].get('text',''))[:70]} — reflected on the board?")))

    # dedupe vs last cycle, drop below threshold, rank, global cap
    out = []
    seen = set()
    for sev, n in sorted(candidates, key=lambda c: -c[0]):
        if sev < _SURFACE_MIN:
            continue
        key = n["dedupe_key"]
        if key in seen or key in existing:
            continue
        seen.add(key)
        out.append(n)
        if len(out) >= _CAP:
            break

    summary = {"sheet": sheet, "lanes": len(lanes), "nudges": len(out), "shadow": shadow}
    if not shadow:
        for n in out:
            try:
                supabase_client.upsert_pending_approval(
                    approval_id=n["dedupe_key"], content_type="gantt_nudge", content=n)
            except Exception as e:
                logger.warning(f"nudge upsert failed: {e}")
    logger.info(f"[gantt_nudge]{'[shadow]' if shadow else ''} {summary}")
    return {**summary, "items": out}


def _mk(ln, lane_label, topic, kind, message):
    area = ln.get("area_id", "")
    return {
        "dedupe_key": f"gnudge-{ln['id'][:8]}-{kind}",
        "gantt_row_id": ln["id"], "area_id": area, "lane": lane_label,
        "topic": topic, "kind": kind, "message": message,
        "suggested_action": "update the board lane or clear the divergence",
    }
