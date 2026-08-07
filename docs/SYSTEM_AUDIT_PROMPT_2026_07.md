# Whole-System Audit — Gianluigi (July 2026, office-manager readiness)

> **Why this exists (July 2026):** A real human **office manager** is about to start
> interacting with Gianluigi. **Eyal remains the sole approver/modifier** — she does not
> approve, distribute, or change anything; she *interacts* (receives messages from it,
> asks it things) and exactly how is still TBD. But the system was designed *Eyal-only*,
> so even a read/recipient second human changes the exposure surface. This audit does
> three things, in this order of importance:
> 1. **Close the loop on issues already raised but not fully fixed** — re-verify the
>    June-2026 audit's open fix-PRs (esp. **PR-D MCP auth** and **PR-B idle-wake retry**,
>    both flagged unresolved) and this session's deferred gaps. Don't only hunt new bugs;
>    account for the old ones first.
> 2. **Map every single-actor assumption** ("the only human is the CEO") so Eyal can
>    safely decide how the office manager plugs in — with the invariant that she can
>    never approve/modify and never receives content above her clearance.
> 3. **Harden generally** + fold in the layers built since June (semantic index,
>    decision synthesis, editable Decisions sheet Phase 2, distribution groups).
>
> Baseline: rev `gianluigi-00156-4rp`.
>
> **How to run — pick ONE vehicle:**
>
> - **(A) Multi-session paste (proven June model, runs on your tokens).** Three fresh
>   Claude Code sessions, each `/model opus`:
>   - *Session 1* — paste: *"Run the system audit in docs/SYSTEM_AUDIT_PROMPT_2026_07.md,
>     phases 0–2. After each phase: save the findings file, then compact before the next."*
>   - *Session 2* — paste: *"Continue the audit in docs/SYSTEM_AUDIT_PROMPT_2026_07.md,
>     phases 3–5. Resume from SYSTEM_AUDIT_2026_07.md. Save + compact after each phase."*
>   - *Session 3* — paste: *"Finish the audit in docs/SYSTEM_AUDIT_PROMPT_2026_07.md,
>     phases 6–7. Resume from SYSTEM_AUDIT_2026_07.md; end with the executive summary."*
> - **(B) Single multi-agent Workflow (fastest, most thorough single shot).** Tell
>   Claude: *"Run the audit in docs/SYSTEM_AUDIT_PROMPT_2026_07.md as a workflow —
>   parallel finders per phase, adversarially verify each finding, synthesize."* This
>   fans out Sonnet finders across all phases at once, verifies, and returns one ranked
>   findings file. Best if you want it hands-off and done in one pass.
> - **(C) `/code-review ultra <PR#>`** — only for a focused adversarial pass on ONE
>   specific PR/change set. Wrong shape for a whole-system audit (it reviews a diff).
>
> **The findings file (`SYSTEM_AUDIT_2026_07.md`) is the only state that matters** —
> compaction/session changes lose nothing. If a usage warning appears mid-phase,
> finish the phase, save, roll the rest into the next session.
>
> **Phases are ordered by readiness priority — P0 first.** If budget runs short, a
> completed P0 (phases 0–3) already covers the leak/data-loss/second-actor risks.

---

## Mission

Perform a structured, whole-system audit of Gianluigi (CropSight's AI operations
assistant), tuned for **readiness to onboard a second human user (an office manager)**.
This is NOT a diff review: assume any file may contain a latent bug regardless of when
it was written. Find correctness bugs, data-loss risks, **cross-tier data leaks**,
**access-control gaps for a second actor**, silent-failure paths, and invariant
violations — ranked by real-world impact on a production system that manages a startup
CEO's tasks, meetings, and *sensitive* business data, now shared with an assistant.

## Hard rules

1. **READ-ONLY against production.** Never run `--apply` scripts, never call write-MCP
   tools, never deploy, never modify the live Sheet/DB/Telegram. `pytest` is allowed
   (conftest guard blocks Google/Telegram; skip live-DB tier3 with `-k "not tier3"`
   unless specifically auditing them).
2. **Findings first, fixes later.** Do not change source code during the audit. Every
   proposed fix goes in the findings file for Eyal's approval (house rule: Gianluigi
   proposes, Eyal approves — same applies to you).
3. **Cost discipline:** main loop is Opus — use it for verification and judgment. ALL
   breadth scanning is done by **Sonnet** subagents (Haiku for mechanical greps/
   inventories). Never spawn an Opus subagent. Keep raw file dumps out of the main
   context — subagents return conclusions; findings live in the file.
4. **The findings file is the single source of truth and the session handoff.** Update
   `SYSTEM_AUDIT_2026_07.md` (repo root) after EVERY phase — never hold findings only
   in context.

## Verification bar

A finding makes the file only with a **concrete failure scenario** (input/state/timing
→ wrong outcome). For every candidate a subagent reports, verify it in the main loop by
reading the actual code path before recording. Survivors get: severity
(CRITICAL/HIGH/MEDIUM/LOW), file:line, scenario, suggested fix, effort (S/M/L). Refuted
candidates get one line in a "Refuted" appendix so future audits don't re-chase them.

Severity calibration:
- **CRITICAL** — data loss/corruption, sensitive-data leak across tiers, prod outage,
  approval-gate bypass, money loss, **a second actor seeing/doing something they must not**.
- **HIGH** — silent wrong behavior Eyal or the office manager would act on (wrong
  deadline/status/recipient), unrecoverable-without-manual-repair states, realistic
  security weakness.
- **MEDIUM** — degraded/noisy behavior, self-healing inconsistencies, missing alerting.
- **LOW** — cleanup, dead code, drift risk, docs.

## System invariants (check EVERY phase; a violation is automatically ≥ HIGH)

From CLAUDE.md and hard-won incidents:

- **I1 Approval gate**: nothing distributes to the team, writes the Gantt board, or
  surfaces pending-approval content without Eyal's explicit approval. Default reads
  filter to `approval_status='approved'` (the 4 central read helpers).
- **I2 All team comms route through Eyal** — no direct nudging of Roye/Paolo/Yoram.
- **I3 Sensitivity follows data**: classified at ingestion (FOUNDERS/CEO/TEAM/PUBLIC),
  propagated to children, enforced at retrieval. Any output path must tier-filter.
- **I4 Restart-safety**: anything mid-flow must be reconstructable from DB rows alone.
  In-memory-only state pending across an await of external I/O is a bug.
- **I5 supabase_client methods are SYNC** — `await supabase_client.x()` is a bug;
  conversely long sync DB calls inside the event loop can starve schedulers.
- **I6 Date safety**: every human-entered date goes through `core/dates.parse_human_date`
  (day-first); a provided-but-unparseable date must NEVER become a NULL overwrite or an
  invented date.
- **I7 Category taxonomy**: `tasks.category` canonicalized via `resolve_category` at the
  supabase choke points; no caller bypasses.
- **I8 Sheet writes**: never treat a missing row as a delete; archived is the only
  sanctioned removal; col-J/col-H UUID identity is sacred; layout comes from
  `TASK_COLUMNS` / decision headers.
- **I9 RLS on every public table** (tests/test_rls_coverage.py + daily QA enforce).
- **I10 All LLM calls go through core/llm.py** with the model-tier policy; every
  call_site labeled for cost tracking.
- **I11 Asia/Jerusalem for all scheduling**; UTC math drifts the briefs.
- **I12 Broken-pipe resilience**: Google API call sites route through the retry wrapper.
- **I13 Heartbeats**: every scheduler loop ticks `core/health_monitor`.
- **I14 Tests hermetic**: no real Google/Telegram, no un-cleaned live-DB writes, no time
  bombs.
- **I15 Semantic-index freshness (NEW):** every decision/topic **write** path
  (create/edit/merge/supersede/reject/close, via approval, proposal-apply, `apply_edits`,
  the Sheet reconcile, or weekly synthesis) must index/deindex/reindex. A stale embedding
  (old text still searchable) or an orphaned embedding (retired entity still surfacing) is
  a quality bug and — if the entity is FOUNDERS/CEO tier — a leak. Enumerate EVERY write
  path and confirm a hook.
- **I16 Single-actor assumption (NEW — the office-manager lens):** the system was built
  assuming the only human is Eyal (Telegram DM `8190904141`; MCP = one shared bearer
  token). **Eyal stays the sole approver/modifier** — the invariant to PROTECT is that the
  office manager can never approve, distribute, or modify, even if given some access. Three
  exposures to map: (1) she will **receive** messages/outputs — any path that could send her
  content must tier-filter to *her* (un-cleared) level, not Eyal's CEO level (I3 with a new,
  lower-clearance recipient); (2) the MCP bearer token is shared and all-or-nothing — whoever
  holds it gets **full CEO read + every write tool**, so if she or her tooling ever holds it,
  that's total access (this is the still-open June P3-01); (3) identity/approval guards must
  gate on *Eyal specifically*, not "a known human." For every surface ask: *if it's the
  office manager on the other end, what can she see, what can she trigger, and is Eyal still
  the only one who can approve?*

## Phases (ordered by readiness priority — P0 = phases 0–3)

For each phase: spawn 2–4 **Sonnet** subagents in parallel with the angle prompts, verify
candidates in the main loop, record, update the file, commit nothing.

### Phase 0 — Bootstrap (once)
Create `SYSTEM_AUDIT_2026_07.md` with: phase checklist, empty findings table, refuted
appendix, **and a "Regression ledger" table seeded from the June audit's fix-PR list
(A–G) — see "Prior open items to RE-VERIFY" below.** Spawn one Haiku subagent for a module
inventory (file, LOC, one-line purpose) across processors/, services/, schedulers/,
guardrails/, core/. Read `SYSTEM_AUDIT_2026_06.md` (the prior findings + fix-PR sequencing),
`KNOWN_ISSUES.md`, and the latest memory notes (esp. semantic-index, decision-synthesis,
Phase-2 editable decisions, distribution-groups) so known items aren't re-discovered — they
are RE-VERIFIED for current status, not re-found from scratch.

### Phase 1 — [P0] Single-actor exposure + protect Eyal-only approval (the office manager)
The dimension the June audit never had. Reframe from the corrected role: **Eyal stays the
only approver/modifier; she interacts and receives.** So this phase protects that invariant
and maps exposure — it does NOT assume she's a co-approver. Files: `guardrails/mcp_auth.py`,
`services/mcp_server.py` (46 tools — split read vs write tools), `services/telegram_bot.py`
(identity/session-stack + who-receives-what), `guardrails/approval_flow.py`, `config/team.py`,
`config/settings.py`. Angles:
- (a) **Re-verify the still-open door first (June P3-01 / PR-D):** is the MCP write surface
  still reachable tokenless / URL-only? Confirm the *current* state of `mcp_auth` (bearer token
  enforced? ingress restricted? rate-limit only?). This is the crux of "if she ever gets the
  URL/token." Report shipped-vs-still-open, don't assume.
- (b) **Protect Eyal-only approval (I1/I2/I16):** enumerate every approve/distribute/modify
  entry point (Telegram callbacks, email approval-reply, MCP write tools, `/sync` apply) and
  confirm each gates on *Eyal's identity specifically*, not "a known/team human." A path that a
  second human could trigger to approve or distribute is CRITICAL.
- (c) **Recipient tier-filtering (I3, new lower-clearance reader):** trace every path that
  could send an output TO the office manager (or that she could read). Does it filter to her
  un-cleared level, or does it assume the reader is always Eyal (CEO-level)? A shared token or
  a new Telegram recipient that returns FOUNDERS/CEO content is a leak.
- (d) **Audit trail:** does `log_action` / mcp_auth audit capture *which* actor did a read/write,
  enough to reconstruct "who did this" once two people can touch the system?

### Phase 2 — [P0] Approval gate, distribution & data-loss integrity
`guardrails/approval_flow.py` (the control point — enumerate EVERY write path that
distributes/promotes and confirm the gate), distribution groups + custom picker
(over-share surface), `processors/sheets_sync.py` reconcile engines (tasks AND decisions),
`services/supabase_client.py` delete/cascade + empty-read guards. Angles:
- (a) **Distribution integrity** (the double-email class): can a summary go out twice, or
  with the wrong recipients/tiers? Idempotency of Approve; distribute-from-DB vs pending
  content; stale-card guards.
- (b) **Reconcile edge matrix** (tasks + decisions): snapshot vs sheet vs DB — deletions,
  clears, partial API failures mid-apply, row shifts, empty/truncated reads (the sheet-wipe
  and dup-UUID incident classes). Does a missing row ever become a delete (I8)?
- (c) **Approval-gate completeness (I1):** any promote/distribute path that skips the gate.

### Phase 3 — [P0] Sensitivity / tier enforcement across ALL outputs (incl. semantic index)
Trace EVERY externally-visible output to its audience and confirm tier-filtering (I3). New
surfaces since June: `processors/semantic_index.py` + `search_memory` + `find_relevant_*`
+ the make-prep "Where Key Topics Stand" section; also morning_brief, weekly_digest,
weekly_pulse, debrief, meeting_prep, deal_intelligence, intelligence_signal. Angles:
- (a) **Every read path tier-filters** — especially the new semantic surfaces (the decision
  semantic branch historically had NO filter; confirm the RPC-returned `sensitivity` is
  actually applied everywhere, not just where it was added this session).
- (b) **Semantic-index freshness (I15):** enumerate every decision/topic write path; confirm
  index/deindex/reindex hook. Look for the *unhooked* path (this is how the reconcile gap was
  found — assume there's another, e.g. quick_inject, rename_thread, knowledge_consolidation
  stale-status, direct create_task→decision).
- (c) **Sensitivity propagation:** classified at ingestion → carried to children → to the
  embedding's top-level column. Any processor that creates/derives content without carrying
  sensitivity forward.

### Phase 4 — [P1] processors/ knowledge + outputs (the layers built since June)
`decision_synthesis`, `decision_clustering`, `decision_intelligence`, `knowledge_synthesis`,
`knowledge_consolidation`, `topic_threading`, `topic_clustering`, `meeting_continuity`,
`cross_reference`, `transcript_processor`, `morning_brief`, `weekly_*`, `summary_rich`,
`gantt_*`. Angles: (a) empty/degraded-data rendering (None/[] → crash or nonsense in
Telegram/MCP?); (b) silent excepts that eat failures Eyal should hear about; (c)
proposal-apply correctness (merge/supersede/relate reversibility, monotonic-status rules,
chained-merge winner resolution).

### Phase 5 — [P1] services/ + guardrails/ security
`supabase_client` (4,900+ lines — split between two subagents), `google_sheets`,
`google_drive`, `google_calendar`, `gmail`, `telegram_bot` (callback/session-stack state
machine + restart reconstruction), `mcp_server`, `embeddings`. Guardrails:
`sensitivity_classifier` + propagation, `content_filter`, `gantt_guard`, `inbound_filter`.
Angles: (a) **error-contract consistency** — which methods return [] vs raise vs None, and
which CALLERS misread "[] because the API failed" as "genuinely empty" (the sheet-wipe
class); (b) retry coverage (I12) — Google call sites NOT routed through a retry wrapper;
(c) **prompt-injection surface** — transcripts/emails/documents are UNTRUSTED input flowing
into LLM prompts whose outputs drive writes: can a malicious transcript plant tasks,
exfiltrate FOUNDERS/CEO context into an outbound summary/email, or steer approval text?

### Phase 6 — [P2] schedulers/ + main.py + core/ + config/
All ~14 schedulers: (a) loop-death modes (uncaught exception that kills the while-loop vs
catch-sleep-continue; heartbeat ticked on EVERY path — I13); (b) timezone math (I11) +
fire-once guards — can a restart double-fire a brief/reminder/synthesis/distribution? (c)
startup ordering in main.py (non-critical service fails init → dependent schedulers skip
cleanly?). core/config: `llm.py` (retry/cost/cache, I10 sweep — any Anthropic client use
outside it), agent/router/tool-definition vs handler schema drift, `settings.py` flag
hygiene (orphan flags ON in prod but unused, or referenced-but-undefined), `models/schemas.py`
vs live DB drift. `scripts/`: anything destructive without a dry-run default.

### Phase 7 — Synthesis (Opus, main loop only — no subagents)
Dedup cross-phase findings, re-rank, write the executive summary: **top-10 by risk-adjusted
priority with the office-manager readiness items called out explicitly**, quick wins
(S-effort HIGHs), a proposed fix-PR sequencing (grouped so each PR is independently
testable), a "what we deliberately accept" list, **and the completed Regression ledger
(status of every June fix-PR A–G + this session's deferred gaps).** Any prior item still
OPEN gets re-filed as a current finding with fresh severity (esp. if a second human raises
its risk). End by presenting the summary to Eyal in chat.

## Prior open items to RE-VERIFY (don't re-discover — confirm current status)

The June-2026 audit (78 findings) proposed 7 fix-PRs. Per the fix-rollout notes + memory,
the S-effort HIGH quick-wins and PRs #33–#38 shipped (tier-leak closed, prompt-injection
hardened, channel approval-gate quick-wins), but **PR-B and PR-D were flagged NOT fully
done.** For EACH row below, read the current code and record in the Regression ledger:
shipped / partial / still-open, with file:line evidence — then re-file any open item as a
live finding.

| June PR | What it covered | Why it matters now (office-manager lens) |
|---|---|---|
| **PR-D — MCP auth (P3-01)** ⚠️ flagged open | Unauthenticated MCP write surface: tokenless requests allowed ("authless for Claude.ai"), public Cloud Run ingress, all write tools + unfiltered `get_full_status` behind URL obscurity + rate-limit only. **June's #1 finding.** | **THE crux.** If this is still open, the "what can a second person do" question is already answered: anyone with the URL gets full CEO read + every write. Verify `mcp_auth` current state FIRST (Phase 1a). |
| **PR-B — idle-wake retry coverage (P3-02/03/04/07, P4-06)** ⚠️ flagged open | Only Google *read* helpers route through the broken-pipe retry wrapper; *write* paths silently fail on the first post-idle call. P3-02 (rebuild wipe) + P3-04 (docx email) were quick-win-patched; **P3-03 (Calendar has NO retry wrapper → `[]` == "no meetings")** and P3-07 (bare `.execute()` write sweep) likely remain. | Silent write failures + a Calendar that reports "no meetings" make Gianluigi unreliable exactly when a new person is depending on its messages. |
| **PR-E — reconcile/sheet data-integrity (P1-02/03, P6-04)** | Non-atomic create→UUID-writeback dup (P1-02); `/sync` title+assignee key collision writing an edit to the WRONG task (P1-03); destructive-by-default scripts. | This session hardened reconcile (empty-read guards, dup incident) — verify P1-02/P1-03 are actually closed now, not assumed. |
| **PR-F — scheduler robustness (P4-01/02/03)** | Sleep-until loops never heartbeat (wedged loop invisible for days, I13); hardcoded UTC+2 DST drift (I11); in-memory fire-once double/skip on restart (I4). | The decision-synthesis missed-Sunday-run this session is exactly the fire-once/timezone class — verify. |
| **PR-G — silent-failure surfacing (batch)** | ~15 small "make invisible failures visible" items across outputs + LLM/DB layer. | Lower priority, but each is a place Eyal (or she) wouldn't hear about a failure. |

Also RE-VERIFY this session's **deliberately deferred** semantic-index gaps (I15): topic
`rename_thread` reindex, `knowledge_consolidation` stale-status handling, and any
`quick_inject` / direct `create_task`→decision path that writes a decision/topic without an
index hook. The Sheet-reconcile gap was closed rev 00156; assume there is at least one more.

## Findings file format (`SYSTEM_AUDIT_2026_07.md`)

```
# System Audit — July 2026 (office-manager readiness)
Status: Phase N of 7 complete · last session YYYY-MM-DD · baseline rev gianluigi-00156-4rp
## Phase checklist
- [x] 0 bootstrap  - [ ] 1 access-control … - [ ] 7 synthesis
## Executive summary (written in Phase 7)
## Regression ledger (June fix-PRs A–G + deferred gaps — seeded Phase 0, closed Phase 7)
| Prior item | Was | Current status (shipped/partial/open) | Evidence file:line | Re-filed as |
|-----------|-----|----------------------------------------|--------------------|-------------|
| PR-D MCP auth (P3-01) | open | ? | | |
| PR-B idle-wake retry (P3-03/07) | open | ? | | |
## Findings
| ID | Sev | Area | file:line | Finding | Failure scenario | Suggested fix | Effort |
|----|-----|------|-----------|---------|------------------|---------------|--------|
| P1-01 | CRITICAL | access | guardrails/mcp_auth.py:NN | … | … | … | M |
## Quick wins
## Refuted candidates (one line each — why)
## Module inventory (appendix)
```

## Known context (don't re-discover these)

- **Semantic index (2026-07-14, rev 00154→00156):** decisions+topics embedded; lifecycle
  hooks on approval/edit/proposal-apply/reject/merge/supersede/synthesis/**sheet-reconcile**.
  Phase 3(b) should still hunt for a *remaining* unhooked write path — don't assume complete.
- **Decision synthesis / Phase-2 editable decisions / distribution groups** are all live;
  see MEMORY.md pointers. The Decisions sheet is A:H (col-H UUID), editable+sticky, cols
  E/F/H protected — same "missing row ≠ delete" rule as tasks (I8).
- **Auto-publish to team is STRUCTURALLY removed** (not just flag-gated) — don't re-file it,
  but DO verify no new path reintroduced team-facing writes.
- The recurring "tasks vanished" incidents were the test suite reaching live Google APIs —
  fixed by the conftest guard; verifying the guard's completeness is in scope.
- KNOWN_ISSUES.md lists accepted limitations — don't re-file them, but DO challenge any whose
  risk has grown *now that a second human is in the loop* (esp. "MCP personal-data leakage").
```
