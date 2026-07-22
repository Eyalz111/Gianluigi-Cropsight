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

# Function words carry no identity — removing them before the token comparison
# is what lets us tell an article/punctuation rewording (a duplicate: "boost THE
# client" ≡ "boost client") apart from a one-content-word change (a DISTINCT
# item: "pilot in Q3" ≠ "pilot in Q4"). Raw token-Jaccard cannot: both differ by
# exactly one token. Comparing CONTENT tokens shrinks the sets so a content-word
# swap is a large Jaccard penalty while a stopword drop is none.
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have in into is it its of on or "
    "that the this to was were will with we our you your they their he she".split()
)


def normalize(text: object) -> str:
    """Lowercase, keep only alphanumeric tokens, single-space-joined. Collapses
    punctuation/whitespace/casing rewordings ('the MVP.' vs 'MVP') to one key."""
    if not text:
        return ""
    return " ".join(_WORD_RE.findall(str(text).lower()))


def _words(text: object) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").lower()))


def _content_tokens(text: object) -> frozenset:
    """Meaning-bearing tokens: all words minus stopwords."""
    return frozenset(w for w in _words(text) if w not in _STOPWORDS)


def _content_str(text: object) -> str:
    """Content tokens joined in ORIGINAL order — the string the char gate runs
    on. A dropped stopword leaves it unchanged (char ratio 1.0), while reordered
    content words score low (so 'Eyal calls Roye' != 'Roye calls Eyal')."""
    return " ".join(w for w in _WORD_RE.findall(str(text or "").lower())
                    if w not in _STOPWORDS)


def jaccard(a: object, b: object) -> float:
    """Token-set overlap |A∩B| / |A∪B| over ALL tokens. 0 when either is empty."""
    aw, bw = _words(a), _words(b)
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw)


def content_jaccard(a: object, b: object) -> float:
    """Token-set overlap over CONTENT tokens (stopwords removed). This is the
    signal is_near_dup gates on — it treats a dropped 'the' as identical but a
    swapped 'Q3'->'Q4' as clearly different."""
    ca, cb = _content_tokens(a), _content_tokens(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / len(ca | cb)


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
    """True when two texts are near-duplicates — same content words, only
    stopword/punctuation/casing reworded.

    Dual-signal and content-token based on purpose: 'Connect with Bar Topper at
    A potential unicorn' vs '...at potential unicorn' clears both (a real
    reword-duplicate), while a single meaning-bearing change — 'pilot in Q3' vs
    'pilot in Q4', 'runway budget' vs 'hiring budget', 'Sara at Banca Intesa' vs
    'Sara's father' — drops the CONTENT-token overlap below the bar and is left
    as a separate row. This is what keeps every destructive path (dedup on
    extraction, the edit reconcile, the self-healing backstop) from silently
    merging two genuinely-distinct items.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ca, cb = _content_tokens(a), _content_tokens(b)
    if not ca or not cb:
        # All-stopword text (rare) — fall back to character similarity alone.
        return char_ratio(a, b) >= char_threshold
    cj = len(ca & cb) / len(ca | cb)
    if cj < token_threshold:
        return False
    # Char gate on the CONTENT strings, not the full text: dropping 'the' leaves
    # the content string identical (ratio 1.0) so short stopword-rewords aren't
    # rejected by length, while a reorder of content words still scores low.
    return SequenceMatcher(None, _content_str(a), _content_str(b)).ratio() >= char_threshold


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

    Matching runs in TWO passes so exact matches always win over fuzzy ones,
    globally — never per-item. Pass 1 claims every item that has an exact index
    or exact normalized text (both honour the secondary guard). Pass 2 fuzzy-
    matches only what's left, against only still-unclaimed rows. This stops an
    early reworded item from fuzzy-claiming a row that a later item would have
    matched exactly (identity swap). A row is claimed at most once; the LLM
    output is de-duped first.

    Empty-section guard: if the LLM returns NOTHING for a section that still has
    rows, that is overwhelmingly an omission/truncation, not an intent to delete
    everything — so preserve the rows rather than wipe them (the blank-summary
    failure class). A real "remove every item" must be done explicitly, not by
    the model dropping a key.
    """
    if not edited_items and old_rows:
        return {"updates": [], "creates": [], "deletes": [], "protected_empty": True}

    old_by_index = {i + 1: r for i, r in enumerate(old_rows)}

    accepted = dedup_llm_output(
        edited_items,
        text_of,
        secondary_of=secondary_of,
        char_threshold=char_threshold,
        token_threshold=token_threshold,
    )

    claimed: set = set()
    matched: dict = {}  # position in `accepted` -> old row

    # --- Pass 1: exact matches only (index, then normalized text) ---
    for i, it in enumerate(accepted):
        orig = None
        idx = index_of(it)
        if isinstance(idx, int) and idx in old_by_index:
            cand = old_by_index[idx]
            cid = id_of(cand)
            if cid and cid not in claimed and _secondary_ok(it, cand, secondary_of):
                orig = cand
        if orig is None:
            nt = normalize(text_of(it))
            orig = next(
                (
                    r for r in old_rows
                    if id_of(r) and id_of(r) not in claimed
                    and normalize(text_of(r)) == nt
                    and _secondary_ok(it, r, secondary_of)
                ),
                None,
            )
        if orig is not None:
            claimed.add(id_of(orig))
            matched[i] = orig

    # --- Pass 2: guarded fuzzy for whatever exact matching left unclaimed ---
    for i, it in enumerate(accepted):
        if i in matched:
            continue
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
        if best is not None:
            claimed.add(id_of(best))
            matched[i] = best

    updates = [(id_of(matched[i]), it) for i, it in enumerate(accepted) if i in matched]
    creates = [it for i, it in enumerate(accepted) if i not in matched]
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
