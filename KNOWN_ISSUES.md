# Known Issues — Gianluigi v2.2 (Post Session 3 + Live Ops Hardening)

Current as of April 11, 2026.

---

## Open Issues

### Critical Bugs
- None currently open (distribution pre-edit and Telegram orphans fixed in Phase 11)

### Google Sheets API
- **Intermittent "broken pipe":** Cloud Run idle connections to Sheets API occasionally break. **Mitigated** in Phase 10 with `_execute_with_retry()` (3 retries, exponential backoff). Monitor — should be rare now.

### Tombstone Matching (Tier 1.9)
- **source_file_path collision:** The watcher matches rejected-meeting tombstones by filename using ILIKE substring match on `meetings.source_file_path`. If a new file is uploaded with the same filename as a previously-rejected file, the watcher will match the tombstone and skip the new file as "already rejected." In practice this is rare because Tactiq uses timestamp-prefixed filenames (e.g., `2026-04-09_1428_cropsight-sync.txt`) — collisions only happen for exact filename+timestamp duplicates, which don't occur organically. Future mitigation: add `drive_file_id` column to `meetings` and match by Drive file ID instead of filename for exact identity. Deferred — not causing active bugs.

### Email Intelligence
- **Forwarded thread dedup:** Forwarded email threads may not deduplicate perfectly at low volume. Cosmetic.
- **5-minute polling delay:** Email watcher is not real-time. Replies to approval emails take up to 5 minutes.

### Telegram UX
- **Polling vs webhook:** Using `run_polling()` on Cloud Run with `min-instances=1`. Cold start means missed messages until warm.
- **Long messages truncated:** Telegram 4096-char limit. Long approval previews or search results get cut off.

### Calendar
- **OR-chain false positives:** Calendar filter classifies personal meetings with 2+ team members as CropSight. Likely fixed — verify.

### Gantt
- **Metrics depend on color accuracy:** `compute_gantt_metrics()` reads status from cell background colors. If the Gantt sheet uses non-standard colors, status parsing may be inaccurate.
- **Free-text cells:** Gantt cells use free text, not standardized status labels. The metrics engine handles this via color-to-status HSL heuristic.

### Document Ingestion
- **No OCR:** Scanned PDFs produce empty text extraction.
- **No image processing:** Charts/diagrams in PPTX/DOCX are ignored.
- **Basic chunking:** Fixed-size character chunking doesn't respect document structure (structure-aware hints added in B2 metadata, but chunking logic is still paragraph-based).

### MCP / Claude.ai
- **Personal data leakage:** Claude.ai mixes Gianluigi tool results with its own conversation history. **Mitigation:** Dedicated Claude Project ("CropSight Ops") isolates business from personal context.

### Cross-Meeting Intelligence
- **Hallucinated connections:** Topic threading surfaces irrelevant or fabricated cross-meeting links. Needs redesign — replace fuzzy semantic threading with explicit project labels + approval.

### Disabled Schedulers
These are implemented but disabled by default:
- `TRANSCRIPT_WATCHER_ENABLED=false` — Google Drive transcript watcher
- `TASK_ARCHIVAL_ENABLED=false` — Archive completed tasks
- `DROPBOX_SYNC_ENABLED=false` — Dropbox → Drive sync (needs SDK + credentials)
- `CONTINUITY_AUTO_APPLY_ENABLED=false` — Auto-apply task matches from extraction (needs A3 production gate)

Note: Morning brief, email scan, debrief prompt, alert scheduler, and task reminders were enabled in Phase 11.

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
