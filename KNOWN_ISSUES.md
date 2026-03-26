# Known Issues — Gianluigi v1.0 (Post Phase 10)

Current as of March 26, 2026. Deployed revision: gianluigi-00034.

---

## Open Issues

### Google Sheets API
- **Intermittent "broken pipe":** Cloud Run idle connections to Sheets API occasionally break. **Mitigated** in Phase 10 with `_execute_with_retry()` (3 retries, exponential backoff). Monitor — should be rare now.
- **SSE transport deprecated:** MCP server uses SSE which is deprecated in favor of Streamable HTTP. Still works, but migration should be planned before Claude.ai drops SSE support.

### Email Intelligence
- **Forwarded thread dedup:** Forwarded email threads may not deduplicate perfectly at low volume. Cosmetic.
- **5-minute polling delay:** Email watcher is not real-time. Replies to approval emails take up to 5 minutes.

### Telegram UX
- **Polling vs webhook:** Using `run_polling()` on Cloud Run with `min-instances=1`. Cold start means missed messages until warm.
- **Long messages truncated:** Telegram 4096-char limit. Long approval previews or search results get cut off.

### Calendar
- **OR-chain false positives:** Calendar filter classifies personal meetings with 2+ team members as CropSight. Needs AND-logic or confidence scoring.

### Gantt
- **Metrics depend on color accuracy:** `compute_gantt_metrics()` reads status from cell background colors. If the Gantt sheet uses non-standard colors, status parsing may be inaccurate.
- **Free-text cells:** Gantt cells use free text, not standardized status labels. The metrics engine handles this via color-to-status HSL heuristic.

### Document Ingestion
- **No OCR:** Scanned PDFs produce empty text extraction.
- **No image processing:** Charts/diagrams in PPTX/DOCX are ignored.
- **Basic chunking:** Fixed-size character chunking doesn't respect document structure.

### MCP / Claude.ai
- **Personal data leakage:** Claude.ai mixes Gianluigi tool results with its own conversation history. **Mitigation:** Dedicated Claude Project ("CropSight Ops") isolates business from personal context.
- **SSE transport:** See above.

### Auth
- **Google OAuth Testing mode:** Tokens expire every 7 days. Move OAuth consent to "Production" mode for permanent tokens (one-time Google Cloud Console action).

### Disabled Schedulers
These are implemented but disabled by default:
- `TRANSCRIPT_WATCHER_ENABLED=true` — Google Drive transcript watcher
- `MORNING_BRIEF_ENABLED=true` — Daily morning brief
- `EMAIL_DAILY_SCAN_ENABLED=true` — Personal Gmail scan
- `DEBRIEF_EVENING_PROMPT_ENABLED=true` — Evening debrief prompt
- `WEEKLY_REVIEW_ENABLED=true` — Weekly review scheduler
- `TASK_ARCHIVAL_ENABLED=true` — Archive completed tasks

---

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
