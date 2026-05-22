"""
Strategic Gantt builder (v3 chunk 2 — fixed-template, DB-backed).

Builds the new Gantt: Strategic Milestones + Management bands, then per Area
2 Planning + 3 Execution + 1 Meetings + 1 HR lanes. The LEFT column is the fixed
lane label ("Planning #1"); the workstream CONTENT lives in the timeline cells.

Source of truth = the `gantt_rows` table (one row per lane, carrying lane_type/
lane_index/label/owner/status/week_start-end). build_gantt() (re)derives the
lanes from the live Gantt's authored workstreams (LLM-consolidated), persists
them, and renders the sheet from the persisted rows — so the generator and the
bidirectional reconcile share one DB-backed structure.
"""

import json
import logging
import re

from config.settings import settings
from core.llm import call_llm
from services.gantt_manager import _get_color_map, _hex_to_sheets_color
from services.google_sheets import sheets_service
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

N_WEEKS, FIRST_WEEK, DATA_C0, TAG_C0 = 24, 9, 4, 129
ACTIVE = ("active", "blocked", "pending_decision")
_OWNER_RE = re.compile(r"^\s*(\[[A-Za-z/&, ]+\])")
# lane_type -> default status (real status comes from briefs later)
_LANE_STATUS = {"planning": "planned", "execution": "active", "meetings": "planned",
                "hr": "planned", "milestone": "active", "management": "planned"}


def _status(t):
    for s in (t.get("brief_json"), t.get("state_json")):
        if isinstance(s, dict) and s.get("current_status"):
            return str(s["current_status"]).lower()
    return "unknown"


def _owner(label):
    m = _OWNER_RE.match(label or "")
    return m.group(1) if m else ""


def _col_letter(i0):
    s = ""; i = i0 + 1
    while i > 0:
        i, r = divmod(i - 1, 26); s = chr(65 + r) + s
    return s


def _extract_sections():
    resp = sheets_service.service.spreadsheets().values().get(
        spreadsheetId=settings.GANTT_SHEET_ID, range=f"'{settings.GANTT_MAIN_TAB}'!A6:BL70").execute()
    out, seen, section = {}, {}, None
    for row in resp.get("values", []):
        label = " ".join((c or "").strip() for c in row[:4] if (c or "").strip()).strip()
        if label and label.upper() == label and len(re.sub(r"[^A-Za-z]", "", label)) > 4:
            section = label; out.setdefault(section, []); seen.setdefault(section, set()); continue
        if section is None:
            continue
        for cell in row[4:]:
            txt = (cell or "").strip().replace("\n", " ")
            if not txt or txt == "#REF!":
                continue
            k = re.sub(r"\s+", " ", txt.lower())[:50]
            if k not in seen[section]:
                seen[section].add(k); out[section].append(txt)
    return out


def _fill_template(area_name, items, topics):
    prompt = (
        f"AREA: {area_name}\n\nConsolidate this area's work into EXACTLY these strategic lanes "
        "(keep an owner prefix like [E/P]; CONCISE names <=8 words; fewer/bigger is better):\n"
        "- up to 2 PLANNING workstreams\n- up to 3 EXECUTION workstreams\n"
        "- 1 MEETINGS lane\n- 1 HUMAN RESOURCES lane\n\n"
        "GANTT WORK-ITEMS:\n" + "\n".join(f"- {i}" for i in items) + "\n\n"
        "TOPICS:\n" + "\n".join(f"- {t}" for t in topics) + "\n\n"
        'Return ONLY JSON: {"planning":["..."],"execution":["..."],"meetings":"...","hr":"..."}'
    )
    text, _ = call_llm(prompt, model=settings.model_agent, max_tokens=700, call_site="gantt_template_fill")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def _match_section(sections, area_name):
    aw = set(re.findall(r"[a-z]+", area_name.lower()))
    for sec in sections:
        if aw & set(re.findall(r"[a-z]+", sec.lower())):
            return sec
    return None


async def build_lane_records(tab_name: str) -> list[dict]:
    """Derive the fixed-template lanes (no DB writes). Each = a gantt_rows dict."""
    sections = _extract_sections()
    areas = supabase_client.get_areas()
    topics = (supabase_client.client.table("topic_threads")
              .select("id,topic_name,area_id,brief_json,state_json").is_("valid_to", "null").execute().data or [])
    lanes = []

    def add(area_id, lt, idx, label, ws=0, we=0):
        lanes.append({"sheet_name": tab_name, "area_id": area_id, "lane_type": lt, "lane_index": idx,
                      "label": label or "", "owner": _owner(label), "status": _LANE_STATUS.get(lt, "planned"),
                      "week_start": ws or None, "week_end": we or None})

    for i, it in enumerate((sections.get("STRATEGIC MILESTONES", []) or [])[:4]):
        add(None, "milestone", i + 1, it, (i * 5) % 18 + FIRST_WEEK, (i * 5) % 18 + FIRST_WEEK + 2)
    for i, it in enumerate((sections.get("MANAGEMENT — CEO OPERATING VIEW", []) or [])[:3]):
        add(None, "management", i + 1, it, FIRST_WEEK, FIRST_WEEK + 5)

    for a in areas:
        sec = _match_section(sections, a["name"])
        items = sections.get(sec, []) if sec else []
        atopics = [t["topic_name"] for t in topics if t.get("area_id") == a["id"] and _status(t) in ACTIVE]
        tpl = _fill_template(a["name"], items, atopics)
        plans = (tpl.get("planning") or [])[:2] + ["", ""]
        execs = (tpl.get("execution") or [])[:3] + ["", "", ""]
        for i in range(2):
            add(a["id"], "planning", i + 1, plans[i], (i * 4) % 10 + FIRST_WEEK, (i * 4) % 10 + FIRST_WEEK + 5)
        for i in range(3):
            add(a["id"], "execution", i + 1, execs[i], (i * 3) % 9 + FIRST_WEEK, (i * 3) % 9 + FIRST_WEEK + 6)
        add(a["id"], "meetings", 1, tpl.get("meetings") or "", FIRST_WEEK, FIRST_WEEK + 3)
        add(a["id"], "hr", 1, tpl.get("hr") or "", FIRST_WEEK, FIRST_WEEK + 2)
    return lanes


def persist_lanes(tab_name: str, lanes: list[dict]) -> int:
    """Soft-close existing lanes for this tab, insert the fresh set."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    try:
        supabase_client.client.table("gantt_rows").update({"valid_to": now}).eq(
            "sheet_name", tab_name).is_("valid_to", "null").execute()
    except Exception as e:
        logger.warning(f"[gantt_builder] soft-close failed: {e}")
    n = 0
    for ln in lanes:
        try:
            supabase_client.client.table("gantt_rows").insert(ln).execute()
            n += 1
        except Exception as e:
            logger.warning(f"[gantt_builder] insert lane failed ({ln.get('lane_type')}#{ln.get('lane_index')}): {e}")
    return n


async def render(target_sheet_id: str, tab_name: str, lanes: list[dict]) -> None:
    """Render the lanes to the target sheet/tab: left = lane label, cells = content bar."""
    svc = sheets_service.service
    cmap = _get_color_map()
    color_of = {"planned": cmap.get("planned", "#cce0f0"), "active": cmap.get("active", "#b7d7b0"),
                "blocked": cmap.get("blocked", "#e85050"), "completed": cmap.get("completed", "#d0d0cc")}
    BAND = "#2d3a3a"
    AREA_BANDS = ["#2d6a4f", "#1b4332", "#40916c", "#52796f", "#354f52", "#2d3a3a"]
    areas = {a["id"]: a["name"] for a in supabase_client.get_areas()}

    meta = svc.spreadsheets().get(spreadsheetId=target_sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab_name:
            svc.spreadsheets().batchUpdate(spreadsheetId=target_sheet_id, body={"requests": [
                {"deleteSheet": {"sheetId": s["properties"]["sheetId"]}}]}).execute()
    add = svc.spreadsheets().batchUpdate(spreadsheetId=target_sheet_id, body={"requests": [
        {"addSheet": {"properties": {"title": tab_name, "gridProperties": {"rowCount": 140, "columnCount": 140}}}}]}).execute()
    sid = add["replies"][0]["addSheet"]["properties"]["sheetId"]

    values, fmts = [], []
    values.append((1, 0, "CropSight — Strategic Gantt"))
    for w in range(N_WEEKS):
        values.append((3, DATA_C0 + w, f"W{FIRST_WEEK + w}"))
    r = 5
    lane_label = {"planning": "Planning", "execution": "Execution", "meetings": "Meetings",
                  "hr": "Human Resources", "milestone": "Milestone", "management": "Management"}

    def band(text, color):
        nonlocal r
        values.append((r, 0, text))
        for w in range(N_WEEKS):
            fmts.append((r - 1, DATA_C0 + w, color))
        r += 1

    def row(left, content, color, ws, we):
        nonlocal r
        values.append((r, 0, left))
        s0 = max(0, (ws or FIRST_WEEK) - FIRST_WEEK)
        s1 = max(s0 + 1, (we or FIRST_WEEK) - FIRST_WEEK + 1)
        for w in range(s0, min(s1, N_WEEKS)):
            fmts.append((r - 1, DATA_C0 + w, color))
            if content:
                values.append((r, DATA_C0 + w, content))
        r += 1

    # group lanes by area in canonical order
    by_area = {}
    bands = {"milestone": [], "management": []}
    for ln in lanes:
        if ln["lane_type"] in bands:
            bands[ln["lane_type"]].append(ln)
        else:
            by_area.setdefault(ln["area_id"], []).append(ln)

    band("STRATEGIC MILESTONES", BAND)
    for ln in sorted(bands["milestone"], key=lambda x: x["lane_index"]):
        row(f"Milestone #{ln['lane_index']}", ln["label"], "#1b4332", ln["week_start"], ln["week_end"])
    band("MANAGEMENT — CEO OPERATING VIEW", BAND)
    for ln in sorted(bands["management"], key=lambda x: x["lane_index"]):
        row(f"Management #{ln['lane_index']}", ln["label"], color_of["planned"], ln["week_start"], ln["week_end"])
    r += 1

    order = {"planning": 0, "execution": 1, "meetings": 2, "hr": 3}
    for ai, (aid, alanes) in enumerate(by_area.items()):
        band(areas.get(aid, "?").upper(), AREA_BANDS[ai % len(AREA_BANDS)])
        for ln in sorted(alanes, key=lambda x: (order.get(x["lane_type"], 9), x["lane_index"])):
            n = lane_label[ln["lane_type"]]
            left = f"{n} #{ln['lane_index']}" if ln["lane_type"] in ("planning", "execution") else n
            row(left, ln["label"], color_of.get(ln["status"], color_of["planned"]), ln["week_start"], ln["week_end"])
        r += 1

    data = [{"range": f"'{tab_name}'!{_col_letter(c)}{rw}", "values": [[t]]} for (rw, c, t) in values]
    svc.spreadsheets().values().batchUpdate(spreadsheetId=target_sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
    reqs = [{"repeatCell": {"range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r0 + 1, "startColumnIndex": c0, "endColumnIndex": c0 + 1},
                            "cell": {"userEnteredFormat": {"backgroundColor": _hex_to_sheets_color(h)}},
                            "fields": "userEnteredFormat.backgroundColor"}} for (r0, c0, h) in fmts]
    for i in range(0, len(reqs), 400):
        svc.spreadsheets().batchUpdate(spreadsheetId=target_sheet_id, body={"requests": reqs[i:i + 400]}).execute()


async def build_gantt(target_sheet_id: str | None = None, tab_name: str = "Strategic Gantt", apply: bool = False) -> dict:
    """Derive lanes -> (optionally) persist to gantt_rows -> render to the target sheet."""
    target_sheet_id = target_sheet_id or settings.GANTT_SHEET_ID
    lanes = await build_lane_records(tab_name)
    summary = {"tab": tab_name, "lanes": len(lanes), "applied": apply}
    if apply:
        summary["persisted"] = persist_lanes(tab_name, lanes)
        await render(target_sheet_id, tab_name, lanes)
        try:
            supabase_client.log_action("gantt_built", details=summary, triggered_by="eyal")
        except Exception:
            pass
    logger.info(f"[gantt_builder] {summary}")
    return summary
