# Task / Decision Flow + Sheet⇄DB Reconcile — Research & Finalize Plan (2026-07)

**Purpose:** understand how the task/decision layer + the Sheet⇄DB "source of truth"
reconcile actually work today, why they create mess/noise instead of value, and a
concrete **wire-better / finalize** plan (NOT a rebuild). Commissioned by Eyal after
`/sync` silently reverted his manual task edits.

---

## TL;DR

The design is **right and already best-practice-aligned** — it's ~80% built with
a few unfinished wires and a couple of mess-makers. This is a *finish-and-tighten*
job, exactly as Eyal framed it.

- **DB is the source of truth; the Google Sheet is an *editable generated mirror*** (v2.5 reframe, cut over live 2026-06-09).
- The reconcile uses **field-level column ownership + a per-task snapshot (3-way merge)** — which is precisely what the industry recommends for "spreadsheet people edit + DB as source of truth" ([Stacksync](https://www.stacksync.com/blog/mastering-two-way-sync-key-concepts-and-implementation-strategies), [getint](https://www.getint.io/blog/what-is-data-synchronization), [Baserow SSOT](https://baserow.io/blog/single-source-of-truth)).
- **The single biggest unfinished piece:** the *"propose, don't clobber"* half of the model is unwired — stickiness flags are written but **never read**, and task-change proposals are **consumable but never produced**. Today's new `/sync` proposal-review flow is the perfect consumer to finish this against.

---

## 1. How it works today (the model)

**Ownership map** (`processors/sheets_sync.py:612-638`):

| Sheet column | Owner | On a manual Sheet edit |
|---|---|---|
| Status, Deadline, Priority, Assignee | **Eyal** (action fields) | **Preserved** — pulled to DB, marked "sticky" via snapshot |
| Category | **Eyal** when non-blank | Preserved + canonicalized to the Gantt-area taxonomy |
| Urgency (col K) | **Eyal** | Preserved (simple pull) |
| **Task text (col C), Label (col B)** | **DB** (content) | **OVERWRITTEN, silently** — one-way DB→Sheet |
| id (col J) | system | identity key; never edited |

**Mechanism:** a per-task **snapshot** (`sheet_snapshots` table) records the last-synced
action-field values. On reconcile, `Sheet-now != snapshot` ⇒ Eyal edited ⇒ pull to DB;
otherwise `DB != Sheet` ⇒ refresh the Sheet from DB. Identity is the **col-J UUID**.
`archived` status = the only sanctioned delete (row → Archive tab). Runs **midday (13:00)
+ pre-nightly (02:00)** via the reconcile scheduler (`RECONCILE_ENABLED`), plus on-demand
`/sync` (Telegram) and MCP `sync_from_sheets`.

**Decisions** are effectively **one-way DB→Sheet only** — no reconcile pulls a manual
decision-status edit back to the DB (the only Sheet→DB decision path is dead code).

---

## 2. Why it makes mess & noise (pain points, ranked)

1. **[Eyal's incident] Manual *content* edits are silently reverted.** Editing task text
   or label in the Sheet is overwritten by the DB on the next reconcile — no warning, no
   proposal (`sheets_sync.py:748-751`). Only action fields survive. This is the #1
   "why did my edit vanish" trap.

2. **The "propose, don't clobber" protection is UNWIRED (biggest gap).** `manual_*`
   sticky flags are written but **read by nothing** (0 readers in the codebase). The
   `task_update_proposal` type is **consumed** (`decide_proposal`, the new `/sync` review,
   morning brief) but **never produced** anywhere. So inference/cross-reference can still
   silently change a field Eyal set, and the morning-brief "Task Proposals" section is
   always empty. The safe-two-way-editing promise is half-built.

3. **Edit-after-distribution creates duplicate rows.** `apply_edits` **deletes and
   re-inserts** a meeting's tasks with **new UUIDs** (`approval_flow.py:1800-1816`). The
   Sheet rows from the earlier distribution carry the *old* UUIDs, so reconcile orphans
   them AND re-adds the new tasks as fresh rows → two rows per task. (This is the class
   behind the 43-vs-25 task count we saw on the 07-06 weekly.)

4. **Two comparison engines disagree.** The morning brief uses the legacy
   `compute_sheets_diff` (title+assignee fuzzy match), while `/sync` + the scheduler use
   the UUID-first `reconcile_tasks`. They classify divergence differently → the brief can
   nag about a "sync issue" that `/sync` then reports as already in sync. Noise + double
   maintenance.

5. **Duplicate tasks from extraction accumulate.** Dedup is LLM-confidence-based and
   misses near-duplicates; the system *detects* fuzzy dupes (60% word overlap) and lists
   them in the brief, but there's **no merge action** — manual cleanup only. Re-dropping
   transcripts compounds it.

6. **Decisions are a dead-end signal.** The brief counts decision divergence between
   Sheet and DB, but nothing can apply it (no Sheet→DB decision path). Looks actionable,
   isn't.

7. **Rebuild-on-reject wipes ALL Sheet edits.** Rejecting *any* meeting rebuilds the
   whole Tasks + Decisions sheet from DB (`approval_flow.py:981-989`), discarding any
   un-reconciled Sheet edits across every task — not just the rejected meeting's.

---

## 3. What "wire better / finalize" looks like (prioritized, non-rebuild)

**P0 — stop the silent content-revert (Eyal's incident).** Decide the intent:
   - **(a) Content stays DB-owned** → make it *visible*: protect the Task-text/Label columns
     in the Sheet (Google Sheets protected ranges) or visually mark them "system-owned," so
     Eyal edits only in the lanes that stick. Zero silent reverts.
   - **(b) Let Eyal edit task text too** → add title/label to the snapshot + pull path
     (same mechanism as action fields). Bigger, but makes the sheet fully editable.
   *Recommendation:* (a) now (fast, safe), consider (b) later if he wants free-text editing.

**P1 — finish "propose, don't clobber" (highest value; connects to today's work).**
   Wire inference/cross-reference to **emit a `task_update_proposal`** when it wants to
   change a field Eyal set (read the `manual_*` flags), instead of silently overwriting or
   doing nothing. The consumer already exists — **the new `/sync` proposal review** — so
   these become one-tap decisions. This delivers the safe-two-way-editing promise.

**P2 — kill the duplicate-on-edit.** Change `apply_edits` to **update tasks in place
   (keep UUIDs)** rather than delete+recreate, or reconcile old-UUID Sheet rows on edit.
   Removes the recurring duplicate-row janitorial loop.

**P3 — one comparison engine.** Retire the legacy `compute_sheets_diff` in the morning
   brief; use the `reconcile_tasks` preview for the "sync status" line. One model, less noise.

**P4 — actionable duplicate merge.** Turn duplicate detection into a proposal (via the
   same `/sync` review flow) — "merge these two tasks?" — instead of a passive nudge.

**P5 — decisions: pick a lane.** Either add a Sheet→DB reconcile for decision-status, or
   make the Decisions sheet read-only + drop the dead-end divergence count.

**P6 — scope rebuild-on-reject** to the rejected meeting's rows only.

**P7 — re-enable the live reconcile.** After P0/P1, flip `RECONCILE_SHADOW_MODE=false`
   again (I set it True as a stop-gap when it was reverting Eyal's edits). The reconcile
   itself was a deliberate, working cutover — shadow is only a temporary safety.

---

## 4. Decisions — LOCKED (Eyal, 2026-07-07)

1. **Tasks — everything editable EXCEPT the pure-info columns.** Eyal wants to edit
   any content lane (task text, label, status, deadline, priority, assignee, category,
   urgency); only **Source-meeting (H), Created (I), id (J)** stay system-owned/locked.
   → Adopt **P0b**: extend the snapshot + pull path to task text (C) and label (B), so
   they reconcile "manual-wins-and-sticky" like the action fields; **protect** columns
   H/I/J in the Sheet (Google Sheets protected ranges) so they can't be accidentally
   edited. Requires a small `sheet_snapshots` migration (add title/label to the snapshot
   for 3-way edit detection).

2. **Decisions — editable AND a real rethink.** Eyal: "the decisions sheet + flow is bad
   — I want something more sophisticated that follows, thinks, learns, and
   synthesizes/updates/reorders decisions." So this is NOT just Sheet→DB wiring; it's a
   **decision-intelligence layer**. Direction to explore (its own design pass): treat each
   decision as a **living, threaded object** (like topic threads) with a supersession/
   reversal chain (the `decision_status` active/superseded/reversed + `get_decision_chain`
   substrate already exists); a periodic **synthesis pass** that de-dupes, re-orders by
   status/recency/relevance, and links related decisions; surfaced in the weekly review
   for approval — the sheet becomes a generated *view*, not the workspace.

3. **Sequencing — Eyal's call is "as you think":**
   - **Phase 1 (tasks, do first):** P0b (make text/label editable + lock info cols) →
     P2 (kill duplicate-on-edit: `apply_edits` update-in-place) → P1 (propose-don't-clobber,
     lands on the new `/sync` review) → re-enable the live reconcile (`RECONCILE_SHADOW_MODE=false`).
     Build entirely under shadow (safe) and cut over at the end.
   - **Phase 2 (decisions):** a dedicated research+design pass on the decision-intelligence
     layer, then build.
   - **Phase 3 (noise):** P3 one-engine, P4 one-tap dup merge, P6 scope reject-rebuild.

**Prior design context** (settled, do not re-litigate): DB=SSOT; Sheet=editable mirror;
Gantt is intentionally one-way read-back; `archived`=sanctioned delete; the "tasks
vanished" incidents were local test runs hitting live Google APIs, not the reconcile.

---

## 5. Phase 1 — EXECUTION CHECKLIST (resume here in a fresh session)

> **STATUS 2026-07-07 — BUILT + TESTED on branch `feat/task-reconcile-editable-phase1`
> (NOT yet cut over).** Steps 1-3 are implemented with tests green (full suite: 2674
> passed; the 2 fails are pre-existing/order-flaky, not from this work). Shadow mode is
> still ON, so nothing is live yet. The remaining work is the **cutover** (§5 Step 4):
> run the migration in Supabase, run the backfill, deploy, then flip
> `RECONCILE_SHADOW_MODE=false`. See "Cutover runbook" at the end of this section.
>
> Files added/changed this pass:
> - `scripts/migrate_task_reconcile_editable_content.sql` — snapshot title/label +
>   tasks.manual_title/manual_label (additive, idempotent).
> - `scripts/backfill_snapshot_content.py` — seed snapshot title/label from DB (run once).
> - `services/supabase_client.py` — `upsert_sheet_snapshot(...title,label)`, `get_task()`,
>   `create_task_update_proposal()`, `_MANUAL_FIELDS` += title/label.
> - `processors/sheets_sync.py` — content columns reconcile snapshot-style (was one-way).
> - `services/google_sheets.py` — protect cols H/I/J in `format_task_tracker` (warningOnly).
> - `guardrails/approval_flow.py` — `apply_edits` updates tasks IN PLACE (UUIDs survive).
> - `processors/cross_reference.py` — inference proposes on a sticky field, doesn't clobber.
> - Tests: `test_apply_edits_inplace.py`, `test_task_update_proposal_producer.py`, plus
>   new cases in `test_reconcile_engine.py` / `test_continuity_extraction.py`.

**Read first:** this doc + memory `project_task_decision_flow_finalize_2026_07`.
**Current live state (2026-07-07):** rev `gianluigi-00129-m2p`; everything on `main`
(PRs #61–#64 merged); `RECONCILE_ENABLED=true`, **`RECONCILE_SHADOW_MODE=true`** (my
stop-gap — the reconcile computes but writes nothing). **LEAVE SHADOW ON during the whole
build; flip it off only in the final cutover step.** Build on a feature branch. Never run
tests/scripts that write the live Sheet without the guards (see `KNOWN_ISSUES.md` /
`conftest.py`).

**Step 1 — P0b: make Task text (col C) + Label (col B) editable.**
- Migration: `ALTER TABLE sheet_snapshots ADD COLUMN title text, ADD COLUMN label text;`
  (it's an existing table — RLS already on). Backfill via `scripts/backfill_reconcile_v3.py`
  or a small script so snapshots carry title/label.
- `services/supabase_client.py`: `upsert_sheet_snapshot` / `get_sheet_snapshots` (~:4520-4607)
  store + return title/label ("" → NULL).
- `processors/sheets_sync.py` matched loop (~:700-783): move title/label OUT of the one-way
  `_CONTENT_MAP` overwrite (~:748-751) INTO the snapshot-pull path — i.e. treat them like
  `_ACTION_FIELDS`: `Sheet-now != snapshot` ⇒ pull to DB (`update_task` title/label) + count
  `pulled`; else `DB != Sheet` ⇒ refresh cell. Include title/label in the snapshot write (~:954).
- `services/google_sheets.py`: add protected ranges on cols **H (source_meeting), I (created),
  J (id)** so they can't be hand-edited (Sheets API `addProtectedRange`). Everything else editable.
- Tests: a Sheet title edit is pulled to DB; an untouched title is refreshed from DB; content
  is no longer force-overwritten when Eyal changed it.

**Step 2 — P2: kill duplicate-on-edit.** `guardrails/approval_flow.py` `apply_edits`
(~:1800-1816) currently delete+recreate tasks (new UUIDs) → orphaned Sheet rows + re-adds.
Change to **update existing task rows in place by id** (keep UUIDs); only create/delete for
genuinely added/removed items. Test: edit-after-distribution produces no duplicate rows.

**Step 3 — P1: propose-don't-clobber.** Make inference (`processors/cross_reference.py` and
any field-changing path) **READ the `manual_*` sticky flags** and, when it wants to change a
field Eyal set, **emit a `task_update_proposal`** pending_approval instead of overwriting.
Consumer already exists: `decide_proposal` + the new `/sync` review (`processors/proposal_review.py`).
Test: inference on a sticky field creates a proposal, not a silent change.

**Step 4 — CUTOVER.** Apply migration + backfill; deploy; sanity-check in shadow via
`audit_log` (optionally on a duplicated Sheet); then flip `RECONCILE_SHADOW_MODE=false`
(`gcloud run services update ... --update-env-vars RECONCILE_SHADOW_MODE=false`); verify a
`/sync` round-trip preserves a text edit + an action edit.

### Cutover runbook (exact steps — do these to go live)

1. **Merge the branch** `feat/task-reconcile-editable-phase1` to `main` (PR).
2. **Migration** — paste `scripts/migrate_task_reconcile_editable_content.sql` into the
   Supabase SQL editor and run it. Validate the two new columns exist (queries at the
   bottom of the SQL) and `pytest tests/test_rls_coverage.py` passes.
3. **Deploy** the code (still shadow — writes nothing):
   `gcloud run deploy gianluigi --source . --region europe-west1 ...` (standard deploy line).
4. **Backfill** the snapshots (DB-only, safe): dry-run then apply:
   `python scripts/backfill_snapshot_content.py` → `... --apply`.
5. **Flip shadow OFF** — the actual go-live:
   `gcloud run services update gianluigi --region europe-west1 --update-env-vars RECONCILE_SHADOW_MODE=false`
   (use `--update-env-vars`, never `--set-env-vars`).
6. **Verify** a `/sync` round-trip: edit a Task-text cell + an action cell in the Sheet,
   run `/sync`, confirm both are preserved in the DB (not reverted) and cols H/I/J warn on
   hand-edit. Watch `audit_log` for `reconcile_applied`.

**Phase 2 (separate):** decision-intelligence design pass — see §6 (grounded in the
2026-07-07 code research: model decisions on the topic-thread engine).


*Prior design context (already settled, do not re-litigate):* DB=SSOT; Sheet=editable
mirror; Gantt is intentionally **one-way** read-back; `archived`=sanctioned delete;
the recurring "tasks vanished" incidents were **local test runs hitting live Google APIs**,
not the reconcile. (See `V2.5_STRATEGY.md`, `KNOWN_ISSUES.md`, project memory.)

---

## 6. Phase 2 — Decisions as living knowledge (research finding, 2026-07-07)

Eyal's ask: decisions shouldn't be a static sheet — the system already turns certain
topics into persistent "knowledge" that self-updates weekly (tasks/topics/areas). **Align
the decision rethink to that same engine instead of inventing a new one.** A code deep-dive
(2026-07-07) confirms this is the right move and that most of the substrate already exists.

**The proven engine to reuse — TOPIC THREADS.** A topic thread is a persistent row whose
state is:
- **incrementally self-updated on-event** (per approved meeting) — `update_topic_state`
  (`processors/topic_threading.py:102`), a cheap Haiku merge of prior state + new meeting,
  versioned, fire-and-forget; wired in `guardrails/approval_flow.py:1116-1151`.
- **nightly consolidated** — staleness + fact-dedupe + light reconcile
  (`processors/knowledge_consolidation.py:run_consolidation`).
- **weekly deep-re-synthesized from full history** (Sonnet) —
  `processors/knowledge_synthesis.py:run_weekly_synthesis`.
- **bi-temporally versioned** (merges *close* the loser, never delete),
  **parented to an Area** (`topic_threads.area_id`), and **linked in a typed graph**
  (`knowledge_links`: belongs_to / supersedes / advances / …).
- **structurally changed only via human-approved proposals** — the weekly clustering pass
  emits `topic_merge` / `topic_assign` proposals (`processors/topic_clustering.py`) that
  Eyal approves via `get_proposals`/`decide_proposal` (and now the `/sync` review).
  (Canonical projects = a naming/alias dictionary that normalizes labels; NOT a living
  object — reuse only as the vocabulary. Weekly *review* = a presentation/approval session,
  not the object owner.)

**Decisions already have the hard substrate, none of the behavior.** The `decisions` table
has `decision_status` (active/superseded/reversed), `parent_decision_id`, `superseded_by`,
bi-temporal `valid_to`/`superseded_at`, `last_referenced_at`; `get_decision_chain` walks the
chain; `cross_reference.detect_supersessions` already *detects* supersession and
`transcript_processor._link_decision_chains` sets the parent pointer. **What's missing is
the self-synthesis:** `mark_decision_superseded` exists but is **never called** (status flip
is manual only); there is **no DecisionBrief / brief_json**, no on-approval updater, no
nightly/weekly re-synthesis, and nothing writes decision→decision `supersedes` links.

**Phase 2 build sketch (a "decision thread" = a topic thread whose parent is
`parent_decision_id`, status is `decision_status`, and brief is a `DecisionBrief`):**
1. Add `DecisionBrief`/`brief_json` on `decisions` (mirror `TopicBrief` in
   `models/schemas.py:255`).
2. On approval, run a `update_decision_state` alongside the topic loop
   (`approval_flow.py:1116`) — clone `update_topic_state`.
3. Auto-flip `decision_status` on detected supersession (wire the orphaned
   `mark_decision_superseded` into `_link_decision_chains`) + write a `supersedes`
   knowledge_link.
4. Extend `run_consolidation` / `run_weekly_synthesis` to re-synthesize decision briefs
   (de-dupe, re-order by status/recency/relevance, link related decisions).
5. Surface the synthesized decision view in the **weekly review** for approval — the
   Decisions sheet becomes a generated *view*, not the workspace (kills the dead-end
   Sheet↔DB decision divergence from §2.6).

Highest-value files to read when building Phase 2: `processors/topic_threading.py`
(`update_topic_state`, `_sync_brief_from_state`), `guardrails/approval_flow.py:1116-1151`,
`processors/knowledge_synthesis.py`, `processors/knowledge_consolidation.py`,
`services/supabase_client.py` (`get_decision_chain`, the orphaned `mark_decision_superseded`,
`create_knowledge_link`), and `models/schemas.py:201-300` (`TopicState`/`TopicBrief`/
`KnowledgeLink` shapes to mirror).
