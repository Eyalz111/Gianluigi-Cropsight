"""Auto-learn new canonical projects from recurring unmatched labels. [2026-07-22]

Phase 10 built this loop and never connected it: `unmatched_labels` had a table,
a writer (`store_unmatched_label`) and a retroactive cleaner
(`_resolve_unmatched_labels`, invoked by `add_canonical_project`) — but the
writer had ZERO callers, so the cleaner always operated on an empty table and
the vocabulary never grew. Meanwhile every unrecognised label spawned a fresh
topic thread, which is how 50 threads accumulated with 46 of them
single-mention.

`resolve_label(capture=True)` now feeds the table. This module closes the loop:
a label seen in enough DISTINCT meetings is PROPOSED as a new canonical project
for Eyal to approve. Never auto-created — a wrong canonical name silently fuses
two real projects, and unpicking that afterwards is expensive.

Mirrors processors/topic_clustering.py: deterministic selection, rate-limited,
de-duped against pending proposals, stored in `pending_approvals`.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# A label must appear in at least this many DISTINCT meetings before it is
# worth proposing. One-off labels are usually a paraphrase, not a project.
_MIN_MEETINGS = 2
_DEFAULT_MAX = 5
_EXPIRY_DAYS = 30
_LOOKBACK_DAYS = 120

PROPOSAL_TYPE = "project_new"
# A recurring label whose work already sits under a project is a TOPIC of it.
# Proposing it as a new project is how the curated 22 would drift back toward
# the 226 free-text labels they replaced. [2026-08-07]
TOPIC_LINK_TYPE = "topic_project_link"
# A label needs a clear home, not a marginal one: below this share of its tasks
# agreeing, it is genuinely cross-cutting and Eyal should see it as a project
# candidate rather than be nudged toward one arbitrary owner.
_HOME_MAJORITY = 0.6


def _existing_keys() -> set[str]:
    """Labels already awaiting a decision, so we don't re-propose weekly."""
    try:
        rows = supabase_client.get_pending_approvals_by_status("pending") or []
    except Exception:
        return set()
    return {
        (r.get("content") or {}).get("key")
        for r in rows
        if r.get("content_type") in (PROPOSAL_TYPE, TOPIC_LINK_TYPE)
        and (r.get("content") or {}).get("key")
    }


def _home_projects(names: list[str]) -> dict:
    """{label.lower() -> {id, name, count}} where a label's tasks clearly live.

    Only returns a home when a clear majority of the label's attached tasks
    agree. A label spread thinly across four projects is cross-cutting, and
    nudging Eyal to file it under whichever one happened to win by a task is
    worse than showing him the new-project proposal.
    """
    if not names:
        return {}
    try:
        rows = (supabase_client.client.table("tasks")
                .select("label,project_id")
                .in_("label", names)
                .not_.is_("project_id", "null")
                .is_("valid_to", "null")
                .limit(2000).execute().data or [])
        projects = {p["id"]: p["name"]
                    for p in supabase_client.get_canonical_projects(status=None)}
    except Exception as e:
        logger.warning(f"[project_learning] home-project lookup failed: {e}")
        return {}

    tally: dict = {}
    for row in rows:
        key = (row.get("label") or "").strip().lower()
        tally.setdefault(key, {})
        pid = row["project_id"]
        tally[key][pid] = tally[key].get(pid, 0) + 1

    out = {}
    for key, counts in tally.items():
        total = sum(counts.values())
        pid, top = max(counts.items(), key=lambda kv: kv[1])
        name = projects.get(pid)
        if name and total and (top / total) >= _HOME_MAJORITY:
            out[key] = {"id": pid, "name": name, "count": top}
    return out


def _store_proposal(content: dict, content_type: str = PROPOSAL_TYPE) -> str:
    pid = f"projprop-{uuid.uuid4()}"
    expires = (datetime.now(timezone.utc) + timedelta(days=_EXPIRY_DAYS)).isoformat()
    supabase_client.create_pending_approval(
        approval_id=pid, content_type=content_type, content=content,
        expires_at=expires
    )
    return pid


def apply_topic_link_proposal(content: dict) -> dict:
    """Approve a topic_project_link — record it and attach the existing tasks."""
    topic = (content or {}).get("topic")
    project_id = (content or {}).get("project_id")
    if not topic or not project_id:
        return {"ok": False, "error": "proposal carries no topic/project"}
    return supabase_client.link_topic_to_project(topic, project_id,
                                                 source="proposal")


def propose_new_projects(max_proposals: int = _DEFAULT_MAX) -> dict:
    """Propose canonical projects for labels that keep recurring.

    Returns a summary dict; never raises (scheduler-safe).
    """
    result = {"scanned": 0, "candidates": 0, "proposed": 0, "labels": []}
    try:
        rows = supabase_client.get_unmatched_labels(days=_LOOKBACK_DAYS)
    except Exception as e:
        logger.warning(f"[project_learning] could not read unmatched_labels: {e}")
        return result
    result["scanned"] = len(rows)
    if not rows:
        return result

    # Group case-insensitively, but keep the most common ORIGINAL spelling as
    # the proposed name — the canonical vocabulary should read the way the team
    # writes it, not lower-cased.
    groups: dict[str, dict] = {}
    for r in rows:
        raw = (r.get("label") or "").strip()
        if not raw:
            continue
        g = groups.setdefault(raw.lower(), {"spellings": {}, "meetings": set(), "titles": set()})
        g["spellings"][raw] = g["spellings"].get(raw, 0) + 1
        if r.get("meeting_id"):
            g["meetings"].add(r["meeting_id"])
        if r.get("meeting_title"):
            g["titles"].add(r["meeting_title"])

    # A label that has since BECOME canonical (Eyal approved it, or an alias was
    # added) must not be re-proposed. Re-check against the live vocabulary.
    try:
        projects = supabase_client.get_canonical_projects(status="active")
    except Exception:
        projects = []

    existing = _existing_keys()
    candidates = []
    for key, g in groups.items():
        if len(g["meetings"]) < _MIN_MEETINGS:
            continue
        name = max(g["spellings"], key=g["spellings"].get)
        if supabase_client.match_label_to_canonical(name, projects=projects):
            continue
        candidates.append((len(g["meetings"]), name, key, sorted(g["titles"])[:4]))

    candidates.sort(reverse=True)
    result["candidates"] = len(candidates)

    # Where does the work under this label ALREADY sit? Since the 2026-08-07
    # backfill every open task carries a project_id, so a recurring label whose
    # tasks all live under one project is a TOPIC of that project — not a new
    # project. Proposing it as one is how the curated list of 22 would drift
    # back toward the 226 free-text labels it replaced, which is why
    # PROJECT_LEARNING_ENABLED has been held off. Deterministic, no LLM: the
    # answer is already in the data.
    home = _home_projects([name for _, name, _, _ in candidates[:max_proposals]])
    result["links"] = []

    for count, name, key, titles in candidates[:max_proposals]:
        if key in existing:
            continue
        target = home.get(name.lower())
        if target:
            content = {
                "proposal_type": TOPIC_LINK_TYPE,
                "topic": name,
                "project_id": target["id"],
                "project_name": target["name"],
                "task_count": target["count"],
                "meeting_count": count,
                "sample_meetings": titles,
                "key": key,
            }
            try:
                _store_proposal(content, content_type=TOPIC_LINK_TYPE)
                existing.add(key)
                result["proposed"] += 1
                result["links"].append(f"{name} -> {target['name']}")
            except Exception as e:
                logger.warning(
                    f"[project_learning] could not store link proposal for {name!r}: {e}")
            continue

        content = {
            "proposal_type": PROPOSAL_TYPE,
            "name": name,
            "meeting_count": count,
            "sample_meetings": titles,
            "key": key,
        }
        try:
            _store_proposal(content)
            existing.add(key)
            result["proposed"] += 1
            result["labels"].append(name)
        except Exception as e:
            logger.warning(f"[project_learning] could not store proposal for {name!r}: {e}")

    if result["proposed"]:
        logger.info(
            f"[project_learning] proposed {result['proposed']} new canonical "
            f"project(s): {result['labels']}"
        )
    return result


def apply_project_proposal(content: dict) -> dict:
    """Approve a project_new proposal — create the canonical project.

    `add_canonical_project` is idempotent and retroactively resolves the
    matching `unmatched_labels` rows, so approving also clears the backlog that
    produced the proposal.
    """
    name = (content or {}).get("name")
    if not name:
        return {"ok": False, "error": "proposal carries no name"}
    project = supabase_client.add_canonical_project(
        name=name,
        description=content.get("description", ""),
        aliases=content.get("aliases") or [],
        area_id=content.get("area_id"),
    )
    if not project:
        return {"ok": False, "error": f"could not create project {name!r}"}
    return {"ok": True, "name": name, "project_id": project.get("id")}
