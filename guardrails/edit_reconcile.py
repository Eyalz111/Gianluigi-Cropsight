"""Robust reconciliation of LLM-edited meeting children back onto their DB rows.

WHY THIS EXISTS
---------------
When Eyal edits a pending meeting summary, `guardrails/approval_flow.apply_edits`
asks an LLM to RE-EMIT the meeting's full task / decision / open-question /
follow-up lists, then matches each returned item back to its existing DB row so
the edit updates IN PLACE (preserving the row's UUID — the Sheet reconcile and
mention/threading tables key on it). The original matcher was BYTE-EXACT on the
title, so a reworded or re-emitted item slipped the match and was inserted as a
NEW row -> duplicates. Every edit added another layer; two edits on the
2026-07-06 weekly left 43 task rows for a 24-task meeting, and duplicated
summaries reached the team (the "double-extraction" incident — actually an
apply_edits matching bug, confirmed via the audit log: batch-2 always landed
within ~2 min of an `approval_status_editing` event).

THE APPROACH (defense in depth, precision-first)
-------------------------------------------------
A record-linkage cascade that keeps byte-exact as the high-precision tier and
adds guarded fuzzy matching only as a fallback, so a reworded KEPT item updates
in place while two genuinely-distinct items are NEVER merged:

    1. exact 1-based index the LLM echoed
    2. exact normalized text (byte-exact after normalize)   <- precision tier
    3. guarded fuzzy: dual-signal (char-ratio AND token-Jaccard both clear a
       high bar; optional secondary key, e.g. assignee, must agree)

plus an idempotent de-dup of the LLM's OWN output (byte-exact then fuzzy) so an
"emitted twice, reworded" item collapses to one before anything is written.

All functions here are PURE (no I/O) so they unit-test in isolation; the DB
writes live in approval_flow. `find_duplicate_groups` also powers the
post-edit self-healing backstop, and `dedup_within` powers extraction-time
de-dup of a single run's output.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Callable

# Defaults are conservative on purpose (dual-signal, high bar) so the fuzzy tier
# only ever merges near-identical text. approval_flow passes the tunable values
# from config.settings; these keep the module usable/testable standalone.
DEFAULT_CHAR_THRESHOLD = 0.88
DEFAULT_TOKEN_THRESHOLD = 0.75

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize(text: object) -> str:
    """Lowercase, keep only alphanumeric tokens, single-space-joined. Collapses
    punctuation/whitespace/casing rewordings ('the MVP.' vs 'MVP') to one key."""
    if not text:
        return ""
    return " ".join(_WORD_RE.findall(str(text).lower()))


def _words(text: object) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").lower()))


def jaccard(a: object, b: object) -> float:
    """Token-set overlap |A∩B| / |A∪B|. 0 when either side has no tokens."""
    aw, bw = _words(a), _words(b)
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw)


def char_ratio(a: object, b: object) -> float:
    """difflib character-level similarity of the NORMALIZED strings (0..1)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def is_near_dup(
    a: object,
    b: object,
    char_threshold: float = DEFAULT_CHAR_THRESHOLD,
    token_threshold: float = DEFAULT_TOKEN_THRESHOLD,
) -> bool:
    """True when two texts are near-duplicates by BOTH signals.

    Dual-signal (char AND token) is deliberately high-precision: a reworded
    variant ('at a potential unicorn' vs 'at potential unicorn') clears both,
    while two distinct items that merely share words ('Follow up with Sara at
    Banca Intesa' vs 'Follow up with Sara's father') fail at least one and are
    left as separate rows.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return (
        char_ratio(a, b) >= char_threshold
        and jaccard(a, b) >= token_threshold
    )


def _similarity(a: object, b: object) -> float:
    """Combined score used only to pick the BEST candidate among several."""
    return char_ratio(a, b) + jaccard(a, b)


def _secondary_ok(
    it: dict,
    row: dict,
    secondary_of: Callable[[dict], object] | None,
) -> bool:
    """Guard the fuzzy tier with a secondary key (e.g. task assignee). Enforced
    ONLY when both sides carry a value — the edit LLM routinely omits assignee,
    and a blank must not block an otherwise-strong text match."""
    if secondary_of is None:
        return True
    s1, s2 = normalize(secondary_of(it)), normalize(secondary_of(row))
    return not (s1 and s2) or s1 == s2


def dedup_llm_output(
    items: list[dict],
    text_of: Callable[[dict], object],
    *,
    secondary_of: Callable[[dict], object] | None = None,
    char_threshold: float = DEFAULT_CHAR_THRESHOLD,
    token_threshold: float = DEFAULT_TOKEN_THRESHOLD,
) -> list[dict]:
    """Drop near-duplicate repeats WITHIN one list, keeping the first occurrence.

    An item is a duplicate of an already-kept one when their text matches
    (exact-normalized OR guarded-fuzzy) AND the secondary key is compatible
    (`_secondary_ok`: equal, or blank on either side). Crucially the EXACT pass
    honours the secondary guard too — two same-title tasks with DIFFERENT
    assignees (an Eyal+Roye split) are NOT duplicates and must both survive.
    Items whose text normalizes to empty are kept untouched.
    """
    kept: list[dict] = []
    for it in items:
        t = text_of(it)
        if not normalize(t):
            kept.append(it)
            continue
        is_dup = any(
            (
                normalize(t) == normalize(text_of(k))
                or is_near_dup(t, text_of(k), char_threshold, token_threshold)
            )
            and _secondary_ok(it, k, secondary_of)
            for k in kept
        )
        if not is_dup:
            kept.append(it)
    return kept


# Backwards-friendly alias — extraction-time de-dup reads better as this name.
dedup_within = dedup_llm_output


def reconcile_children(
    old_rows: list[dict],
    edited_items: list[dict],
    text_of: Callable[[dict], object],
    *,
    index_of: Callable[[dict], object] = lambda it: it.get("index"),
    id_of: Callable[[dict], object] = lambda r: r.get("id"),
    secondary_of: Callable[[dict], object] | None = None,
    char_threshold: float = DEFAULT_CHAR_THRESHOLD,
    token_threshold: float = DEFAULT_TOKEN_THRESHOLD,
) -> dict:
    """Map the LLM's edited items onto existing rows.

    Returns a plan the caller applies with type-specific DB calls:
        {
          "updates": [(old_id, edited_item), ...],  # update these rows IN PLACE
          "creates": [edited_item, ...],            # genuinely new rows
          "deletes": [old_id, ...],                 # rows the edit removed
        }

    Cascade per item (first hit wins): exact index -> exact normalized text ->
    guarded fuzzy. A row is claimed at most once. The LLM output is de-duped
    first, so 'emitted twice (once matching, once reworded)' can no longer
    produce one in-place update PLUS one spurious create.
    """
    old_by_index = {i + 1: r for i, r in enumerate(old_rows)}

    accepted = dedup_llm_output(
        edited_items,
        text_of,
        secondary_of=secondary_of,
        char_threshold=char_threshold,
        token_threshold=token_threshold,
    )

    claimed: set = set()
    updates: list[tuple] = []
    creates: list[dict] = []

    for it in accepted:
        orig = None

        # Tier 1 — exact index the LLM echoed.
        idx = index_of(it)
        if isinstance(idx, int) and idx in old_by_index:
            cand = old_by_index[idx]
            cid = id_of(cand)
            if cid and cid not in claimed:
                orig = cand

        # Tier 2 — exact normalized text, first unclaimed row.
        if orig is None:
            nt = normalize(text_of(it))
            orig = next(
                (
                    r for r in old_rows
                    if id_of(r) and id_of(r) not in claimed
                    and normalize(text_of(r)) == nt
                ),
                None,
            )

        # Tier 3 — guarded fuzzy, best dual-signal unclaimed match.
        if orig is None:
            best, best_score = None, 0.0
            for r in old_rows:
                rid = id_of(r)
                if not rid or rid in claimed:
                    continue
                if not _secondary_ok(it, r, secondary_of):
                    continue
                if is_near_dup(text_of(it), text_of(r), char_threshold, token_threshold):
                    sc = _similarity(text_of(it), text_of(r))
                    if sc > best_score:
                        best, best_score = r, sc
            orig = best

        if orig is not None:
            claimed.add(id_of(orig))
            updates.append((id_of(orig), it))
        else:
            creates.append(it)

    deletes = [id_of(r) for r in old_rows if id_of(r) and id_of(r) not in claimed]
    return {"updates": updates, "creates": creates, "deletes": deletes}


def find_duplicate_groups(
    rows: list[dict],
    text_of: Callable[[dict], object],
    *,
    secondary_of: Callable[[dict], object] | None = None,
    char_threshold: float = DEFAULT_CHAR_THRESHOLD,
    token_threshold: float = DEFAULT_TOKEN_THRESHOLD,
) -> list[list[dict]]:
    """Cluster rows into near-duplicate groups (only groups of size > 1 returned).

    Input order is preserved inside each group, so a caller that passes rows
    sorted oldest-first can keep group[0] (the original identity) and collapse
    the rest. Powers the post-edit self-healing backstop.
    """
    groups: list[list[dict]] = []
    used: set[int] = set()
    for i, r in enumerate(rows):
        if i in used:
            continue
        group = [r]
        used.add(i)
        for j in range(i + 1, len(rows)):
            if j in used:
                continue
            if is_near_dup(text_of(r), text_of(rows[j]), char_threshold, token_threshold) \
                    and _secondary_ok(r, rows[j], secondary_of):
                group.append(rows[j])
                used.add(j)
        if len(group) > 1:
            groups.append(group)
    return groups
