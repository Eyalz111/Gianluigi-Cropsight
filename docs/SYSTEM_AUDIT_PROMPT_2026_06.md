# Whole-System Audit — Gianluigi (June 2026)

> **How to run — 3 sessions** (each: fresh Claude Code session, `/model opus`):
>
> - **Session 1** — paste: *"Run the system audit defined in
>   docs/SYSTEM_AUDIT_PROMPT_2026_06.md, phases 0 through 2. After each phase:
>   save the findings file, then compact the conversation before starting the
>   next phase."*
> - **Session 2** — paste: *"Continue the system audit defined in
>   docs/SYSTEM_AUDIT_PROMPT_2026_06.md, phases 3 through 5. Resume from
>   SYSTEM_AUDIT_2026_06.md. After each phase: save the findings file, then
>   compact before the next phase."*
> - **Session 3** — paste: *"Finish the system audit defined in
>   docs/SYSTEM_AUDIT_PROMPT_2026_06.md, phases 6 and 7. Resume from
>   SYSTEM_AUDIT_2026_06.md and end by presenting the executive summary."*
>
> The findings file is the only state that matters — compaction/session changes
> lose nothing. If a session's usage warning appears mid-phase, finish the phase,
> save, and roll the remaining phases into the next session.

---

## Mission

Perform a structured, whole-system audit of this codebase (Gianluigi — CropSight's
AI operations assistant). This is NOT a diff review: assume any file may contain a
latent bug regardless of when it was written. Goal: find correctness bugs,
data-loss risks, security gaps, silent-failure paths, and invariant violations —
ranked by real-world impact on a production system that manages a startup CEO's
tasks, meetings, and sensitive business data.

## Hard rules

1. **READ-ONLY against production.** Never run scripts with `--apply`, never call
   write-MCP tools, never deploy, never modify the live Sheet/DB/Telegram. Running
   `pytest` is allowed (the conftest guard blocks Google/Telegram; note that some
   tier3 tests intentionally touch the live Supabase with cleanup — skip them with
   `-k "not tier3"` unless specifically auditing them).
2. **Findings first, fixes later.** Do not change source code during the audit.
   Every proposed fix goes in the findings file for Eyal's approval (house rule:
   Gianluigi proposes, Eyal approves — same applies to you).
3. **Cost discipline:** the main loop is Opus — use it for verification and
   judgment. ALL breadth scanning is done by subagents with `model: "sonnet"`
   (use `model: "haiku"` for mechanical greps/inventories). Never spawn an Opus
   subagent. Keep raw file dumps out of the main context — subagents return
   conclusions, and findings live in the file, not in conversation memory.
4. **The findings file is the single source of truth and the session handoff.**
   Update `SYSTEM_AUDIT_2026_06.md` (repo root) after EVERY phase — never hold
   findings only in context.

## Verification bar

A finding makes the file only if you can name a **concrete failure scenario**
(input/state/timing → wrong outcome). For every candidate a subagent reports,
verify it in the main loop by reading the actual code path before recording it.
Findings that survive get: severity (CRITICAL / HIGH / MEDIUM / LOW), file:line,
scenario, suggested fix, and estimated effort (S/M/L). Refuted candidates get one
line in a "Refuted" appendix so future audits don't re-chase them.

Severity calibration:
- CRITICAL — data loss/corruption, sensitive-data leak across tiers, prod outage,
  approval-gate bypass, money loss.
- HIGH — silent wrong behavior Eyal would act on (wrong deadline/status/recipient),
  unrecoverable-without-manual-repair states, security weakness with a realistic path.
- MEDIUM — degraded/noisy behavior, self-healing inconsistencies, missing alerting.
- LOW — cleanup, dead code, drift risk, docs.

## System invariants (check EVERY phase against these)

These come from CLAUDE.md and hard-won incidents; a violation is automatically ≥ HIGH:

- I1 **Approval gate**: nothing distributes to the team, writes the Gantt board, or
  surfaces pending-approval content without Eyal's explicit approval. Default reads
  filter to `approval_status='approved'` (the 4 central read helpers).
- I2 **All team comms route through Eyal** — no direct nudging of Roye/Paolo/Yoram.
- I3 **Sensitivity follows data**: classified at ingestion (FOUNDERS/CEO/TEAM/PUBLIC),
  propagated to children, enforced at retrieval. Any new output path must filter.
- I4 **Restart-safety**: anything mid-flow must be reconstructable from DB rows alone
  (Cloud Run cycles instances). In-memory-only state pending across an await of
  external I/O is a bug.
- I5 **supabase_client methods are SYNC** — an `await supabase_client.x()` is a bug;
  conversely long sync DB calls inside the event loop can starve schedulers.
- I6 **Date safety**: every human-entered date goes through core/dates.parse_human_date
  (day-first); a provided-but-unparseable date must NEVER become a NULL overwrite or
  an invented date (2026-06-11 incident).
- I7 **Category taxonomy**: tasks.category must be canonicalized via resolve_category
  at the supabase choke points; no caller may bypass.
- I8 **Sheet writes**: never treat a missing row as a delete; archived is the only
  sanctioned removal; col-J UUID identity is sacred; layout comes from TASK_COLUMNS.
- I9 **RLS on every public table** (tests/test_rls_coverage.py + daily QA enforce).
- I10 **All LLM calls go through core/llm.py** with the model-tier policy
  (Opus=extraction, Sonnet=agents, Haiku=classification); every call_site is labeled
  for cost tracking.
- I11 **Asia/Jerusalem for all scheduling**; UTC math drifts the briefs.
- I12 **Broken-pipe resilience**: Google API call sites route through the retry
  wrapper that rebuilds the transport.
- I13 **Heartbeats**: every scheduler loop ticks core/health_monitor so /status and
  the QA agent detect death.
- I14 **Tests must be hermetic**: no real Google/Telegram (conftest guard), no
  un-cleaned live-DB writes, no time bombs (assertions that break as dates pass).

## Phases (one per session; update the checklist in the findings file)

For each phase: spawn 2–4 **Sonnet** subagents in parallel with the angle prompts,
verify their candidates in the main loop, record, update the file, commit nothing.

### Phase 0 — Bootstrap (10 min, once)
Create `SYSTEM_AUDIT_2026_06.md` with: phase checklist, empty findings table,
refuted appendix. Spawn one Haiku subagent to produce a module inventory (file,
LOC, one-line purpose) for processors/, services/, schedulers/, guardrails/, core/
— paste as an appendix. Read KNOWN_ISSUES.md + the latest memory notes so known
items aren't re-found.

### Phase 1 — `processors/` part A: the task/meeting pipeline
`transcript_processor`, `cross_reference`, `meeting_continuity`, `sheets_sync`,
`topic_threading`, `document_processor`. Angles: (a) extraction → storage data flow
(fields dropped/defaulted silently? dedup false-positives silently discarding
tasks?); (b) reconcile engine edge matrix (snapshot vs sheet vs DB: deletions,
clears, partial API failures mid-apply, row-number shifts); (c) idempotency +
re-entry (what happens if a run dies halfway and restarts — I4).

### Phase 2 — `processors/` part B: outputs + knowledge
`morning_brief`, `weekly_digest`, `weekly_review*`, `weekly_pulse`, `summary_rich`,
`debrief`, `meeting_prep`, `knowledge_*`, `topic_clustering`, `gantt_*`,
`intelligence_signal*`, `deal_intelligence`. Angles: (a) tier/sensitivity filtering
on every externally-visible output (I3 — trace each output to its audience);
(b) empty/degraded-data rendering (None/[] → crash or nonsense in Telegram?);
(c) silent excepts that eat failures Eyal should hear about.

### Phase 3 — `services/`
`supabase_client` (4,900 lines — split between two subagents), `google_sheets`,
`google_drive`, `google_calendar`, `gmail`, `telegram_bot` (the other monster —
focus on callback/session-stack state machines + restart reconstruction),
`mcp_server`, `embeddings`, `video_assembler`, `elevenlabs`, `perplexity`.
Angles: (a) error contract consistency (which methods return [] vs raise vs None —
and which CALLERS misinterpret "[] because the API failed" as "genuinely empty",
the sheet-wipe class); (b) retry coverage (I12) — find Google call sites NOT routed
through a retry wrapper; (c) auth/token refresh paths under Cloud Run idle-wake.

### Phase 4 — `schedulers/` + `main.py` boot/shutdown
All ~14 schedulers. Angles: (a) loop-death modes — an uncaught exception class that
kills the while-loop permanently vs the intended catch-sleep-continue; heartbeat
actually ticked on EVERY path (I13)? (b) timezone math (I11) + fire-once guards
(audit_log dedup) — can a restart double-fire a brief/reminder/distribution?
(c) startup ordering in main.py: what happens when a non-critical service fails
init — do dependent schedulers skip cleanly (degraded-mode matrix)?

### Phase 5 — `guardrails/` + security
`approval_flow` (the control point — I1: enumerate EVERY write path that
distributes/promotes and confirm the gate), `mcp_auth` (bearer auth, rate limit,
audit log — try to construct a bypass), `sensitivity_classifier` + propagation,
`content_filter`, `gantt_guard`, `inbound_filter`. Plus: prompt-injection surface —
transcripts/emails/documents are UNTRUSTED input that flows into LLM prompts whose
outputs drive writes; trace whether a malicious transcript could plant tasks,
exfiltrate context into outbound summaries/emails, or steer the approval text
itself. Secrets hygiene: anything logged/echoed that shouldn't be.

### Phase 6 — `core/` + `config/` + cross-cutting
`llm.py` (retry/cost/cache headers, I10 compliance sweep: grep for any Anthropic
client use outside it), `agent.py`/`router.py`/tool definitions vs actual tool
handlers (schema drift), `health_monitor`, `system_prompt` + prompt YAML registry
(hot-reload failure modes), `settings.py` flag hygiene (orphan flags ON in prod but
unused in code, or referenced-but-undefined), `models/schemas.py` vs actual DB rows
drift. Also `scripts/`: anything destructive without a dry-run default.

### Phase 7 — Synthesis (Opus, main loop only — no subagents)
Dedup cross-phase findings, re-rank, write the executive summary: top-10 by
risk-adjusted priority, quick wins (S-effort HIGHs), a proposed fix-PR sequencing
(grouped so each PR is independently testable), and a "what we deliberately accept"
list. End by presenting the summary to Eyal in chat.

## Findings file format (`SYSTEM_AUDIT_2026_06.md`)

```
# System Audit — June 2026
Status: Phase N of 7 complete · last session YYYY-MM-DD
## Phase checklist
- [x] 0 bootstrap … - [ ] 7 synthesis
## Executive summary (written in Phase 7)
## Findings
| ID | Sev | Area | file:line | Finding | Failure scenario | Suggested fix | Effort |
|----|-----|------|-----------|---------|------------------|---------------|--------|
| P1-01 | HIGH | reconcile | processors/sheets_sync.py:123 | … | … | … | S |
## Quick wins
## Refuted candidates (one line each — why)
## Module inventory (appendix)
```

## Known context (don't re-discover these)

- 2026-06-11: category realignment (PR #32) — category IS the Gantt-area taxonomy,
  archived status + Archive tab, day-first dates, conftest live-API guard. A
  10-finding review of that PR was already done and fixed; its deferred items ARE
  in scope here: category-cell lockstep revert edge (no snapshot for category),
  conftest guard enumerates 4 Google classes (discovery.build choke point instead?),
  TASK_COLUMNS resolved at import time (runtime resolution refactor).
- The recurring "tasks vanished" incidents were the test suite reaching live Google
  APIs — fixed by the conftest guard; verifying the guard's completeness is in scope.
- KNOWN_ISSUES.md lists accepted limitations — don't re-file them, but DO challenge
  any whose risk has grown.
```
