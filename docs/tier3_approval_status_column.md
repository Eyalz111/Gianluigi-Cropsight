# Tier 3 — Architectural Robustness (Design Doc)

**Status:** Original design only. **POST-T1.9 ADDENDUM at bottom** — read it before implementing.
**Created:** 2026-04-08 (during the Approval Flow Robustness session that shipped Tier 1 + Tier 2).
**Updated:** 2026-04-09 — addendum added after T1.9 tombstone pivot, which changes T3.1's value calculus.
**Related:**
- `.claude/plans/kind-forging-dahl.md` — original Tier 1+2+3 plan
- `.claude/plans/tier3_handoff.md` — handoff context for the next session
- `docs/tier3_approval_status_column.md` — this document

---

## Background — Why Tier 3 Exists

The approval flow has had a **two-stage commit architectural flaw**: extraction writes to the DB eagerly at transcript-processing time, while approval was only wired to gate team distribution. Rejection only flipped `meetings.approval_status` without touching the extracted children, so tasks/decisions/embeddings/etc. stayed as orphans.

Tier 1 (shipped 2026-04-08, revision 50) solved this with **cascading reject** — whenever the user hits Reject, `delete_meeting_cascade()` removes every row linked to the meeting and the Sheets get rebuilt from fresh DB state. Tier 2 (shipped 2026-04-08, revision 51) added observability so any regression surfaces in `/status` and the morning brief.

Tier 3 is **defense in depth**: two additional layers that make the same class of bug structurally impossible, even if future code introduces new child tables or manual deletes. It's belt-and-suspenders on top of Tier 1. Not urgent — the leak is closed — but worth doing when there's focused time and test bandwidth.

---

## T3.1 — `approval_status` Column on Extracted Tables

### Problem

Today, the meetings table has `approval_status` (pending / approved / rejected / editing / auto_publishing) but child tables don't. Any read path that joins tasks, decisions, open_questions, or follow_up_meetings can surface content from rejected meetings if Tier 1's cascade hasn't run (e.g., pre-Tier-1 data, manual DB edits, bugs in future reject handlers, or race conditions between extraction and rejection).

### Proposed design

Add an `approval_status TEXT DEFAULT 'pending'` column to:
- `tasks`
- `decisions`
- `open_questions`
- `follow_up_meetings`

**Extraction (`processors/transcript_processor.py`)** writes child rows with `approval_status='pending'` by default.

**Approval path (`guardrails/approval_flow.py process_response()` approve branch)** bulk-updates all children for the meeting to `approval_status='approved'` right before calling `distribute_approved_content()`.

**Rejection path** continues to cascade delete (no change — Tier 1 already handles it). The column serves as a secondary safeguard, not a replacement.

**All read paths** default to filtering `approval_status='approved'`. Pre-approval/pending/rejected data is invisible to:
- MCP tools (`get_tasks`, `get_decisions`, `get_full_status`, etc.)
- Schedulers (morning brief, task reminder, weekly digest, QA)
- Gantt intelligence
- Weekly review
- Topic threading
- Morning brief

An optional `include_pending: bool = False` arg on the query helpers opts in to unfiltered results (for admin debugging and the approval flow itself).

### Migration SQL

```sql
-- scripts/migrate_tier3_approval_status.sql

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'pending';
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'pending';
ALTER TABLE open_questions ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'pending';
ALTER TABLE follow_up_meetings ADD COLUMN IF NOT EXISTS approval_status TEXT DEFAULT 'pending';

-- Backfill: mark all existing children 'approved' to preserve current read behavior
-- (since existing read paths don't filter yet, everything needs to appear approved)
UPDATE tasks SET approval_status = 'approved'
    WHERE approval_status IS NULL OR approval_status = 'pending';
UPDATE decisions SET approval_status = 'approved'
    WHERE approval_status IS NULL OR approval_status = 'pending';
UPDATE open_questions SET approval_status = 'approved'
    WHERE approval_status IS NULL OR approval_status = 'pending';
UPDATE follow_up_meetings SET approval_status = 'approved'
    WHERE approval_status IS NULL OR approval_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_tasks_approval_status ON tasks(approval_status);
CREATE INDEX IF NOT EXISTS idx_decisions_approval_status ON decisions(approval_status);
CREATE INDEX IF NOT EXISTS idx_open_questions_approval_status ON open_questions(approval_status);
CREATE INDEX IF NOT EXISTS idx_follow_up_meetings_approval_status ON follow_up_meetings(approval_status);
```

### Read-site audit (~20 call sites estimated)

Before shipping T3.1, grep the codebase for all queries to these four tables:

```bash
grep -rn '\.table("tasks")\|get_tasks\|list_tasks' services/ processors/ schedulers/ guardrails/ core/ --include="*.py"
grep -rn '\.table("decisions")\|list_decisions\|get_decisions' services/ processors/ schedulers/ guardrails/ core/ --include="*.py"
grep -rn '\.table("open_questions")\|get_open_questions\|list_open_questions' services/ processors/ schedulers/ guardrails/ core/ --include="*.py"
grep -rn '\.table("follow_up_meetings")\|list_follow_up' services/ processors/ schedulers/ guardrails/ core/ --include="*.py"
```

**Each match needs an explicit decision:**
1. Default (filter to `approved`) — morning brief, weekly digest, Gantt intelligence, topic threading, MCP read tools, reminder scheduler, sheets sync source
2. Explicit `include_pending=True` — approval flow itself, QA scheduler (for orphan detection), cleanup script
3. Unchanged with comment — admin-only debugging paths

### Code changes — centralized helpers first

Before updating every call site, add `include_pending` as a parameter to the central helpers in `services/supabase_client.py`:

- `get_tasks(status, assignee, ..., include_pending=False)` — default filter `approval_status='approved'`
- `list_decisions(meeting_id, topic, ..., include_pending=False)` — same
- `get_open_questions(status, ..., include_pending=False)` — same
- `list_follow_up_meetings(..., include_pending=False)` — same

This way most callers need zero changes and just get the right behavior automatically. Only call sites that need unfiltered access (approval flow, QA scheduler, cleanup) need to pass `include_pending=True`.

### Test impact

Estimated 30-40 test updates:
- Any test that mocks `get_tasks` / `list_decisions` / etc. and expects unfiltered results
- New tests for the filtering behavior itself
- Tests for the approval flow's bulk-update step on approve
- Tests for extraction writing with `status='pending'`

### Risks

- **Breaking read paths silently.** If a caller is missed, it suddenly returns 0 results because pre-existing data might not all be `approved` yet.
  - *Mitigation:* Backfill migration sets everything to `approved` first. Future extractions write `pending` and get promoted on approval.
- **Pending data invisible in debug contexts.** Support/debug paths may not see in-flight work.
  - *Mitigation:* Add `include_pending=True` to admin-only MCP tools and `/status` extension.
- **Transaction boundary during approval.** Bulk-updating children from pending→approved must be atomic with the pending_approvals delete or there's a race.
  - *Mitigation:* Wrap in a single transaction, or at minimum do approval-status updates BEFORE deleting `pending_approvals` so a crash leaves the DB in a consistent state.

### Effort estimate

**2-3 days focused work**:
- Half-day: migration SQL + backfill + deploy to staging/dev Supabase
- Half-day: centralized helper updates + immediate test updates
- 1 day: audit + update all read sites + edge-case tests
- Half-day: full regression test + manual verification of approval/reject/edit flows

---

## T3.2 — DB-Level `ON DELETE CASCADE` Foreign Keys

### Problem

`delete_meeting_cascade()` in `services/supabase_client.py:271` is a Python-level loop that deletes from each child table explicitly. This works but has two weaknesses:

1. **New child tables can be added without updating the cascade.** Example: if a future phase adds a `meeting_tags` table with `meeting_id`, the developer must remember to add it to the loop. Tier 1 already missed `topic_thread_mentions` until we fixed it.

2. **Manual row deletes from pgAdmin / Supabase UI** don't trigger the Python cascade at all. If someone deletes a meeting row directly (for any reason — debug, cleanup, typo), all child rows become true orphans.

### Proposed design

Add `REFERENCES meetings(id) ON DELETE CASCADE` foreign key constraints to every table that references `meetings`:

- `tasks.meeting_id`
- `decisions.meeting_id`
- `open_questions.meeting_id`
- `follow_up_meetings.source_meeting_id`
- `task_mentions.meeting_id`
- `entity_mentions.meeting_id`
- `topic_thread_mentions.meeting_id`
- `commitments.meeting_id`
- `embeddings.source_id` (only where `source_type='meeting'`)

With these constraints, `DELETE FROM meetings WHERE id = ?` atomically cascades to every child, regardless of code path. `delete_meeting_cascade()` can be simplified to a single DELETE (keeping the Python wrapper only for pre-delete counting / logging).

### Migration SQL

```sql
-- scripts/migrate_tier3_cascade_fks.sql

-- First, check for existing FK definitions and drop them if they conflict
-- (Supabase UI sometimes creates NO ACTION or RESTRICT FKs by default)

-- tasks
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_meeting_id_fkey;
ALTER TABLE tasks ADD CONSTRAINT tasks_meeting_id_fkey
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- decisions
ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_meeting_id_fkey;
ALTER TABLE decisions ADD CONSTRAINT decisions_meeting_id_fkey
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- open_questions
ALTER TABLE open_questions DROP CONSTRAINT IF EXISTS open_questions_meeting_id_fkey;
ALTER TABLE open_questions ADD CONSTRAINT open_questions_meeting_id_fkey
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- follow_up_meetings
ALTER TABLE follow_up_meetings DROP CONSTRAINT IF EXISTS follow_up_meetings_source_meeting_id_fkey;
ALTER TABLE follow_up_meetings ADD CONSTRAINT follow_up_meetings_source_meeting_id_fkey
    FOREIGN KEY (source_meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- task_mentions
ALTER TABLE task_mentions DROP CONSTRAINT IF EXISTS task_mentions_meeting_id_fkey;
ALTER TABLE task_mentions ADD CONSTRAINT task_mentions_meeting_id_fkey
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- entity_mentions
ALTER TABLE entity_mentions DROP CONSTRAINT IF EXISTS entity_mentions_meeting_id_fkey;
ALTER TABLE entity_mentions ADD CONSTRAINT entity_mentions_meeting_id_fkey
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- topic_thread_mentions
ALTER TABLE topic_thread_mentions DROP CONSTRAINT IF EXISTS topic_thread_mentions_meeting_id_fkey;
ALTER TABLE topic_thread_mentions ADD CONSTRAINT topic_thread_mentions_meeting_id_fkey
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- commitments
ALTER TABLE commitments DROP CONSTRAINT IF EXISTS commitments_meeting_id_fkey;
ALTER TABLE commitments ADD CONSTRAINT commitments_meeting_id_fkey
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE;

-- embeddings — tricky because source_id is polymorphic (meeting OR document)
-- Skip FK on embeddings; keep the Python-level delete for source_id.
```

### Validation plan

Before applying to production:

1. **Snapshot the current schema** via `pg_dump --schema-only` from production Supabase.
2. **Restore into a dev/staging Supabase project**.
3. Run the migration against staging — note any errors about conflicting existing FKs.
4. Verify with a test fixture: insert a meeting + children, delete the meeting, confirm all children are gone.
5. Run the full pytest suite against the staging DB with the new FKs.
6. Re-snapshot and diff with production schema to confirm no unintended side effects.

### Rollback strategy

```sql
-- scripts/rollback_tier3_cascade_fks.sql
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_meeting_id_fkey;
ALTER TABLE tasks ADD CONSTRAINT tasks_meeting_id_fkey
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE NO ACTION;
-- (repeat for each table)
```

Rollback restores the previous `NO ACTION` / `SET NULL` behavior. Any child rows that were cascade-deleted during the forward migration are gone — this is why validation on a staging copy is mandatory.

### Risks

- **Existing FK conflicts.** Supabase may have auto-created FKs with different `ON DELETE` behavior. The migration's `DROP CONSTRAINT IF EXISTS` handles the common case.
- **Unintended cascade during existing deletes.** If any current code paths delete a meeting and rely on children staying (e.g., for audit purposes), those children will now disappear. Audit needed before rollout.
- **Embedding polymorphism.** `embeddings.source_id` is not meeting-only; skip the FK there and keep the Python delete.

### Effort estimate

**1-2 days**:
- Half-day: snapshot production schema + set up staging
- Half-day: migration + validation on staging
- Half-day: production deploy window + smoke test
- Half-day: simplify `delete_meeting_cascade()` in Python + update tests

---

## Execution Priority

Tier 3 can be tackled in any order — T3.1 and T3.2 are independent. If only one is done, **T3.2 (FK CASCADE) is the higher-value one** because:
- Smaller diff (pure SQL + minor Python simplification)
- Tighter risk envelope (schema-level, no read-path changes)
- Catches more failure modes (manual deletes, future child tables, partial cascade bugs)
- Doesn't require auditing 20+ read sites

T3.1 (approval_status column) is better when there's dedicated bandwidth for the read-site audit and regression testing.

**Suggested order:** T3.2 first (low-risk, high-value), then T3.1 when read-path audit time is available.

---

## Success criteria for a future Tier 3 session

- Full pytest suite green (baseline + new tests)
- Manual verification: delete a meeting row from Supabase UI directly — all children are gone automatically (T3.2)
- Manual verification: extract a meeting, observe child rows have `approval_status='pending'`, approve it, verify they flip to `approved` (T3.1)
- Manual verification: morning brief and weekly digest show only approved content (T3.1)
- Rollback plan tested against a staging clone
- Zero production incidents for 1 week post-deploy before declaring "done"

---

## Non-goals for Tier 3

- **Embedding regeneration on edit** — expensive and out of scope. Edits flip `approval_status` on text rows but don't re-embed. Acceptable because embeddings are used for semantic search, not source of truth.
- **Audit table for approval transitions** — nice to have but not required. Audit log already captures approval/reject actions.
- **Soft-delete for meetings** — meetings are still hard-deleted. If we ever need undo, that's a separate feature.
- **Multi-approver workflow** — out of scope. The system is single-approver (Eyal) by design.

---

# POST-T1.9 ADDENDUM (2026-04-09)

**Read this before implementing anything above.**

## What changed since the original design

Between the original Tier 3 design (2026-04-08) and now, the Approval Flow Robustness session shipped several architectural changes that affect Tier 3:

### T1.9 shifted reject from "full cascade delete" to "cascade-clear + tombstone"

- `delete_meeting_cascade(meeting_id, keep_tombstone=False)` now accepts a flag
- When `keep_tombstone=True` (used by the reject path), all child rows are deleted as before, BUT the `meetings` row itself is kept with `approval_status='rejected'` (plus cleared transcript/summary)
- The watcher uses the tombstone to skip re-processing the same file forever (or until the tombstone is manually deleted)

### T1.10 watcher rejection-aware skip logic
`schedulers/transcript_watcher.py:260` now branches on `approval_status`:
- `approved` → skip + cache in dedup set (terminal)
- `rejected` (tombstone) → skip, DO NOT cache (so rename re-triggers)
- `pending/editing/auto_publishing` → skip, don't cache

### RLS defense-in-depth (4 layers)
Completely unrelated to the original Tier 3, but shipped this session. The `get_table_rls_status()` helper function + pytest test + QA scheduler check + CLAUDE.md rule now guard against new tables being created without RLS.

---

## How this affects the original T3.1 (approval_status column)

### The original pitch
> "Child tables can surface content from rejected meetings if Tier 1's cascade hasn't run. Add `approval_status` column + filter read paths."

### Post-T1.9 reality
**Rejected meetings no longer have children at all.** The T1.9 cascade deletes every child row on reject, whether or not `keep_tombstone` is set. So the scenario T3.1 was guarding against ("read path surfaces a task from a rejected meeting") is structurally impossible for rejected data.

T3.1 is still **theoretically valuable** for these scenarios:
1. **Pending/editing states** — between extraction and approval, child rows exist with their parent meeting still in `approval_status='pending'`. Any read path that doesn't filter would show this in-progress data to team views.
2. **Race conditions** — if a read happens mid-cascade, stale children could be visible for milliseconds. Extremely rare.
3. **Manual DB edits** — if someone deletes the meeting tombstone without cascading, orphan children exist. The QA scheduler's `_check_rejected_meetings` catches this daily.
4. **Future regressions** — if a future code path creates children without going through the approval flow, they'd appear as approved data.

### The decision
**The new Tier 3 session should run a grep-based read-site audit and decide:**
- (a) **Full T3.1** — add column + filter every read site (2-3 days of work)
- (b) **Narrow T3.1** — add column + filter only the highest-risk read paths (0.5-1 day)
- (c) **Skip T3.1** — rely on T1.9 + QA scheduler + T3.2 FK CASCADE for defense in depth (0 days)

The grep audit will determine which is proportionate. See `.claude/plans/tier3_handoff.md` for the exact grep commands.

---

## T3.2 is still 100% valuable

The FK CASCADE migration is unaffected by T1.9. It's still the cleanest way to:
- Prevent future child tables from being forgotten in `delete_meeting_cascade`
- Handle manual row deletes from Supabase UI
- Simplify `delete_meeting_cascade()` to a single statement + count wrapper

**Recommendation:** Ship T3.2 regardless of the T3.1 decision.

---

## NEW items discovered during the session

These were not in the original T3 design but are worth considering:

### T3.3 — Tombstone lifecycle management
- Should tombstones auto-expire after N days?
- Does the cleanup script need updates to distinguish tombstones (no children, expected) from orphans (has children, alert)?
- Does `_check_rejected_meetings` in QA scheduler need adjustment? Currently it flags rejected meetings with orphan children as an issue — which is correct for pre-T1.9 data but incorrect if tombstones have 0 children (which is the normal post-T1.9 state).

**Action:** Audit the cleanup script + QA check and update them to reflect the tombstone reality.

### T3.4 — Broken pipe retry on Gmail/Telegram sends
Observed during test 4 (2026-04-09 22:39:52):
```
[gmail] Error sending email: [Errno 32] Broken pipe
[telegram_bot] Error sending message to 8190904141: BrokenPipeError
```

The transcript extraction completed fine, but the approval message send failed. No retry was attempted. The existing Sheets API has `_execute_with_retry()` wrapping it — Gmail and Telegram sends don't have equivalent wrapping.

**Action:** Add retry decorators to:
- `services/gmail.send_message` (or equivalent)
- `services/telegram_bot.send_approval_request` (or the appropriate send method)

**Estimated effort:** 1-2 hours

### T3.5 — `format_task_tracker()` on `add_task` append
Cosmetic regression observed during test 2 (Franciacorta approval):
> "The new tasks are colored and styled like the title"

`rebuild_tasks_sheet()` calls `format_task_tracker()` at the end, which applies proper styling. But `add_task()` (used when approving a meeting) only appends raw values — no formatting reapplied. The new rows inherit weird styling from whatever was at the position.

**Action:** Either
- (a) Call `format_task_tracker()` at the end of `add_task()` (simple but expensive per-task)
- (b) Rebuild the whole sheet at the end of `distribute_approved_content()` (batch fix)
- (c) Explicitly set the format on new rows inline

**Recommendation:** (b) — cleanest, consistent with reject flow which also rebuilds.

### T3.6 — `source_file_path` + drive_file_id dual matching (OPTIONAL)
Current watcher behavior: matches tombstones by filename only. Edge case: if Eyal uploads a new file with the same name as a previously-rejected file, the tombstone matches and the new content gets skipped.

**In practice** this is rare because Tactiq uses date-prefixed filenames. But it's worth documenting.

**Action:** Either
- (a) Add `drive_file_id` column to meetings table, store on create, check on watcher
- (b) Document as known limitation (cleaner)
- (c) Detect via content hash or modification time

**Recommendation:** (b) — document as known limitation. The cost of (a) is schema change + migration + new columns everywhere; the risk is minimal in practice.

### T3.7 — Find-task-row fuzzy match cleanup (OPTIONAL)
`sheets_service.find_task_row(title)` does partial string matching which is brittle. Not causing active bugs today but should be swapped for exact match or task_id-based lookup when we have time.

---

## Revised scope recommendation

Given the session is using Opus 4.6 + 1M context, the following scope is achievable in one focused session (1-2 days):

### Tier 3 (revised) — load-bearing items
1. **T3.2 — FK CASCADE migration** (the main event; schema change + simplification of `delete_meeting_cascade`)
2. **T3.3 — Tombstone lifecycle alignment** (update cleanup script + QA check to distinguish tombstones from orphans)
3. **T3.4 — Gmail/Telegram retry** (known production pain)
4. **T3.5 — Tasks sheet format on approval** (known cosmetic regression)

### Tier 3 (revised) — decide based on grep audit
5. **T3.1 — approval_status column** — scope (none/narrow/full) decided after grep audit

### Tier 3 (revised) — optional / nice-to-have
6. **T3.6 — drive_file_id dual matching** — likely defer to known-issues
7. **T3.7 — find_task_row cleanup** — likely defer

---

## Estimated effort post-T1.9

| Item | Effort |
|---|---|
| T3.2 FK CASCADE migration + cascade simplification | 0.5 day |
| T3.3 Tombstone lifecycle alignment (script + QA) | 0.25 day |
| T3.4 Gmail/Telegram retry | 0.25 day |
| T3.5 Sheet format on approval | 0.25 day |
| T3.1 (narrow) — if audit shows it's needed | 0.5-1 day |
| T3.1 (full) — if audit strongly supports it | 1.5-2 days |
| **Total (narrow T3.1)** | **1.75-2.25 days** |
| **Total (full T3.1)** | **2.75-3.25 days** |

---

## See also

- `.claude/plans/tier3_handoff.md` — concrete handoff doc for the next Opus 4.6 session with grep commands, current file references, and the 4-phase workflow
- `CLAUDE.md` — "MANDATORY: Row Level Security on every new table" section (added 2026-04-09)
- `scripts/migrate_rls_security_v2.sql` — RLS helper function + template (run on Supabase 2026-04-09)
