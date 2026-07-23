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


def _existing_keys() -> set[str]:
    """Labels already awaiting a decision, so we don't re-propose weekly."""
    try:
        rows = supabase_client.get_pending_approvals_by_status("pending") or []
    except Exception:
        return set()
    return {
        (r.get("content") or {}).get("key")
        for r in rows
        if r.get("content_type") == PROPOSAL_TYPE and (r.get("content") or {}).get("key")
    }


def _store_proposal(content: dict) -> str:
    pid = f"projprop-{uuid.uuid4()}"
    expires = (datetime.now(timezone.utc) + timedelta(days=_EXPIRY_DAYS)).isoformat()
    supabase_client.create_pending_approval(
        approval_id=pid, content_type=PROPOSAL_TYPE, content=content, expires_at=expires
    )
    return pid


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

    for count, name, key, titles in candidates[:max_proposals]:
        if key in existing:
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
