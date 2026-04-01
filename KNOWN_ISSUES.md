# Known Issues — Gianluigi v1.0 (Post Phase 10)

Current as of April 1, 2026.

---

## Open Issues

### Critical Bugs
- **Distribution sends PRE-EDIT version:** When Eyal edits a summary before approving, structured data (tasks, decisions) in the DB is NOT updated — only the summary text changes. Team receives stale data. (Found March 24 QA)
- **Telegram multi-part orphans:** On edit, only the last message part (with buttons) is modified. Earlier parts become stale orphans. (Found March 24 QA)

### Google Sheets API
- **Intermittent "broken pipe":** Cloud Run idle connections to Sheets API occasionally break. **Mitigated** in Phase 10 with `_execute_with_retry()` (3 retries, exponential backoff). Monitor — should be rare now.

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
- **Basic chunking:** Fixed-size character chunking doesn't respect document structure.
- **No document versioning:** Updated documents create duplicates, no diff tracking.

### MCP / Claude.ai
- **Personal data leakage:** Claude.ai mixes Gianluigi tool results with its own conversation history. **Mitigation:** Dedicated Claude Project ("CropSight Ops") isolates business from personal context.

### Cross-Meeting Intelligence
- **Hallucinated connections:** Topic threading surfaces irrelevant or fabricated cross-meeting links. Needs redesign — replace fuzzy semantic threading with explicit project labels + approval.

### Disabled Schedulers
These are implemented but disabled by default:
- `TRANSCRIPT_WATCHER_ENABLED=true` — Google Drive transcript watcher
- `MORNING_BRIEF_ENABLED=true` — Daily morning brief (planned to enable)
- `EMAIL_DAILY_SCAN_ENABLED=true` — Personal Gmail scan
- `DEBRIEF_EVENING_PROMPT_ENABLED=true` — Evening debrief prompt (planned to enable)
- `WEEKLY_REVIEW_ENABLED=true` — Weekly review scheduler
- `TASK_ARCHIVAL_ENABLED=true` — Archive completed tasks
- Task reminder scheduler — disabled, needs time-window filters
- Alert scheduler — disabled, needs time-window filters

---

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
