# Decision Intelligence — Phase 2 Design (2026-07)

**Purpose:** turn decisions from a static, dead-end sheet into a **living, self-synthesizing
knowledge layer** — one that "follows, thinks, learns, and synthesizes/updates/reorders
decisions" (Eyal, 2026-07-07). This is the Phase 2 of the task/decision flow finalize
(see `TASK_DECISION_FLOW_RESEARCH_2026_07.md` §4.2 and §6).

**Core finding (from a 2026-07-07 code deep-dive):** we should NOT invent a new engine.
The system already has a proven "topic → living knowledge, weekly-synthesized" machine —
**topic threads** — and decisions already carry most of its *substrate* but none of its
*behavior*. Phase 2 = **clone the topic-thread engine for a "decision thread."**

---

## 1. What already exists (don't rebuild)

### The topic-thread engine (the template)
A topic thread is a persistent row whose state is kept alive by three cadences:
- **On-event (per approved meeting):** `processors/topic_threading.py:update_topic_state`
  — a cheap Haiku merge of prior state + the new meeting, versioned, fire-and-forget;
  wired next to the approval in `guardrails/approval_flow.py:1116-1151`.
- **Nightly:** `processors/knowledge_consolidation.py:run_consolidation` — staleness,
  fact-dedupe, light reconcile (honors `KNOWLEDGE_SHADOW_MODE`).
- **Weekly deep re-synthesis:** `processors/knowledge_synthesis.py:run_weekly_synthesis`
  — re-synthesizes each active topic from full history (Sonnet) + refreshes Area briefs.
- **Bi-temporal + graph + approval:** merges *close* the loser (never delete); typed
  `knowledge_links` (belongs_to / supersedes / advances / …); structural changes are
  proposed (`topic_clustering.py` → `topic_merge`/`topic_assign` proposals) and Eyal-approved.

### What decisions have already (the substrate)
`decisions` table columns (built across `migrate_phase9a.sql`, `migrate_v2_phase12.sql`,
`migrate_phase_v25_knowledge.sql`):
- `decision_status` (`active` / `superseded` / `reversed`), `superseded_by` → decisions(id)
- `parent_decision_id` → decisions(id) — the chain pointer
- bi-temporal `valid_from` / `valid_to` / `superseded_at`
- `label` (the topic-threading key), `rationale`, `options_considered`, `confidence`,
  `review_date`, `last_referenced_at`
- `get_decision_chain(id)` (`supabase_client.py`) walks up (parent) + down (children)
- `cross_reference.detect_supersessions` already DETECTS supersession (Sonnet);
  `transcript_processor._link_decision_chains` sets `parent_decision_id`

### What's missing (the behavior — this is Phase 2)
- `mark_decision_superseded` **exists but is never called** — the status flip to
  `superseded`/`reversed` is manual-only today.
- **No `DecisionBrief` / `brief_json`** on decisions, no on-approval updater, no
  nightly/weekly re-synthesis, no decision→decision `supersedes` links written.
- The Decisions **sheet is one-way DB→Sheet and a dead-end** (the brief counts divergence
  that nothing can apply).

---

## 2. The model: a "decision thread"

A decision thread is a topic thread with the roles remapped:

| Topic thread | Decision thread |
|---|---|
| `topic_threads.status` (active/stale/closed) | `decisions.decision_status` (active/superseded/reversed) |
| `parent_topic_id` | `parent_decision_id` |
| merge closes the loser (`valid_to`) | supersession closes the old decision (`superseded_at`/`valid_to`) |
| `TopicBrief` in `brief_json` | **`DecisionBrief`** in a new `brief_json` |
| `area_id` (Area parent) | `area_id` (reuse the same Gantt-area taxonomy) |
| `knowledge_links` belongs_to/advances | `knowledge_links` belongs_to(area) / supersedes(decision→decision) |

A `DecisionBrief` (mirror `TopicBrief` in `models/schemas.py:255`) holds: the current
canonical statement, status, rationale, the supersession chain (what it replaced / what
replaced it), linked tasks/topics, per-fact sensitivity + citations, and a short
"evolution narrative" (how this decision moved over time).

---

## 3. The three cadences (what to build)

1. **On-approval incremental update.** In `approval_flow.py` next to the topic loop
   (~:1116), for each decision the approved meeting touched, call a new
   `update_decision_state(decision_id, meeting_id, ...)` (clone of `update_topic_state`):
   load prior brief → Haiku-merge the new meeting → write `brief_json`, bump version,
   fire-and-forget. Also write `advances`/`belongs_to` knowledge_links (the topic path at
   `topic_threading.py:405-424` already does decision→topic — extend it).

2. **Auto-flip on supersession.** Wire the orphaned `mark_decision_superseded` into
   `_link_decision_chains` (`transcript_processor.py`): when `detect_supersessions` finds
   a new decision that supersedes an old one, in addition to setting `parent_decision_id`,
   flip the old one's `decision_status='superseded'` + `superseded_by` + write a
   `supersedes` knowledge_link (decision→decision). (Keep it conservative /
   high-confidence, and — consistent with I1 — consider surfacing it as a proposal rather
   than an automatic flip; see Open Questions.)

3. **Nightly + weekly synthesis.** Extend `run_consolidation` (nightly: dedupe, staleness,
   light reconcile) and `run_weekly_synthesis` (weekly: full re-synthesis from history) to
   also process decision threads — de-dupe near-identical decisions, re-order by
   status/recency/relevance, link related ones, refresh each `DecisionBrief`. Reuse the
   RAG/citation/sensitivity plumbing already in `knowledge_synthesis.py`.

---

## 4. The surface: sheet becomes a generated view

- The Decisions **sheet becomes read-only / a generated view** of the synthesized
  decision threads (kills the dead-end Sheet↔DB divergence from the research §2.6).
- The **weekly review** (`processors/weekly_review.py` + `weekly_review_session.py`) is
  where the synthesized decision view surfaces for Eyal's approval — same propose→approve
  session that already handles Gantt proposals. Re-ordering / merges / status-flips ride
  the existing `start_weekly_review` → `confirm_weekly_review` flow.
- Interrogation stays on MCP (`get_decisions`, `get_decision_chain`, a new
  `get_decision_thread`/brief tool) — decisions are "a knowledge layer you ask", not a
  spreadsheet you maintain.

---

## 5. Suggested build order (phased, shadow-first like Phase 1)

- **2a — schema + brief (no behavior change):** add `brief_json`/`brief_updated_at` to
  `decisions` (additive migration, RLS already on); define `DecisionBrief` in
  `models/schemas.py`; add `update_decision_state` + `_sync_decision_brief` (clones of the
  topic functions). Ship dormant behind a `DECISION_INTELLIGENCE_ENABLED` flag.
- **2b — on-approval updater:** wire `update_decision_state` into `approval_flow.py`
  (fire-and-forget, flag-gated). Backfill briefs for existing active decisions via a
  one-off script (like `knowledge` cold-start).
- **2c — supersession auto-flip / proposal:** wire `mark_decision_superseded` +
  `supersedes` links into `_link_decision_chains` (behind the flag; proposal vs auto per
  the Open Question below).
- **2d — nightly + weekly synthesis:** extend consolidation + weekly synthesis to decision
  threads; run under `KNOWLEDGE_SHADOW_MODE` first, diff, then cut over.
- **2e — surface:** make the Decisions sheet a generated view + surface the synthesized
  decision set in the weekly review for approval.

Each stage is independently shippable and flag-gated; nothing distributes or restructures
without Eyal's approval (I1 preserved).

---

## 6. Open questions for Eyal (decide before 2c/2e)

1. **Auto-flip vs propose on supersession.** When the system is confident a new decision
   reverses/supersedes an old one, should it flip `decision_status` automatically, or
   surface a one-tap proposal (consistent with "Gianluigi proposes, Eyal approves")?
   *Recommendation: propose* — reuse the `/sync` proposal-review + weekly-review surfaces.
2. **What counts as a "decision" worth threading?** All extracted decisions, or only ones
   above a confidence/importance bar? (Affects synthesis noise.)
3. **Cadence.** Weekly deep re-synthesis (matches topics) or also a lighter mid-week pass?
4. **Sheet fate.** Fully read-only generated view, or keep a narrow editable lane (e.g.
   status) like tasks? *Recommendation: read-only view* — decisions are intelligence, not a
   work surface; editing happens by making a new decision that supersedes.

---

## 7. Highest-value files to read when building

- `processors/topic_threading.py` — `update_topic_state:102`, `_sync_brief_from_state:342`
  (the template updater + brief writer)
- `guardrails/approval_flow.py:1116-1151` — the on-approval trigger to clone
- `processors/knowledge_synthesis.py` — `synthesize_topic_brief:314`,
  `run_weekly_synthesis:535` (deep-synthesis template)
- `processors/knowledge_consolidation.py:run_consolidation:141` (nightly template)
- `services/supabase_client.py` — `get_decision_chain`, `mark_decision_superseded`
  (currently orphaned — wire it), `create_knowledge_link`, `update_decision`
- `processors/cross_reference.py:detect_supersessions:551` +
  `processors/transcript_processor.py:_link_decision_chains:1080`
- `models/schemas.py:201-300` — `TopicState`/`TopicBrief`/`KnowledgeLink` shapes to mirror
  for `DecisionBrief`
