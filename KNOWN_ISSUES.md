# Known Issues — Gianluigi

**Current as of 10 August 2026.** Live issues only. For change history use
`git log`; for the reasoning behind a design, the memory topic files.

---

## Open

### Needs a decision from Eyal
- **44 proposals are queued and unseen.** `get_proposals()` lists them,
  `decide_proposal()` actions them. Includes the **7 `task_is_a_meeting`**
  proposals from the detector enabled 2026-08-09 — the Calabria meeting, the
  Avi Perl call and five more — plus 11 topic merges, 11 task-field updates,
  10 decision merge/supersede, 3 question closures. Nothing expires for 30 days,
  but the meeting ones are the reason that detector exists.
- **10 of 23 project rows have no objective** (`To do` on the project row):
  Legal, Corporate, Finance, Investor Outreach, Investors Materials, Business
  Plan and the four "Others" buckets. More visible since actions gained their
  own objective on 2026-08-09.

### Operational
- **Nechama has no `telegram_id`.** She is in `team_members` (founders,
  `nechama@cropsight.io`) and is actively editing the workbook, but gets no
  notifications and cannot action anything from her phone. She sends `/myid`
  to the bot; the id goes in her roster row.
- **Tactiq is still not re-pointed**, so auto-ingest depends on transcripts
  being dropped into Drive by hand. Tactiq holds `drive.file` scope, so its
  picker cannot target an existing or Shared-Drive folder — folder choice needs
  the Business plan, Drive integration itself needs Team. The watcher polls both
  known inboxes via `RAW_TRANSCRIPTS_FOLDER_IDS`. Verify the account is on a
  PAID plan: Free has no Drive integration and fails silently.
- **A meeting can be marked `scheduled` with no date.** One is, today
  ("Business plan R&D personnel section review session"). Nothing reminds
  anyone about a booked meeting with no date; `scripts/check_project_status.py`
  now flags it.

### Product Status workbook — usage notes, not defects
- **`Action` is being used as a progress log.** Three rows now read "Reached
  out — will follow up with his boss in a few days" while still `pending`. The
  Action cell is `tasks.title`, so that text becomes the task's name everywhere
  it appears — morning brief, Telegram, digests — and the original commitment is
  gone. `Comments` is the place for an outcome and the tick box is the signal
  for done. Working as designed; flagged because the cost is invisible from the
  sheet.
- **Three actions share identical text**, distinguished only by `Topic`
  ("Yoram to reach out - Eyal to articulate a message for him" ×3). Fine in the
  sheet, ambiguous anywhere Topic is not shown.

### Known limitations
- **MCP personal-data leakage.** Claude.ai mixes tool results with its own
  conversation memory; MCP `instructions` are guidance, not a sandbox.
  Mitigation: a dedicated Claude Project ("CropSight Ops").
- **`MCP_ALLOW_AUTHLESS=true` in production** and must stay until OAuth is
  activated (`MCP_OAUTH_ENABLED=false`, `docs/MCP_OAUTH_RUNBOOK.md`).
- **Topic threading surfaces fabricated links.** Needs the redesign to explicit
  project labels + approval.
- **Gantt metrics read status from cell background colours** via an HSL
  heuristic; non-standard colours parse wrongly.
- **Document ingestion**: no OCR (scanned PDFs extract empty), images and charts
  ignored, paragraph-based chunking.
- **Telegram**: polling not webhook, so a cold start drops messages; 4096-char
  limit truncates long approval previews.
- **Email**: 5-minute polling, so approval replies are not instant; forwarded
  threads dedup imperfectly at low volume.
- **Tombstone matching by filename.** A re-uploaded file with a previously
  rejected name is skipped as "already rejected". Rare — Tactiq names are
  timestamp-prefixed. Real fix is matching on `drive_file_id`.
- **Calendar OR-chain false positives**: a personal meeting with 2+ team members
  classifies as CropSight. Believed fixed; unverified.
- **Test baseline: 5 failures** — `test_ux_hardening` search ×3,
  `test_rag_search`, `test_tier3_approval_status`. Two more
  (`test_gantt_drift`) fail on date-sensitive days.

---

## Corrections to earlier entries in this file

- **"Plain row deletion still resurrects (safety)" is NO LONGER TRUE**, and the
  behaviour now differs by surface, deliberately:
  - **Project Status action row** deleted -> `ps_suppressed`. It leaves the
    sheet and stays OPEN in the database. Not done, not archived, not deleted.
  - **Meetings row** deleted -> `status='dropped'` with the status marked
    manual. Terminal, so it is never re-added, and it moves to Past Meetings
    where the history stays readable. Capped at 5 per cycle.
  - **Tasks tab** is a READ-ONLY MIRROR since 2026-08-09
    (`TASKS_TAB_READ_ONLY=true`); edits there are ignored, not applied.
- **The disabled-scheduler list below is stale.** Live production today:
  `TRANSCRIPT_WATCHER_ENABLED=true`, `TASK_ARCHIVAL_ENABLED=true`,
  `RECONCILE_ENABLED=true`, `MEETING_RECONCILE_ENABLED=true`,
  `PROJECT_STATUS_RECONCILE_ENABLED=true`,
  `PROJECT_STATUS_AUTO_INJECT_ENABLED=true`,
  `MEETING_SHAPED_TASKS_ENABLED=true`, `QUESTION_TRIAGE_ENABLED=true`,
  `WORKSPACE_SORT_ENABLED=true`. Off on purpose:
  `TASK_REMINDER_ENABLED=false`, `EMAIL_DAILY_SCAN_ENABLED=false`,
  `DROPBOX_SYNC_ENABLED` (needs SDK + credentials),
  `CONTINUITY_AUTO_APPLY_ENABLED`. **`config/settings.py` defaults are NOT what
  production runs** — read the Cloud Run env before believing a flag.

---

## Fixed — 2026-08-09/10 (Project Status v2, meetings pool, max code review)

Detail in `git log` and the memory topic files. Headlines:

- **Project Status v2**: `# | Project | Topic | Action | To do | Date | Resp. |
  Priority | Comments`. Row kind is DECLARED (`Project` filled vs `Action`
  filled), never inferred; both filled is reported, never guessed. Contiguous
  per-tab numbering. `To do` is the objective on BOTH row kinds
  (`canonical_projects.objective` / `tasks.objective`).
- **Meetings pool moved** into the Project Status workbook, simplified to six
  visible columns with a hidden identity, `parked` and `recurring` statuses,
  one colour per status, and re-sorted every cycle.
- **16 verified defects** from a max-effort review, all closed. Two themes worth
  remembering: sheet formatting is per-cell and survives `values().clear()`, so
  **assert the whole state rather than adding to it** (5 of the 16); and **a
  constant changed on only one side of a round-trip** (read through a mapping,
  written back raw; a read parameterised without its write; an enum member that
  never reached the sort maps).
- **Layout guards** now stand between a column change and mass duplicate
  creation: `unresolved_columns` / `NON_AREA_TABS` for the area tabs,
  `meetings_layout_ok()` for the pool. A tab that does not declare the expected
  header is skipped entirely and says so.
- **Provenance stopped lying**: 16 of 68 rows carried an `[auto · …]` chip while
  having come from no meeting. Extraction always attaches its source, so no
  `meeting_id` means a human typed it — decided at the one place a row is
  shaped, so no caller can drift again.

---

## Fixed in Sheets-Sync Hardening (April 11, 2026 — evening)

Follow-up session after Eyal reported `/sync` saying "0 to sync" when he'd clearly edited statuses in the Tasks sheet, AND that the Tasks sheet kept silently losing rows ("once again don't have the tasks"). Commit `b60a59b`.

- **Bare-range bug silently broke all Sheets reads (`get_all_tasks()` + `ensure_task_tracker_headers()`):** Both functions called `_read_sheet_range(range_name="A:I")` with no tab prefix. The Sheets API resolves bare A1 ranges against whichever sheet sits at index 0. The moment any backup tab (from `scripts/rebuild_sheets.py`, `duplicateSheet`, or manual reorder) landed in front of `Tasks`, every read silently returned the wrong data. This poisoned `/sync`, `find_task_row`, the task reminder scheduler, overdue reminders, Telegram task-status buttons, MCP task update path, and `archive_completed_tasks` — every single consumer of `get_all_tasks`. **Fix:** qualify every read with `settings.TASK_TRACKER_TAB_NAME`. Regression tests in `tests/test_sheets_sync_tab_resolution.py`.
- **`rebuild_tasks_sheet`/`rebuild_decisions_sheet` could wipe on silent Supabase read failures:** The "tasks vanished" incidents traced back to this failure mode. A transient query error returning `[]` silently propagated into the rebuild, which clear-and-rewrote the sheet with 0 rows. **Fix:** added defensive `force_empty=False` guard that refuses to clear a populated sheet when fed an empty list unless the caller explicitly opts in. Also audit-logs every rebuild as `sheets_rebuild_tasks` / `sheets_rebuild_decisions` so future incidents can be diff'd against the timeline.
- **`scripts/rebuild_sheets.py` silently truncated at 100:** Called `get_tasks()` and `list_decisions()` with the default `limit=100` while the other two rebuild callsites (`approval_flow._reject_meeting_cascade`, `cleanup_rejected_meetings`) correctly pass `limit=1000`. Latent foot-gun; didn't bite today (we have 64 tasks) but would have once the count crossed 100. **Fix:** bumped to `limit=10000`.
- **Dead duplicate `find_task_row` definition:** Two `find_task_row` definitions in `services/google_sheets.py`; Python resolved to the second one and the first was unreachable. Deleted.
- **Fuzzy duplicate detector false-positive storm:** `_detect_duplicate_tasks` was surfacing 9 duplicate pairs on live data, 7 of which were false positives because the matcher counted scheduling filler ("schedule:", "meeting", "session") toward its 60% word-overlap threshold. Post-hardening the same live data returns 2 pairs — both genuinely borderline cases. **Fix:** added scheduling stop-words + punctuation normalization.
- **Extraction-time dedup excluded recently-done tasks:** `deduplicate_tasks()` in `processors/cross_reference.py` only compared new extractions against `pending` + `in_progress`. A task closed last week being re-mentioned always classified NEW, creating a fresh duplicate row. **Fix:** also fetch tasks with `status='done'`, `approval_status='approved'`, `updated_at >= now-30d`, and feed them into the comparison. Also sharpened the prompt to call out cross-assignee scheduling tasks and recently-done no-reopen-intent cases as DUPLICATE.
- **Morning brief duplicate count was too thin to act on:** `format_sync_summary` showed only `"Potential duplicates: N task pairs"` — easy to dismiss with no lever to act. **Fix:** now surfaces an actionable list of up to 5 pairs with titles + assignees.
- **Duplicate detection was stuck behind `/sync`:** `_detect_duplicate_tasks` ran only inside `compute_sheets_diff`, so when `/sync` was broken (see bare-range bug above) duplicates were invisible for days. **Fix:** added a dedicated `_check_duplicate_tasks` in the daily QA scheduler that runs independently of sync and surfaces its own issue line in the morning brief.

Live data actions taken the same session:
- Rebuilt Tasks tab from 67 approved DB rows (sheet had been empty).
- Rebuilt Decisions tab from 68 approved DB rows (sheet had only a header).
- Applied 19 pending status edits from Sheets → DB via `apply_sheets_to_db()` (Eyal's manual edits that had been invisible to the broken sync).
- Deleted 3 duplicate task rows with full snapshot audit trail in `audit_log` as `task_dup_cleanup_delete`: `7ca91b65` (Paolo dup of investor Q&A), `9bff4e1a` (shorter Monday Strategy dup), `f71ddae8` (Product V1 roadmap merged into done Monday).

---

## Fixed in Live Ops Hardening (April 11, 2026)

Three production bugs surfaced during a live debugging session and fixed end-to-end (commits `4825f24`, `925da8c`, deployed as `gianluigi-00064-9ps`).

- **Debrief silent dedup data loss:** `_inject_debrief_items` ran each CEO-typed quick-inject task through `deduplicate_tasks` (Haiku) and silently dropped the row if the LLM false-positive-flagged it as a duplicate (`if dedup_result.get("new_tasks"):` had no fallback). 2026-04-10 incident lost 3 tasks (Yoram legal / U Bank / D&O insurance). **Fix:** bypass dedup entirely for debrief — CEO-authored, trust the input. Lost items recovered manually into the existing pseudo-meeting.
- **Debrief approval_status pending trap (T3.1 interaction):** Even after the dedup fix, the debrief pseudo-meeting and child rows defaulted to `approval_status='pending'` and were invisible to the central read helpers. **Fix:** promote pseudo-meeting + tasks/decisions/open_questions/follow_up_meetings to `approved` at end of `_inject_debrief_items` (debrief bypasses the normal meeting approval flow because Eyal already confirmed via the Inject button).
- **Intelligence signal Telegram ping silent failure:** `_submit_for_approval` never called `schedule_approval_reminders()` and ignored the `send_to_eyal` return value. Eyal got zero notification for `signal-w15-2026` generated 2026-04-09. **Fix:** check return value, retry as HTML-stripped plain text on failure, schedule reminders so the same gentle-ping system used for meeting approvals covers intelligence signals.
- **`/debrief` blue-link non-command:** Bot never called `setMyCommands`, so `/debrief` in message bodies rendered as a tappable link that only populated the composer instead of sending the command. **Fix:** register all 15 commands via `BotCommand` list at bot startup. Applied via direct API call too so it took effect without restart.
- **PTB silent handler error swallowing:** python-telegram-bot's default behavior swallowed handler exceptions to stdout, hiding the real cause of `/debrief` "doing nothing". **Fix:** added global `_on_handler_error` that logs to our logger, persists to `audit_log` as `telegram_handler_error`, and DMs Eyal a one-line error summary. Also wrapped `_handle_debrief` in defensive try/except with immediate "Starting debrief..." ack and inline error reply.
- **MoviePy temp_audiofile cwd permission denied (W15 video silent failure):** `write_videofile` defaulted `temp_audiofile=None`, causing the internal temp audio file to be created in the process cwd. On Cloud Run cwd is read-only outside `/tmp` → `Permission denied opening output moviepy_rawTEMP_MPY_wvf_snd.mp4`. Caught by outer try/except in `_generate_video` as non-fatal → `drive_video_url` stayed `None` → email distribution skipped the 30-min Drive transcoding wait and sent without video link. **Fix:** explicit `temp_audiofile=os.path.join(tmp_dir, "moviepy_temp_audio.m4a")` + `remove_temp=True` at both call sites, AND `TMPDIR=/tmp` env var set on the Cloud Run service as belt-and-braces.

---

## Fixed in Phases 11-13 + X1 (April 1-2, 2026)

- **Distribution pre-edit bug:** Fixed — atomic upsert, always read from pending_approvals.content (Phase 11 C1)
- **Telegram multi-part orphans:** Fixed — delete all non-last parts on approve/reject (Phase 11 C8)
- **Disabled schedulers spam:** Fixed — time-window filters on alerts + task reminders (Phase 11 C2)
- **Morning brief needed approval:** Fixed — sends directly to Eyal, no approval gate (Phase 11 C3)
- **No sensitivity propagation:** Fixed — LLM classification + propagation to child items + distribution filtering (Phase 11 C6)
- **No document versioning:** Fixed — title+source versioning + content hash dedup (Phase 13 B2)
- **No email body storage:** Fixed — body stored for relevant/borderline emails (Phase 13 B4)
- **No email attachment persistence:** Fixed — uploaded to Drive before processing (Phase 13 B3)

## Fixed Post-Phase 10 (April 2026)

- **Supabase RLS enabled:** All 30 tables secured with Row-Level Security. Migration: `migrate_rls_security.sql`. Service role key required.
- **SSE transport migrated:** MCP server moved from SSE to Streamable HTTP.
- **Google OAuth Production mode:** Moved from Testing (7-day expiry) to Production (permanent tokens).
- **Email body markdown rendering:** Fixed — emails now render clean HTML.
- **Word doc task table formatting:** Improved formatting.

## Fixed in Phase 10 (March 25-26, 2026)

- **Gantt metrics returned zeros:** `compute_gantt_metrics()` read wrong data structure keys. Fixed to use `items`/`status`.
- **No Sheets API retry:** 44+ `.execute()` calls had no retry. Added `_execute_with_retry()` with exponential backoff.
- **Token expiry on long-running instances:** Added `_ensure_fresh_credentials()` to refresh OAuth tokens.
- **Data validation errors:** Removed all dropdown validation from Tasks sheet (caused Hebrew errors). Conditional colors remain.
- **Commitment code removed:** ~350 lines of deprecated commitment functions cleaned up.
- **Tasks sheet column reorder:** Phase 10 layout (Priority, Label, Task, Owner, Deadline, Status, Category, Source, Created).
- **Data row formatting inheritance:** Fixed header dark-blue style bleeding into data rows.

## Fixed in Phases 7-9 (March 18-25, 2026)

- **16 QA hardening issues** (commitments deprecated, extraction prompt improved, alerting, timezone, decisions export)
- **Weekly review migrated** to Claude.ai MCP (Phase 7.5)
- **Extraction intelligence** (task continuity, team roles, escalation, Hebrew) (Phase 8)
- **Decision intelligence** (rationale, confidence, review triggers, supersession) (Phase 9A)
- **Cross-meeting topic threading** (Phase 9B)
- **Gantt intelligence** (velocity, slippage, Now-Next-Later) (Phase 9C)

## Fixed in Phase 6 and Earlier

See git history. Key fixes: weekly review UX (8 bugs), meeting prep redesign, approval reminders + expiry, RAG source weights, datetime serialization, Sheets tab naming, Calendar filter.
