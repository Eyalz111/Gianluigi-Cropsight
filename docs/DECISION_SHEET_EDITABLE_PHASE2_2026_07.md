# Phase 2 — Editable Decisions Sheet (full parity with tasks) — Execution Plan (2026-07)

**Scope (Eyal, 2026-07-11): FULL PARITY WITH TASKS.** The Decisions Google Sheet
becomes an *editable* mirror (his Q4 call — NOT a read-only view): decision text,
label, rationale, confidence, **and status** are hand-editable + sticky, plus a
`decision_update_proposal` producer (inference proposes instead of clobbering) and
`DecisionBrief` groundwork.

This mirrors the Phase 1 editable-tasks work (see
`docs/TASK_DECISION_FLOW_RESEARCH_2026_07.md` §5). The difference: for tasks most of
the plumbing already existed; **for decisions we build the first real Sheet→DB path.**

Substrate map that grounds this plan: dispatched 2026-07-11 (Explore). Key facts:
- Decisions sheet is **A:G, NO id column** (`services/google_sheets.py:268-291`);
  matching is fuzzy text-only (`_decision_key` = `description[:100]`, `sheets_sync.py:47-51`).
- `sheet_snapshots` is **task-shaped** (FK `tasks(id)`, accessors hardcode
  `entity_type='task'`, `supabase_client.py:4656-4718`) but the `entity_type`
  discriminator was built for exactly this reuse (`migrate_phase_v3_reconcile.sql:28-29`).
- **No `manual_*` flags, no `brief_json`, no `updated_at` on decisions.**
- The only Sheet→DB decision code (`apply_sheets_to_db`, `sheets_sync.py:429-517`) is
  **DEAD** (0 production callers); `reconcile_tasks` has no decisions branch.
- `apply_edits` **still delete+recreates decisions** with fresh UUIDs
  (`approval_flow.py:1810-1815`) — the Phase 1 in-place fix was tasks-only.
- Supersession proposals (Phase 2.1) are **already live** behind
  `DECISION_INTELLIGENCE_ENABLED` and write `decision_status` via
  `mark_decision_superseded` (`decision_intelligence.py:87`) — a **second writer** of
  status we must interoperate with (see §Design/Status).

---

## Design decisions (locked)

### Column ownership — Decisions sheet becomes A:H (id added as col H)
| Col | Field | Owner | On manual Sheet edit |
|---|---|---|---|
| A | Label | **Eyal** | pulled + sticky |
| B | Decision text (`description`) | **Eyal** | pulled + sticky |
| C | Rationale | **Eyal** | pulled + sticky |
| D | Confidence | **Eyal** | pulled + sticky |
| E | Source Meeting | system | **protected** |
| F | Date | system | **protected** |
| G | Status (`decision_status`) | **Eyal, monotonic** | pulled + sticky, *with the un-supersede guard* |
| H | id (UUID) | system | **protected** — identity key |

### Status — the two-writer rule (the crux of "full parity")
`decision_status` has two writers: the supersession layer (auto, via proposals) and
a manual Sheet edit. Rule: **supersession is monotonic — Eyal can retire by hand,
but a stale/careless cell can never resurrect a retired decision.**
- DB is `superseded`/`reversed`, sheet cell says `active` → **DB wins** (refresh
  Sheet ← DB); never pull. Prevents an old cached "active" cell from un-superseding.
- Sheet moves a decision *forward* (active → superseded/reversed by hand) → **pull +
  sticky** (Eyal deliberately retiring it). Then also honor it in the chain if a
  `superseded_by` target is implied (out of scope for first cut — just flip status).
- Normalize case: Sheet stores `"Active"`, DB stores `"active"` (`sheets_sync.py:311-314`).

### Identity / matching
UUID-keyed once col H exists. Transitional: rows with a blank id fall back to the
legacy fuzzy `_decision_key` match, and reconcile backfills the id cell. `archived`/
delete semantics: decisions have no Archive tab today — first cut does **not** add
Sheet-driven decision deletion (only content/status edits); deletion stays DB-side.

### Shadow gating
New flag **`DECISION_RECONCILE_ENABLED`** (default **False**) gates the whole
Sheet→DB decision path independently of the task reconcile. Build + deploy dark;
flip on only at cutover. (Do NOT reuse `RECONCILE_SHADOW_MODE` — that's task-scoped.)

---

## Build sequence (3 PRs, all under shadow until cutover)

### PR A — substrate + identity (no behavior change)
1. **Migration** `scripts/migrate_decision_reconcile_editable.sql` (WRITTEN):
   generalizes `sheet_snapshots` (decision_id + description/rationale/confidence/
   decision_status; label reused) and adds decisions `manual_*` + `brief_json` +
   `updated_at`. Additive/idempotent.
2. **Snapshot accessors** (`services/supabase_client.py` ~:4656-4718): make
   `get_sheet_snapshots(entity_type=...)` / `upsert_sheet_snapshot(...)` handle
   `entity_type='decision'` (key on `decision_id`, store the decision columns). Add
   `_MANUAL_FIELDS` decision analog + `mark_decision_field_manual`.
3. **id column on the Decisions sheet**: extend `DECISION_COLUMNS`/headers to A:H
   (`google_sheets.py:268-291`); write `decisions.id` into col H in `rebuild_decisions_sheet`
   (:1077-1085) and `add_decisions_batch_to_sheet` (:1919-1939).
4. **`format_decision_tracker`** (new, mirror `format_task_tracker:2124-2163`):
   protect cols **E, F, H** (warningOnly); call it wherever the Decisions tab is built.
5. **Backfill** `scripts/backfill_decision_snapshots.py` (mirror
   `backfill_snapshot_content.py`): seed a decision snapshot per approved decision.
6. Tests: snapshot round-trip for decisions; id written to col H; protected ranges set.

### PR B — reconcile_decisions + apply_edits in place
7. **`reconcile_decisions(dry_run)`** in `processors/sheets_sync.py` (mirror
   `reconcile_tasks:645`): UUID-keyed match, column-ownership + snapshot 3-way merge,
   the **empty/truncated-read GUARD** (mirror PR #70 `reconcile_aborted_bad_read`),
   the Status monotonic-supersede rule (§Design). Gated by `DECISION_RECONCILE_ENABLED`.
8. **Wire it in** alongside `reconcile_tasks`: reconcile scheduler
   (`schedulers/reconcile_scheduler.py:128-129`), MCP `sync_from_sheets`
   (`mcp_server.py:2607-2611`), Telegram `/sync` (`telegram_bot.py:2034, 3717`).
9. **apply_edits in place for decisions** (`approval_flow.py:1810-1815`): mirror the
   task `_apply_in_place` (:1753-1808) — update decision rows by id, keep UUIDs; only
   create/delete genuinely added/removed. Enrich `create_decision` or use
   `create_decisions_batch` so reconcile-create-from-sheet can set label/rationale/status.
10. Tests: sheet text edit pulled + sticky; untouched cell refreshed from DB; a stale
    "active" cell does NOT un-supersede; edit-after-distribution makes no dup rows.

### PR C — propose-don't-clobber + DecisionBrief groundwork
11. **`decision_update_proposal`** producer: when inference/cross-reference wants to
    change a decision field Eyal set (read `manual_*`), emit a proposal instead of
    overwriting (mirror `create_task_update_proposal` + `cross_reference.py:716-727`).
    Consumer already exists: `decide_proposal` + `/sync` review (`proposal_review.py`) —
    add the new type to `REVIEWABLE_TYPES`.
12. **`DecisionBrief`** (`models/schemas.py`, mirror `TopicBrief:255`) + `brief_json`
    on decisions; `update_decision_state` on approval (clone `update_topic_state`,
    hook next to the topic loop `approval_flow.py:1116`). This is groundwork — the full
    nightly/weekly decision synthesis (research §6 items 4-5) is a SEPARATE later phase.
13. Tests: inference on a sticky decision field creates a proposal, not a silent change.

---

## Cutover runbook (go-live — after all 3 PRs merged)
1. **Merge** `feat/decision-sheet-editable-phase2` to `main`.
2. **Migration** — paste `scripts/migrate_decision_reconcile_editable.sql` into the
   Supabase SQL editor, run, validate (queries at the bottom of the SQL);
   `pytest tests/test_rls_coverage.py` passes.
3. **Deploy** (still shadow — `DECISION_RECONCILE_ENABLED=false`): standard
   `gcloud run deploy gianluigi --source . ...` line.
4. **Backfill** decision snapshots (DB-only, safe — NEVER writes the Sheet):
   `python scripts/backfill_decision_snapshots.py` → `... --apply`.
5. **Flip on**: `gcloud run services update gianluigi --region europe-west1
   --update-env-vars DECISION_RECONCILE_ENABLED=true` (use `--update-env-vars`).
6. **Populate col-H ids on the Sheet from PROD** (not a local script): trigger one
   `rebuild_decisions_sheet` in prod (e.g. an MCP/Telegram admin path or the first
   reconcile), which rewrites the Decisions tab A:H with ids from DB and applies the
   protected ranges. Only after this does the sheet carry the identity keys.
7. **Verify** a `/sync` round-trip: edit a Decision-text cell + a Status cell, run
   `/sync`, confirm both preserved (not reverted); confirm a hand "active" on a
   DB-superseded decision does NOT resurrect it; cols E/F/H warn on hand-edit. Watch
   `audit_log` for the decision reconcile actions.

---

## Guardrails (do not violate)
- **Build entirely dark.** `DECISION_RECONCILE_ENABLED=false` until Step 5 of cutover.
  Deploying PR code before the migration is fine (dark) — but do NOT flip the flag
  before the migration + backfill.
- **Never write the live Sheet from a local test/script** without the conftest guards
  (the "tasks vanished" incident — real Google singletons in pytest). Same risk here.
- **Empty-read guard is mandatory** in `reconcile_decisions` from day one (Phase 1's
  dup incident, PR #70). The Decisions sheet has weaker self-heal than Tasks —
  a bad read must abort, never re-append.
- Supersession layer stays authoritative for the *don't-resurrect* direction.
