"""Milestones — the company's dated commitments, and their slippage.

Phase 3 of docs/GANTT_V2_PLAN.md.

WHERE THEY COME FROM. Not from the old board's `STRATEGIC MILESTONE` section,
which is structurally present and completely empty. The real ones live in four
lanes, and the board marks them with glyphs that map cleanly onto a kind:

    ★  Technology     product      MVP Product Delivery, Product V1 Target
    ●  Commercial     commercial   First MVP Delivery, 1st Paying Client, Q-goals
    ◆  Funding        funding      Pre-Seed Round timing
       Company OKRs   (by title)   Q1 OKR, Investor's Package, Signing #1

`Strategy & Decisions` is deliberately NOT a source. It holds a monthly investor
update repeating to 2027-11, an annual planning offsite each December, and a
couple of decisions. That is recurrence and decision history — the recurrence
belongs to the Meetings pool, which already models it, and inventing a second
recurrence concept here would be the mistake the plan warns about.

SLIPPAGE IS THE POINT. The board records its own: `Signing #1 MVP client` sits
at 2026-06-01 and again seven weeks later as `Signing #1 MVP client - postponed
here`. Both appear in TWO lanes, so one commitment and one move are spread
across four bars. Collapsing that correctly — one milestone, original 1 Jun,
target 6 Jul, one recorded move — is most of what this module does.

NOTHING IS AUTO-CREATED. Seeding proposes; Eyal approves. Same gate as start
dates, and for the same reason: reading a milestone off a board is a judgement.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

PROPOSAL_TYPE = "milestone_proposal"
ISRAEL_TZ = timezone(timedelta(hours=3))

# Lives here, not in services/ceo_sheet.py, so `project_status_reconcile` can
# put it in NON_AREA_TABS without a processor importing a service — the same
# arrangement TIMELINE_TAB has. Registration is not optional: an unregistered
# tab is parsed as an area tab and stripped by the formatting pass, which is
# what happened to the meetings pool on 2026-08-09.
CEO_TAB = "CEO"

STAR, DOT, DIAMOND = "★", "●", "◆"

# Lane -> kind, for the three glyph lanes that carry no section on the board.
_LANE_KIND = {"Technology": "product",
              "Commercial": "commercial",
              "Funding": "funding"}
_GLYPH_KIND = {STAR: "product", DOT: "commercial", DIAMOND: "funding"}

# The only lanes milestones are read from. See the module docstring for why
# `Strategy & Decisions` is not among them.
SOURCE_LANES = ("Company OKRs", "Technology", "Commercial", "Funding")

_OWNER_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")
_GLYPHS = re.compile(f"[{STAR}{DOT}{DIAMOND}]")
# "- postponed here", "— postponed to Q3", "(postponed)" ... the board writes
# this by hand, so match the intent rather than one exact phrasing.
_MOVED = re.compile(r"\s*[-–—(]+\s*(postponed|moved|delayed|pushed)\b[^)]*\)?\s*$",
                    re.IGNORECASE)


def _clean(label: str) -> str:
    """Board text -> a milestone title: owner tag and glyph stripped."""
    out = _OWNER_TAG.sub("", label or "")
    out = _GLYPHS.sub("", out)
    return re.sub(r"\s+", " ", out).strip(" ·-–—").strip()


def normalise_title(label: str) -> str:
    """The identity of a milestone, for matching a moved bar to its original.

    Strips the trailing "- postponed here" the board uses to mark a slip, so the
    two bars collapse to one commitment. Lowercased and whitespace-flattened
    because the board is hand-typed and inconsistent about both.
    """
    return _MOVED.sub("", _clean(label)).lower().strip()


def is_move_marker(label: str) -> bool:
    """Does this bar announce itself as the moved copy of an earlier one?"""
    return bool(_MOVED.search(_clean(label)))


def _kind_of(label: str, lane: str) -> "str | None":
    for glyph, kind in _GLYPH_KIND.items():
        if glyph in (label or ""):
            return kind
    if lane in _LANE_KIND:
        return _LANE_KIND[lane]
    # Company OKRs carries no glyph; read the title.
    low = (label or "").lower()
    if any(w in low for w in ("raising", "investor", "round", "funding", "package")):
        return "funding"
    if any(w in low for w in ("signing", "client", "paying", "account", "revenue")):
        return "commercial"
    if any(w in low for w in ("mvp", "product", "delivery", "v1", "platform")):
        return "product"
    if any(w in low for w in ("legal", "entity", "bp", "branch", "esop", "corporate")):
        return "corporate"
    return None


def collapse_bars(bars: list[dict]) -> list[dict]:
    """Archived bars -> one entry per milestone, with its move history.

    Deduplicates across lanes (the same commitment is written in both
    `Company OKRs` and `Commercial`) and folds a "postponed here" bar into the
    milestone it moved, rather than emitting it as a second milestone.
    """
    groups: dict[str, list[dict]] = {}
    for b in bars:
        lane = (b.get("lane") or "").strip()
        label = b.get("label") or ""
        if lane not in SOURCE_LANES:
            continue
        if not b.get("start_date"):
            continue
        key = normalise_title(label)
        if not key:
            continue
        groups.setdefault(key, []).append(b)

    out = []
    for key, rows in groups.items():
        rows = sorted(rows, key=lambda r: (r["start_date"], is_move_marker(r.get("label") or "")))
        # The earliest date is the original commitment; the latest is where the
        # board last put it. Same date twice across two lanes is a duplicate,
        # not a move — dedupe on the date, not the row.
        dates = sorted({r["start_date"] for r in rows})
        first, last = dates[0], dates[-1]
        # Prefer a label WITHOUT the move marker for the title, and the longest
        # such — the board's fuller phrasing is the more useful one.
        titles = [r.get("label") or "" for r in rows if not is_move_marker(r.get("label") or "")]
        source = max(titles or [r.get("label") or "" for r in rows], key=len)
        lane = next((r.get("lane") or "").strip() for r in rows)

        out.append({
            "title": _clean(source),
            "key": key,
            "kind": _kind_of(source, lane),
            "original_date": first,
            "target_date": last,
            "moves": [{"from_date": a, "to_date": b_}
                      for a, b_ in zip(dates, dates[1:])],
            "source_label": source[:200],
            "lanes": sorted({(r.get("lane") or "").strip() for r in rows}),
            "bar_count": len(rows),
        })
    return sorted(out, key=lambda m: m["original_date"])


def propose_milestones() -> dict:
    """Emit one proposal per milestone found on the archived board.

    Nothing is written to `milestones` here — approving is what creates it,
    exactly like `project_start_proposal`.
    """
    c = supabase_client.client
    out = {"proposed": 0, "already_exists": 0, "already_pending": 0, "no_kind": 0}

    try:
        bars = (c.table("gantt_legacy_bars").select("*").limit(1000)
                .execute()).data or []
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[milestones] legacy bars unavailable: {e}")
        return {**out, "skipped": "no archive"}

    try:
        existing = {(m.get("title") or "").strip().lower()
                    for m in (c.table("milestones").select("title")
                              .execute()).data or []}
    except Exception as e:                                   # noqa: BLE001
        # The migration has not been run yet. Proposing against a table that
        # cannot receive the result would queue cards Eyal cannot action.
        logger.warning(f"[milestones] table not ready, not proposing: {e}")
        return {**out, "skipped": "migration not run"}

    pending = {
        (a.get("content") or {}).get("key")
        for a in (supabase_client.get_pending_approvals_by_status("pending") or [])
        if a.get("content_type") == PROPOSAL_TYPE
    }

    for m in collapse_bars(bars):
        if m["title"].strip().lower() in existing:
            out["already_exists"] += 1
            continue
        if m["key"] in pending:
            out["already_pending"] += 1
            continue
        if not m["kind"]:
            # Better to leave it out than file it under a guessed kind; the bar
            # stays visible in the archive block either way.
            out["no_kind"] += 1
            continue

        supabase_client.upsert_pending_approval(
            approval_id=f"milestone-{abs(hash(m['key'])) % (10 ** 12)}",
            content_type=PROPOSAL_TYPE,
            content={
                "key": m["key"],
                "title": m["title"],
                "kind": m["kind"],
                "original_date": m["original_date"],
                "target_date": m["target_date"],
                "moves": m["moves"],
                "source_label": m["source_label"],
                "lanes": m["lanes"],
            },
        )
        out["proposed"] += 1

    logger.info(f"[milestones] {out}")
    return out


def apply_milestone(content: dict) -> dict:
    """Approve: create the milestone and record any moves the board showed."""
    title = (content or {}).get("title")
    if not title:
        return {"ok": False, "error": "proposal carries no title"}

    target = content.get("target_date")
    original = content.get("original_date") or target
    now = datetime.now(ISRAEL_TZ).isoformat()
    try:
        row = (supabase_client.client.table("milestones").insert({
            "title": title,
            "kind": content.get("kind"),
            "target_date": target,
            # Written ONCE, here. Nothing else in this module ever updates it.
            "original_date": original,
            "status": "open",
            "source_label": content.get("source_label"),
            # Approving a specific date IS a decision, so the rail defends it
            # from later inference — the same reasoning as apply_project_start.
            "manual_target_date": True,
            "manual_set_at": now,
            "manual_set_source": "approval",
        }).execute()).data
    except Exception as e:                                   # noqa: BLE001
        logger.error(f"[milestones] insert failed for {title!r}: {e}")
        return {"ok": False, "error": str(e)}

    mid = (row or [{}])[0].get("id")
    moves = content.get("moves") or []
    if mid and moves:
        try:
            supabase_client.client.table("milestone_moves").insert(
                [{"milestone_id": mid, "from_date": mv.get("from_date"),
                  "to_date": mv.get("to_date"), "source": "legacy_board"}
                 for mv in moves]).execute()
        except Exception as e:                               # noqa: BLE001
            # The milestone is the record that matters; a lost move row is a
            # thinner history, not a wrong one.
            logger.warning(f"[milestones] move history not recorded for {title!r}: {e}")

    return {"ok": True, "milestone_id": mid, "title": title,
            "target_date": target, "moves": len(moves)}


def move_milestone(milestone_id: str, to_date: str, source: str = "sheet") -> dict:
    """Change a milestone's target date, keeping the fact that it moved.

    `original_date` is never touched. If it was somehow never set, it is filled
    from the date being left behind — a milestone whose first commitment is
    unknown is worse than one that remembers where it started.
    """
    c = supabase_client.client
    try:
        cur = (c.table("milestones").select("*").eq("id", milestone_id)
               .limit(1).execute()).data or []
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if not cur:
        return {"ok": False, "error": "milestone not found"}

    row = cur[0]
    from_date = row.get("target_date")
    if str(from_date or "") == str(to_date or ""):
        return {"ok": True, "unchanged": True, "milestone_id": milestone_id}

    now = datetime.now(ISRAEL_TZ).isoformat()
    updates = {"target_date": to_date, "updated_at": now,
               "manual_target_date": True, "manual_set_at": now,
               "manual_set_source": source}
    if not row.get("original_date"):
        updates["original_date"] = from_date

    try:
        c.table("milestones").update(updates).eq("id", milestone_id).execute()
        c.table("milestone_moves").insert({
            "milestone_id": milestone_id, "from_date": from_date,
            "to_date": to_date, "source": source}).execute()
    except Exception as e:                                   # noqa: BLE001
        logger.error(f"[milestones] move failed for {milestone_id}: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "milestone_id": milestone_id,
            "from_date": from_date, "to_date": to_date}


def list_milestones() -> list[dict]:
    """Every milestone with its move count, earliest target first."""
    c = supabase_client.client
    try:
        rows = (c.table("milestones").select("*")
                .order("target_date").execute()).data or []
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[milestones] unavailable: {e}")
        return []
    try:
        moves = (c.table("milestone_moves").select("milestone_id,from_date,to_date")
                 .limit(2000).execute()).data or []
    except Exception:                                        # noqa: BLE001
        moves = []

    by_id: dict[str, list] = {}
    for mv in moves:
        by_id.setdefault(mv["milestone_id"], []).append(mv)
    for r in rows:
        r["moves"] = sorted(by_id.get(r["id"], []),
                            key=lambda m: str(m.get("to_date") or ""))
    return rows
