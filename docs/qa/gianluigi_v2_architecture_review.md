# Gianluigi v2 Implementation Plan — Architecture Review

**Reviewer:** Claude (Architecture Layer, CropSight Ops Project)  
**Date:** April 1, 2026  
**Document reviewed:** Gianluigi v2 Implementation Plan (Phases 11–13 + Cross-Cutting)  
**For:** Claude Code implementation handoff  
**Status:** Review complete — concerns raised, recommendations inline

---

## Overall Assessment

The plan is well-structured. The workstream decomposition (C = operational maturity, A = meeting continuity, B = data sources) with C shipping first is the correct sequencing — fix what's broken before adding capabilities. The level of specificity (file names, line numbers, exact changes) is excellent for implementation.

However, there are several scope, sequencing, and complexity concerns that should be addressed before or during implementation. Each is flagged below with a severity level and recommendation.

---

## Concern 1: Dropbox Sync (B1/B5) — Defer Entirely

**Severity:** HIGH — scope risk  
**Items affected:** B1 (Dropbox → Drive Full Sync), B5 (Dedup/Conflict Handling)

**Issue:** B1 is the heaviest item in the entire plan: new OAuth integration (Dropbox SDK), new scheduler, new tracking table (`dropbox_drive_sync`), conflict resolution rules, and hash-based change detection. B5 is entirely dependent on B1.

**Question to resolve:** What is the concrete operational pain that Dropbox sync solves? If the issue is "Paolo has some BD files in Dropbox," the simpler solution is asking Paolo to move files to the existing CropSight Ops Drive folder.

**Recommendation:**  
- **Remove B1 and B5 from the v2 plan entirely.**
- Remove the `dropbox_drive_sync` table from `migrate_v2.sql`.
- Remove Dropbox config entries (`DROPBOX_APP_KEY`, `DROPBOX_REFRESH_TOKEN`, etc.) from the plan.
- Re-evaluate after 1 month of v2 usage. If Dropbox sync is truly needed, it becomes a standalone Phase 14 item.
- **Ship B2, B3, B4 as planned** — these are solid incremental improvements.

**Revised B Phase Order:** B4 → B3 → B2 (done)

---

## Concern 2: Meta-Agent Framework (X1) — Simplify to Standalone Script

**Severity:** MEDIUM — premature abstraction  
**Items affected:** X1 (Meta-Agent Framework)

**Issue:** The plan calls for `meta_agents/base_agent.py` (base class with `schedule()`, `run()`, `report()`), `registry.py` (dict-based registry, `list_agents()`, `get_schedule()`), and a runner — all for a single consumer (`qa_agent.py`). This is textbook premature abstraction. Building a framework for one agent constrains future agents to fit patterns that can't be predicted yet.

**Recommendation:**  
- **Build `qa_agent.py` as a standalone scheduled task** — a single file in `schedulers/` or `processors/`, modeled after existing scheduler patterns.
- No base class, no registry, no runner framework.
- If 3+ agents emerge and share clear patterns, extract the framework then.
- The `qa_agent` should: run on a schedule (daily or on-demand), check extraction quality, distribution completeness, scheduler health, data integrity, and output a markdown report to Drive or Telegram.

---

## Concern 3: Sensitivity LLM Classification (C6) — Reduce Scope

**Severity:** MEDIUM — scope creep risk  
**Items affected:** C6 (Sensitivity Logic Deep Dive)

**Issue:** C6 expands from keyword-based classification to: Haiku-powered LLM classifier + meeting-to-child sensitivity propagation + per-item filtering in distribution + Telegram group filtering. That's 4–5 files touched across the codebase. This is warranted only if sensitive data has actually leaked to the group chat or keyword classification has demonstrably failed.

**Recommendation — implement in two tiers:**

**Tier 1 (ship with Phase 11):**
- Add `sensitivity` columns to tasks/decisions/open_questions (already in migration — keep this).
- Implement `propagate_meeting_sensitivity()` — copy meeting-level sensitivity to child items after extraction.
- Add sensitivity check in `distribute_approved_content()` — filter sensitive items from group distribution.
- Keep keyword-based classification as-is.

**Tier 2 (defer to post-Phase 11, only if keywords fail):**
- `classify_sensitivity_llm()` using Haiku.
- Telegram group message filtering at the individual item level.
- Per-item sensitivity override in the approval flow.

**Rationale:** Tier 1 closes the actual risk (sensitive items reaching group chat) with minimal code change. Tier 2 adds sophistication only when there's evidence it's needed.

---

## Concern 4: Continuity-Aware Extraction (A2) — Quality Gate Required

**Severity:** HIGH — regression risk on critical path  
**Items affected:** A2 (Continuity-Aware Extraction)

**Issue:** A2 modifies the transcript extraction prompt (`get_summary_extraction_prompt()`) to include task evolution examples and a new schema field (`existing_task_match`). This is the most critical path in the system — any degradation in extraction quality affects every downstream operation.

**Recommendation — mandatory quality gate:**
1. **Before modifying the prompt:** Run extraction on the full existing test corpus. Save the output as a baseline.
2. **After modifying the prompt:** Run extraction on the same corpus. Compare:
   - Are all previously extracted items still extracted?
   - Are existing_task_match annotations accurate (spot-check 10+)?
   - Is there any new noise or hallucinated matches?
3. **Do not ship A2 if extraction quality regresses on non-matching tasks.** The prompt change could cause the LLM to "over-match" — forcing connections where none exist.
4. The auto-apply feature flag (`CONTINUITY_AUTO_APPLY_ENABLED=False`) is correct. Do not change this default during Phase 12.

---

## Concern 5: Interactive Dedup (A3) — Gate on A2 Proving Itself

**Severity:** MEDIUM — dependency risk  
**Items affected:** A3 (Interactive Dedup)

**Issue:** A3's UX quality depends entirely on A2's confidence scoring being reliable. If A2 produces noisy confidence levels, the interactive dedup flow will surface too many false positives ("Task X looks like Y" when they're unrelated), which trains Eyal to ignore the prompts.

**Recommendation:**
- A3 is correctly placed last in the A-workstream ordering. Keep it there.
- **Additionally gate A3 on A2 running in production for at least 1 week.** Review the `existing_task_match` annotations across real transcripts before building the interactive dedup UX.
- If A2's confidence scoring proves unreliable, redesign A3 before implementation (possibly as a batch review in the weekly review rather than real-time inline prompts).
- Consider making A3 a standalone "Phase 12.5" that ships separately.

---

## Concern 6: Single Migration Script — Split by Workstream

**Severity:** LOW — operational hygiene  
**Items affected:** `scripts/migrate_v2.sql`

**Issue:** The migration adds columns and tables for all three workstreams in a single batch. If Phase 12 or 13 slips (likely, given their complexity), unused columns and tables will sit in production.

**Recommendation:**
- Split into three migration scripts: `migrate_v2_phase11.sql`, `migrate_v2_phase12.sql`, `migrate_v2_phase13.sql`.
- Run each migration immediately before starting the corresponding workstream.
- Since Dropbox sync is recommended for deferral (Concern 1), the `dropbox_drive_sync` table should be removed from the migration entirely.

**Phase 11 migration (run before C-workstream):**
```sql
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'normal';
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'normal';
ALTER TABLE open_questions ADD COLUMN IF NOT EXISTS sensitivity TEXT DEFAULT 'normal';
ALTER TABLE email_scans ADD COLUMN IF NOT EXISTS body_text TEXT;
```

**Phase 12 migration (run before A-workstream):**
```sql
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS last_referenced_at TIMESTAMPTZ;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS parent_decision_id UUID REFERENCES decisions(id);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS spawned_from_decision_id UUID REFERENCES decisions(id);
CREATE TABLE IF NOT EXISTS task_signals (...);
```

**Phase 13 migration (run before B-workstream):**
```sql
ALTER TABLE documents ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT;
```

---

## Concern 7: Morning Brief (C3) — Add Audit Trail

**Severity:** LOW — operational safety  
**Items affected:** C3 (Enable Morning Brief)

**Issue:** C3 removes the approval gate for morning briefs, sending directly to Eyal's Telegram. If there's a bug in `compile_morning_brief()`, malformed or incorrect content goes straight to Eyal with no record.

**Recommendation:**
- Keep the direct-send approach (no approval gate — this is correct for a brief).
- **Add a `pending_approvals` record with `status='auto_sent'`** for audit purposes only. Don't gate distribution on it. This gives you a paper trail in Supabase for debugging without adding friction.
- Alternatively, log the full brief content to a `morning_brief_log` field or a simple log table.

---

## Concern 8: Sheets Sync Conflict Resolution (C7) — Define Who Wins

**Severity:** MEDIUM — missing specification  
**Items affected:** C7 (Sheets On-Demand Sync)

**Issue:** The plan specifies `compute_sheets_diff()` with match by title+assignee composite key, but doesn't define:
- What "modified" means (which fields are compared?)
- Who wins when both DB and Sheets have changed the same task
- How to handle tasks that exist in DB but not in Sheets (deleted in Sheets? or never synced?)

**Recommendation — define conflict rules explicitly:**
- **Sheets wins** for any task/decision that exists in both DB and Sheets with differences. Rationale: Sheets is the user-facing operational artifact; if Eyal or the team edited it there, that's the intended state.
- **DB-only items** (exist in DB, not in Sheets): Flag for review in the diff preview — "These items exist in DB but not in Sheets. Remove from DB?" Don't auto-delete.
- **Sheets-only items** (exist in Sheets, not in DB): Add to DB as new items.
- **"Modified" definition:** Compare status, assignee, deadline, priority. Ignore formatting differences and minor text variations.
- Include these rules in the diff preview so Eyal sees exactly what will change before approving.

---

## Concern 9: Missing Test Count Targets

**Severity:** LOW — scope tracking  
**Items affected:** All phases

**Issue:** The plan references maintaining "1350+ trajectory" but doesn't set per-phase test expectations. Without targets, scope creep is harder to detect.

**Recommendation — expected test contributions:**

| Phase | Items | Expected New Tests |
|-------|-------|--------------------|
| C1–C5 | Bug fixes, config changes, enabling schedulers | +30–50 |
| C6 (Tier 1 only) | Sensitivity propagation + distribution filter | +20–30 |
| C7 | Sheets sync diff + apply | +40–60 |
| C8 | Telegram multi-part fix | +10–15 |
| A1 | Enhanced context gatherer | +20–30 |
| A2 | Continuity-aware extraction | +40–60 (including quality regression tests) |
| A4–A6 | Decision freshness, signals, chain traversal | +30–50 |
| A3 | Interactive dedup (if shipped) | +30–40 |
| B2–B4 | Document versioning, email attachments, body storage | +40–60 |
| **Total** | | **+260–395** |

Target: **1600–1750 tests** after full v2 completion.

---

## Summary of Recommendations

### Remove from plan:
1. **B1 (Dropbox Sync)** — defer entirely, re-evaluate in 1 month
2. **B5 (Dedup/Conflict)** — depends on B1, defer with it
3. **X1 (Meta-Agent Framework)** — replace with standalone `qa_agent.py`

### Reduce scope:
4. **C6 (Sensitivity)** — ship Tier 1 only (propagation + distribution filter, keyword classification stays)

### Add quality gates:
5. **A2 (Continuity Extraction)** — mandatory before/after extraction quality comparison on test corpus
6. **A3 (Interactive Dedup)** — gate on 1 week of A2 in production

### Improve specification:
7. **Migration** — split into per-workstream scripts
8. **C3 (Morning Brief)** — add audit trail record
9. **C7 (Sheets Sync)** — define conflict resolution rules (Sheets wins)
10. **All phases** — add test count targets

### No changes needed (ship as planned):
- C1 (Distribution bug fix)
- C2 (Time-window filters)
- C3 (Morning brief — with audit trail addition)
- C4 (Evening debrief prompt)
- C5 (Watcher intervals)
- C7 (Sheets sync — with conflict rules added)
- C8 (Telegram multi-part fix)
- A1 (Enhanced context gatherer)
- A4 (Decision freshness)
- A5 (Signal-based task completion)
- A6 (Decision chain traversal)
- B2 (Document ingestion architecture)
- B3 (Email attachment persistence)
- B4 (Full email body storage)
- X2 (Skill packaging docs)

---

## Revised Phase Ordering

```
Phase 11 (Workstream C): C1 → C2 → C5 → C3 → C4 → C8 → C6 (Tier 1) → C7
  ↓ (deploy + 1 week live usage)
Phase 12 (Workstream A): A1 → A2 → A4 → A5 → A6 → [1 week gate] → A3
  ↓ (deploy + usage)
Phase 13 (Workstream B): B4 → B3 → B2
  ↓
QA Agent: Standalone scheduled task (replaces X1 framework)
```

---

*Review produced by Claude (Architecture Layer) for Claude Code handoff. April 1, 2026.*
