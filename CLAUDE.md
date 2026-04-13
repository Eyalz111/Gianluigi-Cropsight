# CLAUDE.md — Gianluigi Project Context

**Last Updated:** April 13, 2026
**Current Version:** v2.2 (Phases 0-13 + X1/X2 + Intelligence Signal + Deal Intelligence + CEO UX + Approval Flow Robustness Tiers 1-3 + Live Ops Hardening 2026-04-11 + Sheets-Sync Hardening 2026-04-11 + Telegram Comms Overhaul 2026-04-13, 43 MCP tools)
**Status:** Telegram Communication Layer Overhaul deployed. Production revision `gianluigi-00066-tf8` (deployed via `gcloud run deploy --source .`, `/health` returning 200). PR 1 (bug fixes: smart message splitting, parse mode migration, dead code cleanup) + PR 2 (voice: office-manager system prompt, debrief/weekly review loosening, ~12 hardcoded string replacements) + PR 3 (formatter restructure: morning brief, debrief summary, /status, command handlers, alerts) shipped together. 1968 tests passing, zero regressions from our changes. **PR 4 still pending:** flip default `parse_mode` from `"Markdown"` to `"HTML"` on `send_message()` / `send_to_eyal()` after observing production for a week. Prior status: Sheets-Sync Hardening on `gianluigi-00065-8tj`, Tier 3 + Live Ops Hardening on `gianluigi-00064-9ps`.

---

## What Is This Project

Gianluigi is CropSight's AI operations assistant — an "AI Office Manager" for a 4-person AgTech founding team. It processes meeting transcripts, tracks tasks/decisions with cross-meeting topic threading and continuity intelligence, maintains institutional memory via hybrid RAG + operational snapshots, generates weekly market intelligence (Intelligence Signal), and serves as the CEO's private operations dashboard via Claude.ai MCP (43 tools).

**CropSight:** Israeli AgTech startup — ML-powered crop yield forecasting. Pre-revenue, PoC stage. Team: Eyal (CEO), Roye (CTO), Paolo (BD, Italy), Prof. Yoram Weiss (Advisor).

---

## Current State (Post Tier 3 + Live Ops Hardening)

- ~1950 tests, all new tests passing (22 pre-existing failures baselined in `tier3_handoff.md`)
- MCP server with 43 tools, connected to Claude.ai via CropSight Ops project
- Full cycle verified live: transcript → extraction (pending) → approve → distribution → MCP query, and reject → tombstone → cascade-clear
- Meeting continuity engine: cross-meeting context, task match annotations, decision chains
- Daily QA agent: extraction quality, distribution completeness, scheduler health, data integrity, RLS coverage, rejected orphans, **approved-with-pending-children safety net (T3.1)**
- Document versioning with content hash dedup, Dropbox sync ready (disabled, needs credentials)
- Phase 11-13 migrations applied (sensitivity, email body, decision freshness, task signals, doc versioning)
- **Approval flow robustness** (Tiers 1+2+T1.9+3) complete: cascading reject + tombstones + FK CASCADE + `approval_status` gating + Gmail/Telegram retry + sheet format on approval
- **Live ops hardening** (2026-04-11): debrief dedup bypass + approval_status promote, intelligence signal approval reminders + plain-text fallback, Telegram setMyCommands + global PTB error handler + defensive `_handle_debrief`, MoviePy temp_audiofile pinning + TMPDIR=/tmp env var (W15 video silent failure root cause)
- Production revision: `gianluigi-00064-9ps` (as of 2026-04-11)

### What Works
- Full transcript pipeline: Tactiq → Drive → Claude extraction → Supabase → approval → distribution
- Hybrid RAG (semantic + full-text, RRF fusion, time-weighted, parent chunks, source weights)
- Telegram bot with commands, Q&A, approval flow, /status command
- Gmail send/receive, Google Drive watchers, Calendar reading
- Task deduplication, status inference, open question resolution
- Entity registry, proactive alerts, system failure alerts (CRITICAL → Telegram DM, WARNING → batched)
- Meeting prep generation, weekly digest (Friday), pre-meeting reminders
- Word document summaries, Google Sheets integration
- Cost optimization (tiered models: Opus/Sonnet/Haiku, prompt caching)
- 5-layer inbound security
- **v1.0 Phase 1:** Multi-agent pattern (Router/Conversation/Analyst/Operator)
- **v1.0 Phase 2:** Bidirectional Gantt integration (read/write/rollback/backup)
- **v1.0 Phase 3:** End-of-day debrief (quick injection + full interactive sessions)
- **v1.0 Phase 4:** Email intelligence (personal Gmail scan, morning brief, email classifier)
- **Architecture Review:** Approval reminders, expiry, health monitoring, RAG source weights, session locking
- **v1.0 Phase 5:** Meeting prep redesign — propose-discuss-generate pipeline, template-driven prep, meeting type classifier, Telegram inline outline flow, timeline modes, restart-safe state, .docx generation, sensitivity-aware distribution
- **v1.0 Phase 6:** Weekly review + outputs — interactive 3-part session (stats → decisions → outputs), HTML report with per-report tokens, Gantt proposal distribution, session corrections with Haiku/Sonnet fallback, digest/review scheduler coexistence, 48h session expiry, debrief interruption support
- **v1.0 Phase 7:** MCP Core + Read Tools — FastMCP SSE server on port 8080, 16 tools (15 read + `get_full_status()` composite), bearer token auth, rate limiting (100/hr), audit logging, `get_system_context()` onboarding tool, session save/load, health/ready/report routes on same port
- **QA Hardening:** 16 issues fixed — commitments deprecated (unified into action items), extraction prompt improved (deadline-only-if-explicit, consolidation 3-7 items), decisions exported to Sheets, summary teaser distribution, all schedulers Israel timezone, system failure alerts (`services/alerting.py`), MCP `_success(warnings=...)` pattern, stakeholder tab fix, silent logging fixes
- **v2 Phase 11 (Operational Maturity):** Distribution pre-edit fix, time-window filters, morning brief direct send, evening debrief prompt, sensitivity LLM + propagation, Sheets on-demand sync, Telegram multi-part fix
- **v2 Phase 12 (Meeting Continuity):** Enhanced context gatherer, continuity-aware extraction (existing_task_match), decision freshness, task signal detection, decision chain traversal
- **v2 Phase 13 (Data Ingestion):** Email body storage, attachment Drive persistence, document versioning + content hash dedup, Dropbox sync (disabled), CropSight document types
- **X1:** Daily QA Agent (extraction quality, distribution, scheduler health, data integrity)
- **X2:** Skills manifest (17 capabilities documented in docs/SKILLS.md)
- **Intelligence Signal (v2.1):** Weekly market intelligence — Perplexity research → Opus synthesis → .docx report → video (MoviePy + ElevenLabs v3 TTS) → email distribution with attachments. 5 new MCP tools (38→43), competitor watchlist auto-curation, Thursday 18:00 IST scheduler.
- **v2.2 Session 1:** Broken pipe retry fix (3 attempts, exponential backoff), 15-min transcript watcher interval, YAML prompt library (`config/prompts/`) with hot-reload.
- **v2.2 Session 2:** Sensitivity tiers redesigned (FOUNDERS/CEO/TEAM/PUBLIC), retrieval-level filtering, interpersonal signal extraction, Telegram task replies via inline buttons.
- **v2.2 Session 2.5:** Sensitivity tier rename (TEAM→FOUNDERS, CEO_ONLY→CEO), retrieval-level filtering across all processors.
- **v2.2 Session 3 — Deal Intelligence (Phase 4):** 3 new DB tables (deals, deal_interactions, external_commitments), `deal_ops` MCP composite tool (9 actions, replaces deprecated `get_commitments`), deal signal detection from transcripts, Deal Pulse + Commitments Due in morning brief, stakeholder sheet +3 columns (Deal Stage, Deal Value, Last Interaction), zero-friction auto-interaction from meetings/emails.
- **v2.2 Session 3 — CEO UX (Phase 5):** `get_full_status(view="ceo_today")` CEO dashboard (overdue tasks, this week, milestones, deal pulse, drift alerts), Gantt drift detection (>50% overdue = drift alert), morning brief enhancements (Task Urgency, Gantt Milestones, Drift Alerts sections).
- **Approval Flow Robustness Tier 1 (2026-04-07):** Cascading reject via `delete_meeting_cascade` + unified Telegram→DB writer + cleanup script + watcher rejection awareness. Closes the "rejected meetings leave orphan children" leak.
- **Approval Flow Robustness Tier 2 (2026-04-07):** `/status` dashboard + morning brief "System State" section + QA scheduler `_check_rejected_meetings` defense-in-depth check.
- **Approval Flow Robustness T1.9 pivot (2026-04-09):** Reject uses DB tombstones (`keep_tombstone=True`) instead of hard delete — the `meetings` row is preserved with `approval_status='rejected'` so the watcher can skip re-processing the same source file. HTTP 403 on user-owned Drive files made the original Drive-move approach unworkable.
- **Approval Flow Robustness Tier 3 (2026-04-09):** Architectural robustness + known-issue fixes.
  - **T3.1 narrow** — `approval_status` column on tasks/decisions/open_questions/follow_up_meetings with CHECK constraint and partial indexes. The 4 central read helpers (`get_tasks`, `list_decisions`, `get_open_questions`, `list_follow_up_meetings`) now filter to approved-only by default (`include_pending=False`). Extraction writes rows as 'pending' via DB default; approve flow promotes them via `_promote_children_to_approved()` with 3-attempt retry.
  - **T3.2 FK CASCADE** — every meeting child table now has `ON DELETE CASCADE` (discovered live-DB schema drift: tasks/decisions/open_questions had been `NO ACTION` in production despite `setup_supabase.sql` claiming `CASCADE`). `delete_meeting_cascade(keep_tombstone=False)` simplified to single DB delete + embeddings (polymorphic) cleanup.
  - **T3.3 cleanup + safety net** — fixed latent bug in `scripts/cleanup_rejected_meetings.py` that was destroying tombstones by calling `delete_meeting_cascade()` without `keep_tombstone=True`. Added QA scheduler check `_check_approved_meetings_with_pending_children` that catches any silent `_promote_children_to_approved` failures (parent='approved' + child='pending' = invisible data hole). Runs daily, surfaces in morning brief.
  - **T3.4 retry on Gmail/Telegram sends** — extracted network calls into `_execute_send` (gmail) and `_bot_send_message` (telegram_bot) wrapped with `@retry` from `core/retry.py`. 3 attempts, exponential backoff. Fixes BrokenPipe silent-drops observed during test 4.
  - **T3.5 `format_task_tracker()` on approval** — `distribute_approved_content()` now calls `format_task_tracker()` after the tasks append loop, so new rows don't inherit header-bleed styling.
  - **T3.6 known limitation doc** — `KNOWN_ISSUES.md` now documents the `source_file_path` ILIKE substring match collision risk for tombstone matching (rare because Tactiq filenames are timestamp-prefixed).
- **Live Ops Hardening (2026-04-11):** Three production bugs caught + fixed during a live debugging session (commits `4825f24`, `5f1d88d`, `925da8c`, deployed as `gianluigi-00064-9ps`).
  - **Debrief silent dedup data loss** — `_inject_debrief_items` ran each CEO-typed quick-inject task through `deduplicate_tasks` (Haiku) and used `if dedup_result.get("new_tasks"):` with no fallback. When Haiku false-positive-flagged a task as a duplicate, the row was dropped silently — no log, no warning. The 2026-04-10 incident lost 3 tasks (Yoram legal / U Bank / D&O insurance) this way. Compounding bug: post-T3.1 the pseudo-meeting and its children defaulted to `approval_status='pending'` so even if dedup had worked, the rows would have been invisible to central read helpers. **Fix:** bypass dedup entirely for debrief (CEO-authored, trust the input) and promote pseudo-meeting + tasks/decisions/open_questions/follow_up_meetings to `approval_status='approved'` at the end of `_inject_debrief_items`. The 3 lost items were recovered manually into the existing pseudo-meeting (`2dfc3a1f-97bb-40f1-8d44-a570e837e98b`).
  - **Intelligence signal Telegram ping unreliable** — `processors/intelligence_signal_agent.py::_submit_for_approval` never called `schedule_approval_reminders()`, so a missed one-shot ping had no follow-up. `send_to_eyal` swallows exceptions and returns False; the caller ignored the return value. Eyal got zero notification for `signal-w15-2026` generated 2026-04-09. **Fix:** check return value, retry with HTML-stripped plain-text fallback on failure, and call `schedule_approval_reminders(signal_id, "intelligence_signal")` so the same gentle reminder system used for meeting approvals also covers signals.
  - **`/debrief` silent failure + general PTB diagnostic blindspot** — two stacked bugs: (1) `setMyCommands` was never called on bot startup, so `/debrief` rendered as a blue link in messages but tapping it only populated the composer instead of sending the command; (2) PTB swallows handler exceptions to stdout, so any error in `_handle_debrief` vanished without trace. **Fix:** added `BotCommand` list registration in `services/telegram_bot.py::start()` covering all 15 commands, wrapped `_handle_debrief` body in try/except with immediate "Starting debrief..." ack + Markdown error reply showing the exception, made the `get_pending_approval_summary` preview defensive, and added a global `_on_handler_error` that captures any handler crash, persists to `audit_log` as `telegram_handler_error`, and DMs Eyal a one-line summary so future silent failures are visible.
  - **MoviePy temp_audiofile cwd permission denied (W15 video silent failure)** — `services/video_assembler.py::write_videofile` calls in both `assemble_video` and `assemble_video_segments` used MoviePy's default `temp_audiofile=None`. In this MoviePy version that creates the temp audio file in the process cwd instead of beside the output file. On Cloud Run cwd is read-only outside `/tmp` → `Permission denied opening output moviepy_rawTEMP_MPY_wvf_snd.mp4`. The whole video assembly failed silently (caught by outer try/except in `_generate_video` as a non-fatal warning) → `drive_video_url` stayed `None` → distribution skipped the 30-min Drive transcoding wait and emailed only the .docx, no video link. **Fix:** pass explicit `temp_audiofile=os.path.join(tmp_dir, "moviepy_temp_audio.m4a")` + `remove_temp=True` at both call sites (pin inside the existing `tempfile.mkdtemp()` directory) AND set `TMPDIR=/tmp` env var on the Cloud Run service (belt-and-braces against any other subsystem that respects TMPDIR).
  - **Approval reminder coverage for intelligence signals** — `schedule_approval_reminders` was previously only called from the meeting approval flow; intelligence signals now also schedule reminders, mirroring the same gentle-ping pattern.
- **Sheets-Sync Hardening (2026-04-11 evening):** End-to-end audit and fix of the Sheets ↔ DB sync flow after Eyal reported `/sync` returning "0 to sync" on freshly-edited rows and recurring "tasks once again missing from the sheet" (commit `b60a59b`, **not yet deployed** — awaits redeploy).
  - **Bare-range read bug in `get_all_tasks()` + `ensure_task_tracker_headers()`** — both called `_read_sheet_range(range_name="A:I")` with no tab prefix. The Sheets API resolves a bare A1 range against whichever sheet sits at index 0; the moment any other tab (a backup tab from `scripts/rebuild_sheets.py`, a tab created by `duplicateSheet`, or a manual reorder) landed in front of `Tasks`, every read silently returned the wrong data. This single bug was the root cause of the "0 to sync" symptom — `compute_sheets_diff` was reading from an empty backup tab, so all 19 manual status edits were invisible. It also silently poisoned `find_task_row`, the task reminder scheduler, overdue reminders, Telegram task-status buttons, MCP task update path, and `archive_completed_tasks` — every downstream consumer of `get_all_tasks`. **Fix:** qualify every read with `f"'{settings.TASK_TRACKER_TAB_NAME}'!A:I"`. Regression test in `tests/test_sheets_sync_tab_resolution.py` creates a new tab in front of Tasks and asserts the read still resolves to the live tab.
  - **`rebuild_*_sheet` clear-on-empty wipe** — `rebuild_tasks_sheet` and `rebuild_decisions_sheet` would clear-and-rewrite the sheet with 0 rows if their `tasks_from_db` / `decisions_from_db` argument was `[]`. If an upstream Supabase read ever returned `[]` due to a silent transient failure (vs. a real exception), the sheet would be wiped. This is the suspected root cause of the earlier "tasks vanished" incidents. **Fix:** defensive `force_empty=False` parameter; both functions now refuse to clear when fed an empty list unless the caller explicitly opts in. Every successful rebuild also audit-logs as `sheets_rebuild_tasks` / `sheets_rebuild_decisions`, and every refusal audit-logs as `sheets_rebuild_refused_empty`, so future incidents can be diff'd against the timeline.
  - **`scripts/rebuild_sheets.py` silent truncation at 100** — called `get_tasks()` / `list_decisions()` with the default `limit=100` while the other two callsites (`approval_flow._reject_meeting_cascade`, `cleanup_rejected_meetings`) correctly pass `limit=1000`. A latent foot-gun that would have bitten once approved-task count crossed 100. **Fix:** bumped to `limit=10000`.
  - **Dead duplicate `find_task_row` definition** — two `find_task_row` methods in `services/google_sheets.py` (line 531 + line ~898); Python resolved to the second, so the first was unreachable. Removed the dead copy.
  - **Fuzzy duplicate detector false-positive storm** — `_detect_duplicate_tasks` (runs in `compute_sheets_diff` + the new QA check) was flagging 9 pairs on live data, 7 of which were false positives that only shared scheduling filler (`schedule:`, `meeting`, `session`). **Fix:** added scheduling stop-words + punctuation normalization. Same live data now returns 2 pairs, both genuinely borderline.
  - **Extraction-time dedup missed recently-done tasks** — `deduplicate_tasks()` in `processors/cross_reference.py` only compared new extractions against `pending`+`in_progress`. A task closed last week being re-mentioned always classified NEW. **Fix:** also fetch `status='done'`, `approval_status='approved'`, `updated_at >= now-30d`, feed into the comparison. Prompt also sharpened to call out cross-assignee scheduling and recently-done-no-reopen as DUPLICATE.
  - **Morning brief duplicate count too thin to act on** — `format_sync_summary` showed only `"Potential duplicates: N task pairs"`. **Fix:** now surfaces an actionable list of up to 5 pairs with titles + assignees.
  - **Duplicate detection was stuck behind `/sync`** — `_detect_duplicate_tasks` ran only inside `compute_sheets_diff`, so when the bare-range bug broke sync, duplicates were invisible for days. **Fix:** new `_check_duplicate_tasks` in `schedulers/qa_scheduler.py` runs independently of sync and surfaces its own issue line in the morning brief.
  - **Live state restored the same session:** rebuilt Tasks tab (67 rows from DB → 64 after dup cleanup), rebuilt Decisions tab (68 rows), applied 19 pending status edits, deleted 3 duplicate task rows with full snapshot audit trail. End-to-end diff verified: 64 tasks + 68 decisions in sync, 0 drift, `get_all_tasks()` returns 64 regardless of tab order.
- **Telegram Comms Overhaul (2026-04-13):** Full rewrite of the Telegram communication layer — presentation only, no business logic changes. Previously messages read like database dumps (counting headers `"Tasks (3):"`, `[type]` tags, key-value dashboards, robotic strings `"Processing..."`, stats footers nobody reads). Messages also got cut mid-sentence at 4000 chars with no indication they continued. Voice benchmark: "trusted, close-circle office manager — kind who has been around since day one." Commits `e3332fa` (PR 1+2), `078d598` (PR 3), `da55173` (test fixes), merged as `bf84e03` and deployed as `gianluigi-00066-tf8`.
  - **PR 1 — Infrastructure fixes:**
    - `_split_message()` rewrite: max_len 4000→3800, continuation markers `(...)` appended/prepended at split boundaries, space-based fallback (no mid-word cuts), new `_adjust_cut_for_html_tags()` that never splits inside `<b>`/`<i>` spans (count opens vs closes, back up on imbalance) or inside `<a href="...">...</a>` spans (back up to before the `<a\b`).
    - Dead code cleanup in `schedulers/meeting_prep_scheduler.py`: deleted `_emergency_background_generate()` (the emergency mode now uses `_create_quick_brief` single-message path; the old method was dead code that would have re-enabled the dual-send race if accidentally called). Added `_prep_in_progress` set with try/finally guard so a crashed outline run doesn't permanently mark the event as "done" (would silently swallow failures).
    - Parse mode migration: 13 callsites (`format_summary_teaser`, `format_alerts_message`, 3 task reminder functions, 8 command handlers) converted from Markdown to HTML with explicit `parse_mode="HTML"`. **Default NOT flipped** — PR 4 will handle that after a week of production observation. Markdown v1 mode breaks on unescaped `_`/`*`/`[` in data; HTML only requires escaping `<`/`>`/`&` which `_escape_html()` already handles.
  - **PR 2 — Voice for LLM-generated content:**
    - `core/system_prompt.py` + `config/prompts/system.yaml` COMMUNICATION STYLE rewrite: 5-line "Professional, concise, and clear" block → 10-rule office-manager voice guide ("Write like you talk to someone you respect", "Lead with what matters", "No system-speak", "Contractions are fine", "Never start a message with a heading").
    - `core/debrief_prompt.py` + `config/prompts/debrief.yaml`: `response_text` "1-2 sentences max" → "1-3 sentences, sound like a person confirming, not a system logging. 'Got the Yoram meeting items' not 'Captured 3 items from your input'".
    - `core/weekly_review_prompt.py` + `config/prompts/weekly_review.yaml`: bullet-point-mandating style → "sharp office manager running a weekly check-in, not rendering a dashboard." (Hit a YAML single-quote escape bug on "don't" → fixed with `don''t`.)
    - ~12 hardcoded strings in `services/telegram_bot.py`, `processors/debrief.py`, `schedulers/debrief_prompt_scheduler.py`: `"Starting debrief..."` → `"One sec..."`, `"Processing..."` deleted, `"Searching for: query..."` → `"Let me look into that..."`, `"Approved! Distributing..."` → `"Done — distributing now."`, `"Rejecting..."` → `"Got it — rejecting."`, `"Approved! Injecting items..."` → `"Done — injecting now."`, `"Use the buttons below to approve, request changes, or reject."` → `"Approve or reject below."`, `"Let's do your end-of-day debrief."` → `"Ready for your end-of-day wrap-up."`, `"Welcome back to your debrief. You have N items..."` → `"Picking up where we left off — N items so far."`, `"We still haven't covered"` → `"Still haven't touched on"`, `"What else happened today?"` → `"What else?"`, `"Debrief approved and saved."` → `"All saved."`, `"Debrief cancelled. Nothing was saved."` → `"Cancelled — nothing saved."`, evening debrief prompt rewritten from formal HTML to plain conversational text.
  - **PR 3 — Formatter restructure:**
    - `processors/morning_brief.py::format_morning_brief()` full rewrite (Option A: tightened scannable, approved by Eyal via before/after sketch). Kept visual hierarchy (bold headers, 🔴/🟡 severity emoji, bullet items). Dropped counting headers (`"(N):"`), `[type]` tags (`[task]`/`[info]`), stats footer (`"N email items • N meetings today"`), and empty sections. Merged `alerts` + `task_urgency` + `drift_alerts` into single "Needs attention" section. Merged `deal_pulse` + `commitments_due` into "Deals". Calendar + `pending_prep_outlines` merged under "Today". System state as one-liner (`"System: all clear"` or `"System: watcher stale, 2 rejected meetings"`), no bold header. QA health only surfaces if non-healthy. Continuity and sheets_sync sections intentionally omitted. Target: ~800-1200 chars for a normal day. **Exception kept:** `[SENSITIVE]` tag on email categories (meaningful metadata, unlike derivable type tags).
    - `processors/debrief.py::_format_extraction_summary()` rewrite: prose for 1-4 items per type ("Three tasks — Paolo to follow up with Lavazza, you to schedule the security review, and Roye to draft the accuracy framework. One decision: going with AWS over Azure."), compact numbered list for 5+ items ("8 tasks:\n  1. Follow up with Lavazza — Paolo\n  ..."). No "Debrief Summary (N items):" header. Sensitive items as " (sensitive)" inline.
    - `services/telegram_bot.py::_handle_status()` rewrite: key-value dump → merged sentences. "47 meetings processed, last: today. 67 tasks tracked — 12 open, 3 overdue. 23 documents ingested." Healthy sections collapse to confirmation sentences; problem sections get described plainly.
    - Command handlers rewritten: `/start` trimmed to one sentence, `/help` grouped by use case down to 10 lines, `/tasks` shows prose for 1-5 tasks with `/tasks all` overflow for 6+, `/decisions` / `/questions` lead with count then top 5, `/meetings` simplified to title + date (dropped participant count and status).
    - `processors/proactive_alerts.py::format_alerts_message()`: dropped severity group headers (`"HIGH PRIORITY"`), single `"Heads up"` header, flat sorted list with 🔴/🟡 severity emoji. `format_alerts_message` is for mid-day proactive alerts (alert scheduler fires every 12h); morning brief has its own "Needs attention" section at 7am — no collision.
    - Task reminders in `schedulers/task_reminder_scheduler.py` rewritten to one-sentence format: `"<b>{task}</b> is {N} days overdue ({assignee})."`, `"<b>{task}</b> is due today ({assignee})."`, `"<b>{task}</b> is due {date} — {N} days from now ({assignee})."`
    - Lighter-touch formatters (`format_summary_teaser`, `_format_cross_reference_section`, `format_outline_for_telegram`, `format_telegram_notification`) were already decent — minor HTML/voice alignment only.
    - 18 golden-snapshot tests in `tests/test_formatter_snapshots.py`: 9 morning brief scenarios (busy day, quiet day, all-overdue, sensitive-only, zero emails, system problems, deal pulse, empty, morning after heavy debrief), 5 debrief scenarios (single task prose, mixed small counts, large counts compact list, sensitive items, empty), 4 alert scenarios (mixed severities, high only, low only, empty). Structural assertions: no `[type]` tags (except `[SENSITIVE]`), no Markdown bold artifacts, no counting headers, char limit 3800, emoji whitelist (🔴/🟡 only).
    - 15 new tests in `tests/test_split_message.py` covering continuation markers, HTML tag safety, boundary preferences, no-mid-word-cut invariant, edge cases.
    - Updated 10 existing tests that checked old format strings (counting headers, section names, key-value format, Markdown bullets) to match new output.
  - **Deploy sequence compressed:** Plan called for PR 1 deploy + 48h soak before PR 3, to catch edge-path callsites that PR 1 might have missed in the parse mode migration. Shipped all 3 PRs together because (a) the parse mode migration was callsite-explicit (not a default flip), (b) test suite covers every touched callsite, (c) rolling back all 3 is one `git revert` per commit. **PR 4 still deferred** — flipping the default `parse_mode` on `send_message()` / `send_to_eyal()` from `"Markdown"` to `"HTML"` requires a full grep audit for every send call that omits `parse_mode`, explicit decision per callsite, then the flip. Scheduled for next week after observing production.
  - **Final test state:** 1968 passing, 19 pre-existing failures (9 test-ordering contamination in weekly_review_scheduler that pass in isolation, 10 environment/unrelated in test_mcp_auth, test_v1_models, test_intelligence_signal_prompts). Zero regressions from our changes.

### Known Issues
- Email dedup edge cases: forwarded threads may not deduplicate perfectly at low volume
- Transcript watcher disabled by default (TRANSCRIPT_WATCHER_ENABLED=false)
- Dropbox sync needs SDK + credentials before enabling
- See KNOWN_ISSUES.md for full list

---

## v1.0 — "The AI Office Manager" (Complete)

**Design document:** `V1_DESIGN.md` (comprehensive spec for v1.0 phases)
**v2 implementation plan:** `.claude/plans/keen-strolling-pnueli.md` (Phases 11-13 + X1/X2)
**Architecture review:** `docs/qa/gianluigi_v2_architecture_review.md` (v2 concerns, addressed)
**Skills manifest:** `docs/SKILLS.md` (17 capabilities with triggers, inputs, outputs, costs)
**Phase 6 architecture:** `docs/system_architecture_v1_phase6.md` (visual, pre-v2 — needs update)

### Completed Phases
- **Phase 0:** Database migration, new models
- **Phase 1:** Multi-agent foundation
- **Phase 2:** Gantt integration
- **Phase 3:** Debrief flow
- **Phase 4:** Email intelligence
- **Post-Phase 4:** Architecture review fixes (approval expiry, health monitoring, RAG weights, session locking)
- **Phase 5:** Meeting prep redesign (propose-discuss-generate, templates, type classifier, timeline modes)
- **Phase 6:** Weekly review + outputs (3-part interactive session, HTML reports, Gantt distribution, live QA fixes)
- **Phase 7:** MCP Core + Read Tools (SSE server, 16 tools, auth, rate limiting, audit logging)
- **QA Hardening:** 16 issues fixed (commitments deprecated, extraction improved, alerting, timezone, decisions export, MCP composite tool)

- **Phase 7.5:** Weekly review migration (weekly review via Claude.ai MCP, Telegram redirect)
- **Phase 8a:** Extraction intelligence (task continuity, team roles, escalation, Hebrew) + MCP write tools (task CRUD, quick inject, Gantt propose)
- **Phase 8b:** Health monitoring (scheduler heartbeats), cost monitoring, tsvector Hebrew fix, index audit
- **Phase 9A:** Decision intelligence (rationale, confidence, review triggers, supersession detection), canonical project labels, task archival
- **Phase 9B:** Cross-meeting memory — meeting-to-meeting continuity, compressed operational snapshots, topic threading (4 MCP tools)
- **Phase 9C:** Gantt intelligence (velocity, slippage, milestone risk, Now-Next-Later), follow-up tracking
- **Phase 9D:** Tool grouping (category prefixes on 33 tools), weekly review integration, Word doc labels

- **Phase 10:** Polish & Ship — Sheets redesign (TASK_COLUMNS/DECISION_COLUMNS constants, column reorder, rebuild functions), dynamic canonical projects (DB table + 2 MCP tools), Claude.ai project prompt (35 tools documented), deprecated commitment code removed, data validation removed, smoke test transcript

- **Phase 11 (v2 Workstream C — Operational Maturity):** Distribution pre-edit fix, time-window filters (enabled alert+reminder schedulers), morning brief direct send, evening debrief prompt, watcher intervals, sensitivity LLM classification + propagation, Sheets on-demand sync, Telegram multi-part fix
- **Phase 12 (v2 Workstream A — Meeting Continuity):** Enhanced context gatherer (daily + pre-meeting), continuity-aware extraction with existing_task_match annotations, decision freshness tracking (touch + stale surfacing), task signal detection (email/Gantt/calendar), decision chain traversal + MCP tool
- **Phase 13 (v2 Workstream B — Data Ingestion):** Full email body storage, email attachment Drive persistence, document versioning (title+source + content hash dedup), CropSight document types, Dropbox → Drive sync (disabled, needs credentials)
- **X1:** Daily QA Agent — extraction quality, distribution completeness, scheduler health, data integrity checks. Runs 06:00 IST, feeds morning brief, on-demand MCP tool
- **X2:** Skills manifest (`docs/SKILLS.md`) — 17 capabilities documented

- **v2.2 Session 1:** Broken pipe retry, 15-min watcher, YAML prompt library
- **v2.2 Session 2:** Sensitivity tiers (4-level), interpersonal signals, Telegram task replies
- **v2.2 Session 2.5:** Sensitivity tier rename (FOUNDERS/CEO), retrieval-level filtering
- **v2.2 Session 3 Phase 4:** Deal Intelligence — 3 tables, `deal_ops` MCP tool (replaces `get_commitments`), deal signal detection, Deal Pulse + Commitments Due in morning brief, stakeholder sheet expansion
- **v2.2 Session 3 Phase 5:** CEO UX — `ceo_today` view on `get_full_status`, Gantt drift detection, Task Urgency/Milestones/Drift in morning brief

### Deferred (Beyond v2.2)
- Risk register, meeting effectiveness scoring, OKR layer, full Sheets bidirectional sync

### Known MCP Limitation: Personal Data Leakage
Claude.ai mixes MCP tool results with its own conversation memory. MCP `instructions` are guidance, not a sandbox — Claude.ai can and will use prior conversation context when Gianluigi data is sparse. **Current mitigation:** Use a dedicated Claude Project ("CropSight Ops") to isolate business conversations. **Future:** OAuth integration (Phase 8) may enable stricter session isolation. This is a Claude.ai platform limitation, not a Gianluigi bug.

### What's NOT Changing
- Supabase (EU region) as primary database
- Telegram as primary daily interaction channel
- Google Workspace (Drive, Sheets, Calendar, Gmail) integrations
- Professional tone guardrails and sensitivity classification
- CEO-approval-first pattern for all team distributions
- Tactiq for meeting transcription
- Tiered model strategy (Opus/Sonnet/Haiku)
- Cloud Run hosting

---

## Important Design Principles

- **Gianluigi proposes, Eyal approves.** Never write to Gantt, distribute to team, or make structural changes without explicit CEO approval.
- **All team interactions go through Eyal.** No direct nudging of team members. Only approved distributions.
- **Brain is interface-agnostic.** Capabilities are Python functions. Telegram and MCP are interfaces.
- **Free-text resilience.** No rigid command formats. Understand natural language, typos, abbreviations.
- **Confirm before action.** Any write operation from ambiguous input must be confirmed first.
- **Source citations.** Every extracted item references its source.
- **Sensitivity follows data.** Tags applied at ingestion, follow through to outputs.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Claude API (Opus/Sonnet/Haiku) via Anthropic SDK |
| Database | Supabase (PostgreSQL + pgvector, EU region Frankfurt) |
| Embeddings | OpenAI text-embedding-3-small (1536d) |
| Chat | Telegram Bot (python-telegram-bot) |
| Email | Gmail API (gianluigi.cropsight@gmail.com) |
| Files | Google Drive API |
| Tasks/Gantt | Google Sheets API |
| Calendar | Google Calendar API (read-only, authenticated as Eyal via per-user OAuth token) |
| Hosting | Google Cloud Run (europe-west1) |
| Transcription | Tactiq (Chrome extension) |
| CEO Interface | Claude.ai via MCP server (Streamable HTTP, FastMCP SDK) |
| MCP Server | `mcp` Python SDK + uvicorn, 43 tools on port 8080 |
| Video | MoviePy + PIL + matplotlib + ffmpeg (2-pass encoding) |
| TTS | ElevenLabs v3 API (per-segment narration) |
| Research | Perplexity Sonar Pro API (web search with citations) |
| Language | Python 3.11+, async |

---

## Supabase Notes
- All methods are **SYNC** (never await them)
- Uses PostgREST API via supabase-py
- pgvector for semantic search, tsvector for full-text
- v1.0 tables: gantt_schema, gantt_proposals, gantt_snapshots, debrief_sessions, email_scans, mcp_sessions, weekly_reports (+ html_content, access_token, expires_at), weekly_review_sessions, meeting_prep_history (+ outline_content, focus_instructions, timeline_mode), pending_approvals (with expires_at), calendar_classifications (+ meeting_type), meetings (+ meeting_type)

## MANDATORY: Row Level Security on every new table
Every `CREATE TABLE` statement in a migration SQL file MUST be followed by
`ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;`. Without this, Supabase flags
the table as publicly accessible via the anon key (security vulnerability).

Gianluigi uses the service_role key, which bypasses RLS automatically — so
enabling RLS has zero functional impact, it only locks out the anon/public
path.

Enforcement layers:
1. **Test:** `tests/test_rls_coverage.py` queries Supabase and fails pytest
   if any public table is missing RLS.
2. **Runtime:** `schedulers/qa_scheduler._check_rls_coverage()` runs daily
   and fires a CRITICAL alert in the morning brief + `/status` if anything
   slipped through.
3. **Template:** copy the pattern from `scripts/migrate_rls_security_v2.sql`
   (bottom of file has a commented template).

Both layers 1 and 2 depend on `public.get_table_rls_status()` being installed
— it's created by `scripts/migrate_rls_security_v2.sql`.

## LLM Notes
- **Opus:** Transcript extraction, document analysis (accuracy-critical) — Analyst Agent
- **Sonnet:** Conversations, tool use, Gantt operations — Conversation + Operator Agents
- **Haiku:** Classification, intent routing, outline agenda generation, focus classification — Router Agent
- Prompt caching via `cache_control: {"type": "ephemeral"}` on system prompts
- All calls go through `core/llm.py` centralized helper

## Calendar Architecture
- Gianluigi reads Eyal's calendar using **Eyal's OAuth token** (`EYAL_CALENDAR_REFRESH_TOKEN`), not a shared calendar
- This lets us see Eyal's event colors (purple = CropSight), declined status, etc.
- Token obtained via `python scripts/get_calendar_token.py` (calendar.readonly scope)
- Falls back to Gianluigi's token if Eyal's not set (but colors won't be visible)
- **Future (Phase B):** When CropSight moves to Google Workspace, replace per-user tokens with service account + domain-wide delegation

## Important IDs
- Eyal Telegram DM: `8190904141`
- Group chat: `-5187389631`
- Calendar color `3` (purple = CropSight)

---

## Files to Read for Context
1. `V1_DESIGN.md` — Full v1.0 specification (START HERE for new features)
2. `config/settings.py` — All environment variables and configuration
3. `config/team.py` — Team emails, filter keywords, blocklists
4. `core/system_prompt.py` — Gianluigi's personality and guardrails
5. `models/schemas.py` — All Pydantic data models
6. `KNOWN_ISSUES.md` — Bugs from live testing
7. `docs/qa/ARCHITECTURE_REVIEW_ISSUES.md` — Architecture review findings
8. `docs/system_architecture_v1_phase5.md` — Post-Phase 5 system architecture
9. `docs/system_architecture_v1_phase6.md` — Post-Phase 6 system architecture
10. `config/meeting_prep_templates.py` — Meeting prep template definitions
11. `services/mcp_server.py` — MCP server with 38 tools (read + write)
12. `guardrails/mcp_auth.py` — MCP bearer token auth, rate limiting, audit logging
13. `docs/SKILLS.md` — All 17 system capabilities documented
14. `processors/meeting_continuity.py` — Cross-meeting context (Phase 12)
15. `schedulers/qa_scheduler.py` — Daily QA agent (X1)
