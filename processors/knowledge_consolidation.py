"""
Nightly knowledge consolidation (v2.5 PR7/8).

Keeps the topic briefs honest between weekly syntheses:
- Staleness: an active topic with no activity in 30+ days is marked 'stale'
  (deterministic) so the morning brief can surface it.
- Fact de-duplication: drop exact-duplicate facts that accumulate from on-event
  merges (deterministic).
- Light reconcile: for topics touched in the last 24h, a cheap Haiku pass
  cleans contradictions and tightens the narrative/status — bounded to a
  handful of topics per night, and conservative (only narrative/status/facts
  are taken from the model; risks/next_actions/citations are preserved).

Nothing here touches shipped meeting summaries — it only updates internal
briefs (a "quiet" update that auto-applies). When KNOWLEDGE_SHADOW_MODE is on,
it computes + logs the changes without applying them.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

_STALE_DAYS = 30
_TOUCHED_WINDOW_H = 24


def _parse_dt(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_json(response: str) -> dict | None:
    if not response:
        return None
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    obj = re.search(r"\{[\s\S]*\}", response)
    if obj:
        try:
            return json.loads(obj.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _dedupe_facts(brief: dict) -> bool:
    """Remove exact-duplicate facts (by normalized text). Returns True if changed."""
    facts = brief.get("facts") or []
    seen, out = set(), []
    for f in facts:
        key = (f.get("text") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(f)
    if len(out) != len(facts):
        brief["facts"] = out
        return True
    return False


def _reconcile_brief(topic_name: str, brief: dict) -> dict | None:
    """
    Cheap Haiku clean-up of a recently-touched brief. Conservative: only
    narrative / current_status / facts are taken from the model; everything
    else (risks, next_actions, citations, open_items) is preserved. Returns the
    cleaned brief dict or None on any failure.
    """
    try:
        from core.llm import call_llm
        from models.schemas import TopicBrief

        prompt = f"""Clean up this CropSight topic brief. Remove redundant or contradictory
facts, ensure current_status reflects the facts and open items, and tighten the
narrative. Do NOT add new information not already present.

Topic: {topic_name}
Brief JSON:
{json.dumps(brief, ensure_ascii=False)[:4000]}

Return ONLY valid JSON with keys: narrative (string),
current_status (active|blocked|pending_decision|stale|closed),
facts (array of {{"text","sensitivity","citation"}}). Preserve each fact's
existing sensitivity and citation. No prose, no code fences."""

        response, _usage = call_llm(
            prompt=prompt,
            model=settings.model_simple,  # Haiku — cheap, bounded to touched topics
            max_tokens=2000,
            call_site="nightly_reconcile",
        )
        data = _parse_json(response)
        if not data:
            return None

        cleaned = dict(brief)  # preserve all fields by default
        cleaned["narrative"] = data.get("narrative", brief.get("narrative", ""))
        cleaned["current_status"] = data.get("current_status", brief.get("current_status", "active"))
        if isinstance(data.get("facts"), list) and data["facts"]:
            # Haiku sometimes drops the REQUIRED `sensitivity` on a fact, which
            # made TopicBrief(**cleaned) ValidationError → return None for EVERY
            # touched topic every night — a broken hot path silently burning Haiku
            # tokens with reconciled=0. Default a missing tier to the brief's own
            # tier (NOT 'founders' blindly — that could downgrade a CEO fact and
            # leak it). Conservative: never tier a fact LOWER than the brief. [audit P2-10]
            brief_tier = brief.get("sensitivity") or "founders"
            _valid = {"public", "team", "founders", "ceo"}
            fixed_facts = []
            for f in data["facts"]:
                if isinstance(f, dict):
                    s = str(f.get("sensitivity") or "").lower()
                    # Missing tier silently defaults to FOUNDERS (a DOWNGRADE/leak
                    # if the source was CEO); an INVALID tier raises ValidationError
                    # (the nightly no-op). Both → the brief's own tier (conservative).
                    if s not in _valid:
                        f = {**f, "sensitivity": brief_tier}
                fixed_facts.append(f)
            cleaned["facts"] = fixed_facts
        return TopicBrief(**cleaned).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"[nightly] reconcile failed for '{topic_name}': {e}")
        return None


async def run_consolidation(apply: bool | None = None) -> dict:
    """
    Run the nightly consolidation sweep over all topics with a brief.

    apply: when True, write changes; when False, log-only (shadow). Defaults to
    (not KNOWLEDGE_SHADOW_MODE) so the global shadow switch controls it.
    """
    if apply is None:
        apply = not settings.KNOWLEDGE_SHADOW_MODE

    now = datetime.now(timezone.utc)
    topics = (
        supabase_client.client.table("topic_threads")
        .select("id, topic_name, status, brief_json, last_updated")
        .not_.is_("brief_json", "null")
        .execute()
        .data
        or []
    )

    staled = deduped = reconciled = updated = reconcile_failed = 0
    changes: list[dict] = []

    for t in topics:
        brief = t.get("brief_json") or {}
        if not brief:
            continue
        changed = False
        last = _parse_dt(t.get("last_updated"))

        # 1. Staleness (deterministic)
        status = brief.get("current_status", "active")
        if last and (now - last).days >= _STALE_DAYS and status in ("active", "blocked", "pending_decision"):
            brief["current_status"] = "stale"
            changed = True
            staled += 1

        # 2. Fact de-duplication (deterministic)
        if _dedupe_facts(brief):
            changed = True
            deduped += 1

        # 3. Light Haiku reconcile for recently-touched topics (bounded)
        if last and (now - last) <= timedelta(hours=_TOUCHED_WINDOW_H):
            recon = _reconcile_brief(t.get("topic_name", ""), brief)
            if recon:
                brief = recon
                changed = True
                reconciled += 1
            else:
                # A None here is a real reconcile failure (parse/validation/LLM),
                # not a no-op — surface it instead of silently counting nothing. [audit P2-10]
                reconcile_failed += 1

        if changed:
            brief["version"] = int(brief.get("version", 0)) + 1
            changes.append({"topic": t.get("topic_name"), "status": brief.get("current_status")})
            if apply:
                supabase_client.update_topic_brief(t["id"], brief)
                if brief.get("current_status") == "stale" and t.get("status") != "stale":
                    try:
                        supabase_client.client.table("topic_threads").update(
                            {"status": "stale"}
                        ).eq("id", t["id"]).execute()
                    except Exception:
                        pass
                # Keep the semantic index in step with the consolidated narrative:
                # a staled topic is deindexed, an active one reindexed (audit TS-03).
                from processors.semantic_index import schedule_reindex_topic, deindex as _si_deindex
                if brief.get("current_status") == "stale":
                    _si_deindex("topic", t["id"])
                else:
                    schedule_reindex_topic(t["id"])
                updated += 1

    summary = {
        "topics": len(topics),
        "staled": staled,
        "deduped": deduped,
        "reconciled": reconciled,
        "reconcile_failed": reconcile_failed,
        "updated": updated,
        "applied": apply,
    }
    if reconcile_failed:
        logger.warning(
            f"[nightly] {reconcile_failed} topic reconcile(s) failed "
            f"(parse/validation/LLM) — see per-topic warnings above"
        )
    try:
        supabase_client.log_action(
            action="knowledge_nightly" if apply else "shadow_nightly",
            details={**summary, "changes": changes[:20]},
            triggered_by="auto",
        )
    except Exception:
        pass
    logger.info(f"[nightly] consolidation complete: {summary}")
    return summary
