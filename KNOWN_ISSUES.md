# Known Issues — Gianluigi v1.0 (Post Phase 6)

Bugs and limitations discovered during live testing (Feb 25 – Mar 18, 2026).
Issues marked **FIXED** have been resolved. Open issues should be addressed in upcoming phases.

---

## Open Issues

### Email Intelligence (Known Limitations)
- **Forwarded thread dedup:** Forwarded email threads may not deduplicate perfectly. At current volume (~50 emails/day scan), this is cosmetic. Noted for Phase 5+.
- **5-minute polling delay:** Email watcher is not real-time. If Eyal replies to an approval email, it takes up to 5 minutes to be processed.

### Telegram UX
- **Polling vs webhook:** Using `run_polling()` on Cloud Run with `min-instances=1`. Works but not ideal — a cold start means missed messages until the instance is warm.
- **Long messages truncated:** Telegram has a 4096-char limit per message. Long approval previews or search results sometimes get cut off without a clean split.

### Document Ingestion
- **No OCR:** Scanned PDFs produce empty text extraction. Only text-based PDFs work.
- **No image processing:** Charts, diagrams, and images in PPTX/DOCX are ignored.
- **Basic chunking:** Fixed-size character chunking doesn't respect document structure (sections, headings).

### Calendar Filter False Positives
- **OR-chain classification:** Calendar filter uses an OR chain (blocklist → color → participants → prefix → uncertain). A personal meeting with 2+ team members (e.g., Saturday lunch with Yoram) gets classified as CropSight even without purple color. Needs AND-logic or confidence scoring to distinguish personal from business meetings with team members.

### Weekly Review UX
- **Too heavy for Telegram:** The 3-part weekly review (stats → decisions → outputs) works but is cumbersome in a chat interface. Phase 7 will move this to Claude.ai as the primary interface, with Telegram as notification/fallback.
- **HTML report requires Cloud Run:** Report URLs use `REPORTS_BASE_URL` (Cloud Run). Locally falls back to `localhost:8080` which requires the health server to be running.

### Disabled Schedulers
These schedulers are implemented but disabled by default. Enable via settings:
- `MORNING_BRIEF_ENABLED=true` — Daily morning brief
- `EMAIL_DAILY_SCAN_ENABLED=true` — Personal Gmail scan
- `DEBRIEF_EVENING_PROMPT_ENABLED=true` — Scheduled evening debrief prompt
- `TRANSCRIPT_WATCHER_ENABLED=true` — Google Drive transcript watcher

---

## Fixed Issues (Phase 6 QA — Mar 18)

### Weekly Review Smoke Test Fixes (Fixed Mar 18)
- **Button order swapped:** [<< Back] was on right, [Continue >>] on left — swapped to correct positions
- **Part 2 misleading title:** "Decisions needed" showed calendar meetings confusingly — renamed to "Next week + decisions", meetings shown first
- **HTML report URL broken locally:** Bare relative path when `REPORTS_BASE_URL` empty — added `localhost:8080` fallback
- **No distribution feedback:** After approval, no output shown — now shows success/failure per channel
- **Debrief text not intercepted:** Typing "debrief" during review did nothing — added text interception
- **Session TTL conflict:** `WEEKLY_REVIEW_TTL_MINUTES` (120 min) conflicted with 48h expiry — removed, unified to `WEEKLY_REVIEW_SESSION_EXPIRY_HOURS`
- **Stack-before-validation:** Session stack push happened before validation — moved after successful start
- **Correction flow missing buttons:** After editing, [Approve & Distribute] buttons disappeared — re-shown after correction

---

## Fixed Issues (Phase 5 — Mar 17)

### Meeting Prep Quality (Fixed Mar 17)
- **Issue:** Prep docs pulled loosely related context, wrong context for meeting type, bad timing.
- **Fix:** Phase 5 redesign — template-driven prep per meeting type (founders_technical, founders_business, monthly_strategic, generic), scoring-based type classifier, propose-discuss-generate flow with Telegram inline outline proposals, timeline modes (normal/compressed/urgent/emergency/skip), graceful degradation per data query.

---

## Fixed Issues (v1.0 Architecture Review)

### Approval Reminders + Expiry (Fixed Mar 16)
- **Issue:** Pending approvals could sit unnoticed indefinitely.
- **Fix:** Gentle Telegram reminders at configurable intervals. Approvals expire gracefully per content type. `/status` shows pending queue.

### RAG Source Weights (Fixed Mar 16)
- **Issue:** Only debrief source had a weight boost (1.5x). V1_DESIGN specified a full weight table.
- **Fix:** Configurable per-source weights in settings. Dict lookup replaces binary check.

### Weekly Digest Timing (Fixed Mar 16)
- **Issue:** Hardcoded Sunday 18:00-20:00. Israel work week ends Friday.
- **Fix:** Configurable day/hour/window. Default moved to Friday 14:00.

---

## Fixed Issues (v0.5)

### Tasks Going to Wrong Sheets Tab (Fixed Mar 13)
- **Fix:** Added `tab_name` parameter, all task callers pass `tab_name="Tasks"`.

### Email Approval Route Incomplete (Fixed Mar 13)
- **Fix:** Added `[ref:{meeting_id[:8]}]` tag to approval email subjects. Handler extracts ref, looks up pending approval.

### Edit Count Message Inaccurate (Fixed Mar 13)
- **Fix:** Replaced with generic "Edits applied successfully."

---

## Fixed Issues (Pre-v0.5)

See git history for details. Key fixes: datetime serialization, Tactiq format parsing, Supabase sync method handling, Telegram message routing, Gmail scope, Google Sheets tab naming, Calendar filter format, pydantic-settings env export.
