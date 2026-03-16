# Known Issues — Gianluigi v1.0 (Post Phase 4)

Bugs and limitations discovered during live testing (Feb 25 – Mar 16, 2026).
Issues marked **FIXED** have been resolved. Open issues should be addressed in upcoming phases.

---

## Open Issues

### Meeting Prep Quality (Phase 5 Target)
- **Too much noise:** Prep docs pull in loosely related context, making them long and unfocused. The RAG search returns quantity over relevance for prep generation.
- **Wrong context for meeting type:** A BD meeting prep might include unrelated product/legal context. No filtering by meeting category or attendee relevance.
- **Timing issues:** Prep generation triggers based on calendar proximity, but sometimes fires too early or too late depending on how far ahead the meeting was created.

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

### Disabled Schedulers
These schedulers are implemented but disabled by default. Enable via settings:
- `MORNING_BRIEF_ENABLED=true` — Daily morning brief
- `EMAIL_DAILY_SCAN_ENABLED=true` — Personal Gmail scan
- `DEBRIEF_EVENING_PROMPT_ENABLED=true` — Scheduled evening debrief prompt

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
