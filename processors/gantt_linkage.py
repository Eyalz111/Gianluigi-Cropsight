"""
PR-C: per-lane → topics linkage (DB-only; `knowledge_links` 'gantt_covers').

A Gantt lane (Area × phase) holds work-items that advance several topics. This
links each lane to the topics it covers, by matching the lane's authored cell
content to its Area's topics. Proposal-only (never auto-applied); the SHADOW
dry-run produces the lane→topic table that is the go/no-go gate for the whole
Gantt backend. No board writes ever.
"""

import json
import logging
import re

from config.settings import settings
from core.llm import call_llm
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

ACTIVE = ("active", "blocked", "pending_decision")
_SCORE_T = 0.25


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _overlap(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _status(t):
    for s in (t.get("brief_json"), t.get("state_json")):
        if isinstance(s, dict) and s.get("current_status"):
            return str(s["current_status"]).lower()
    return "unknown"


def _content_by_row(sheet):
    """Distinct authored work-item texts per row on the live Gantt."""
    from services.google_sheets import sheets_service
    resp = sheets_service.service.spreadsheets().values().get(
        spreadsheetId=settings.GANTT_SHEET_ID, range=f"'{sheet}'!A6:BL70").execute()
    out = {}
    for i, row in enumerate(resp.get("values", []), start=6):
        seen, items = set(), []
        for cell in row[4:]:
            txt = (cell or "").strip().replace("\n", " ")
            if not txt or txt == "#REF!":
                continue
            k = re.sub(r"\s+", " ", txt.lower())[:50]
            if k not in seen:
                seen.add(k); items.append(txt)
        if items:
            out[i] = items
    return out


def _brief_snippet(t, n=70):
    bj = t.get("brief_json")
    if isinstance(bj, dict):
        return str(bj.get("narrative") or "")[:n]
    return ""


def _llm_match_area(area_name, lanes, topics) -> dict:
    """Semantically map each lane → the topic_ids it covers (token-overlap can't bridge the vocab gap)."""
    lane_block = "\n".join(
        f"- {p['lane']}: " + " | ".join(p["content"][:4]) for p in lanes if p["content"])
    if not lane_block or not topics:
        return {}
    topic_block = "\n".join(f"[{t['id']}] {t['topic_name']} — {_brief_snippet(t)}" for t in topics)
    prompt = (
        f"AREA: {area_name}. Each Gantt LANE holds work-items; each TOPIC is a workstream. "
        "For each lane, list the **3-5 CORE topic IDs that lane PRIMARILY covers** — its main work. "
        "Match by MEANING, not shared words (the Gantt uses shorthand). Omit tangential/loose matches; "
        "a lane covers 0-5 topics.\n\n"
        f"LANES:\n{lane_block}\n\nTOPICS:\n{topic_block}\n\n"
        'Return ONLY JSON: {"<lane>": ["<topic_id>", ...]}'
    )
    text, _ = call_llm(prompt, model=settings.model_agent, max_tokens=1200, call_site="gantt_link_match")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def build_link_proposals(sheet=None) -> dict:
    """Produce the lane→topic candidate table via semantic (LLM) matching. No writes."""
    sheet = sheet or settings.GANTT_MAIN_TAB
    lanes = [r for r in supabase_client.get_gantt_rows(sheet) if r.get("lane_type") and r.get("area_id")]
    content = _content_by_row(sheet)
    topics = (supabase_client.client.table("topic_threads")
              .select("id,topic_name,area_id,brief_json,state_json").is_("valid_to", "null").execute().data or [])
    by_area_topics, areas = {}, {a["id"]: a["name"] for a in supabase_client.get_areas()}
    for t in topics:
        if _status(t) in ACTIVE and t.get("area_id"):
            by_area_topics.setdefault(t["area_id"], []).append(t)

    # assemble per-area lane proposals (content first), then LLM-match per area
    by_area_lanes = {}
    for ln in lanes:
        by_area_lanes.setdefault(ln["area_id"], []).append({
            "gantt_row_id": ln["id"], "area_id": ln["area_id"], "area": areas.get(ln["area_id"], "?"),
            "lane": f"{ln['lane_type']}#{ln['lane_index']}", "row": ln.get("display_order"),
            "content": content.get(ln.get("display_order"), []), "candidates": [],
        })

    proposals = []
    for aid, alanes in by_area_lanes.items():
        atopics = by_area_topics.get(aid, [])
        tmap = {t["id"]: t for t in atopics}
        matches = _llm_match_area(areas.get(aid, "?"), alanes, atopics)
        for p in alanes:
            ids = matches.get(p["lane"], []) if isinstance(matches, dict) else []
            for tid in ids:
                t = tmap.get(tid)
                if t:
                    p["candidates"].append({"topic_id": tid, "topic_name": t["topic_name"], "score": "llm"})
            p["content"] = p["content"][:4]
            proposals.append(p)
    return {"sheet": sheet, "proposals": proposals}


def propose_lane_links(sheet=None, persist_preview=False) -> dict:
    """SHADOW dry-run: build + report the link table (the go/no-go gate). No links created."""
    table = build_link_proposals(sheet)
    if persist_preview:
        try:
            supabase_client.upsert_pending_approval(
                approval_id=f"glink-{table['sheet']}", content_type="gantt_link_preview", content=table)
        except Exception as e:
            logger.warning(f"persist link preview failed: {e}")
    return table


def apply_lane_links(proposals: list[dict]) -> dict:
    """Apply approved lane→topic links as knowledge_links 'gantt_covers' (DB-only)."""
    n = 0
    for p in proposals:
        for c in p.get("candidates", []):
            try:
                supabase_client.create_knowledge_link(
                    from_type="gantt_row", from_id=p["gantt_row_id"],
                    to_type="topic", to_id=c["topic_id"],
                    link_type="gantt_covers", created_by="eyal")
                n += 1
            except Exception as e:
                logger.warning(f"link create failed: {e}")
    logger.info(f"[gantt_linkage] created {n} gantt_covers links")
    return {"links_created": n}
