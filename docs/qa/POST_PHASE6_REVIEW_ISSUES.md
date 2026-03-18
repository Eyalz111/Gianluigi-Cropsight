# Phase 6: Post-Implementation Review — Issues & Hardening Items

**Date:** March 18, 2026
**Context:** Phase 6 (Weekly Review + Outputs) is code-complete with 1246 tests passing. This review identifies failure points, resilience gaps, and operational concerns discovered during architecture analysis. Items are ordered by severity — address critical items before live QA testing.

---

## 1. CRITICAL — Gantt Backup Must Happen BEFORE Write, Not After

**Current behavior:** The atomic distribution sequence is: execute Gantt proposals → backup Gantt → upload PPTX → email → Telegram.

**Problem:** The backup happens AFTER the Gantt write (step 2), which means the backup captures the post-write state. If the Gantt write succeeds but a downstream step fails (Drive upload, email), the Gantt has been modified but the team never sees the corresponding report. The backup is useless for recovery because it contains the new state, not the pre-write state.

Additionally, if the Gantt write itself partially fails (some rows written, some not), there's no pre-write snapshot to rollback to.

**Required change:**
- Move the Gantt snapshot/backup to BEFORE executing proposals: backup → execute proposals → verify success → proceed with distribution
- If ANY Gantt proposal execution fails after backup: offer [Rollback Gantt + Hold] [Distribute anyway] [Retry failed proposals]
- If a downstream step fails (Drive, email) after successful Gantt write: the pre-write backup is available for rollback if Eyal chooses
- The existing `gantt_snapshots` table and rollback mechanism should handle this — just reorder the operations

**Files to modify:** `guardrails/approval_flow.py` (distribute_approved_review), `processors/weekly_review_session.py` (confirm_review)

---

## 2. CRITICAL — Session Stack Must Be Persisted in Supabase

**Current behavior:** The session stack (which allows debrief to interrupt weekly review) appears to be an in-memory list — push/pop operations on a Python list or similar structure.

**Problem:** If Cloud Run restarts while Eyal is mid-review and has also started a debrief (stack = [weekly_review, debrief]), the stack is lost. On restart, both sessions exist independently in their respective Supabase tables (weekly_review_sessions and debrief_sessions) but the system doesn't know which one was on top or that they were stacked. Both could try to capture Eyal's messages, or neither could resume correctly.

**Required change:**
- Persist the session stack in Supabase. Options:
  - A simple `session_stack` table with `user_id, position, session_type, session_id, status` columns
  - Or a JSONB field on an existing table: `{"stack": ["weekly_review:uuid-123", "debrief:uuid-456"]}`
- On startup, `startup_recovery()` should reconstruct the session stack from this persisted state
- The `_active_interactive_session` property getter should check Supabase as fallback when in-memory stack is empty (same pattern as the focus_active persistence in Phase 5)
- On session push/pop, update both the in-memory stack AND the Supabase record

**Files to modify:** `services/telegram_bot.py` (session stack persistence), `services/supabase_client.py` (stack CRUD methods), `main.py` (startup recovery)

---

## 3. CRITICAL — Calendar Re-Verification for Weekly Review Events

**Current behavior:** The scheduler detects the review event and triggers data compilation at T-3h and notification at T-30min. No mention of re-checking the event between these stages or before `/review` starts.

**Problem:** If Eyal reschedules the review event from 14:00 to 16:00 at 11:15, the T-3h trigger already fired at 11:00. The compiled data and pre-generated outputs are timestamped for a 14:00 meeting, but the actual meeting is at 16:00. The T-30min notification goes out at 13:30 (based on original time), which is now 2.5 hours early. If Eyal adds or moves the event after the T-3h trigger, the system runs on stale assumptions.

This is the same calendar-change issue identified for meeting prep (ARCHITECTURE_REVIEW_ISSUES.md Item 9).

**Required changes:**
- Before sending the T-30min notification: re-check the calendar event. If deleted → cancel session, notify Eyal: "Weekly Review was removed from calendar — session cancelled." If time changed significantly (>30min shift) → update session, recalculate notification timing, recompile if data is >3h stale.
- When Eyal types `/review`: verify the calendar event still exists and the session data is fresh enough. If data was compiled >4h ago, offer to recompile: "Your review data was compiled 4 hours ago. Want me to refresh? [Refresh] [Use existing]"
- Store the original `event_id` and `event_start_time` in the `weekly_review_sessions` record for lookup

**Files to modify:** `schedulers/weekly_review_scheduler.py` (_send_notification, _check_cycle), `processors/weekly_review_session.py` (start_weekly_review)

---

## 4. HIGH — HTML Report Token Expiry and Access Logging

**Current behavior:** Each HTML report gets a `secrets.token_urlsafe(32)` URL. No expiry. No access logging.

**Problem:** A report URL shared 6 months ago still works. Weekly reports contain CropSight operational data — tasks, decisions, Gantt status, milestones, commitment scorecards. An indefinitely-valid URL to this data is an unnecessary security exposure, especially since the URL might be shared in email or Telegram group messages that could be accessed by future team members or compromised accounts.

**Required changes:**
- Add `expires_at TIMESTAMPTZ` column to `weekly_reports` table (default: 30 days after generation). Add to Phase 6 migration or create a small follow-up migration.
- In the health server `/reports/weekly/{token}` handler: check `expires_at`. If expired, return a friendly HTML page: "This report has expired. Contact the CropSight team for access." (not a raw 404)
- Add basic access logging: store `last_accessed_at`, `access_count` on the weekly_reports record. Update on each GET. Not a full access log table — just enough to notice unusual patterns.
- Consider: should expired reports be automatically cleaned up from Supabase by the orphan_cleanup_scheduler? If so, add a sweep for `weekly_reports WHERE expires_at < NOW() - INTERVAL '7 days'`.

**Files to modify:** `services/health_server.py` (expiry check + access logging), `services/supabase_client.py` (update access tracking), `scripts/migrate_phase6.sql` (add expires_at column if not present), `schedulers/orphan_cleanup_scheduler.py` (optional expired report cleanup)

---

## 5. HIGH — PPTX Bytes in Supabase Will Hit Storage Limits

**Current behavior:** PPTX binary data is stored directly in the `weekly_reports` table as bytes.

**Problem:** A formatted PPTX with multiple section tables and color coding can be 500KB-2MB. At one review per week, that's 25-100MB/year in PPTX bytes alone. Supabase free tier has a 500MB database limit. Combined with pgvector embeddings (the primary storage consumer — 1536 floats per chunk), the database will approach capacity faster than expected. Storing binary files in a relational database is also operationally inefficient — it slows backups and increases query overhead.

**Required change:**
- Store PPTX in Google Drive immediately on generation (not just on distribution approval). Use the existing `GANTT_SLIDES_FOLDER_ID` Drive folder.
- Store the Drive file ID in `weekly_reports.pptx_drive_id` instead of raw bytes
- On distribution approval, the file is already in Drive — just update sharing permissions and include the link
- On correction/regeneration during Part 3, upload the new version to Drive (overwrite or create new version) and update the file ID
- This also makes the PPTX immediately accessible via Drive link in the T-30min notification, which is useful

**Fallback consideration:** If Drive upload fails during pre-generation, keep the bytes temporarily in Supabase as a fallback and retry Drive upload on distribution. But don't keep bytes permanently.

**Files to modify:** `processors/gantt_slide.py` (upload to Drive on generation), `processors/weekly_review_session.py` (use Drive ID instead of bytes), `services/supabase_client.py` (replace pptx_bytes with pptx_drive_id), `guardrails/approval_flow.py` (distribute uses Drive link)

---

## 6. HIGH — Correction Parsing Should Use Haiku, Not Sonnet

**Current behavior:** In Part 3 of the review session, Eyal's corrections ("Change Moldova row to blocked", "Update slide title to Q1 Review") are parsed by Sonnet to identify what needs changing, then Sonnet regenerates the affected output.

**Problem:** Correction instructions are typically simple find-and-replace or status-change commands. Using Sonnet for parsing is expensive when 5-6 corrections push the review cost toward $0.50+. The parsing step ("understand what Eyal wants to change") is a classification/extraction task — ideal for Haiku. The regeneration step ("actually produce the new output") still benefits from Sonnet.

**Required change:**
- Split the correction flow into two steps:
  1. **Parse** (Haiku): classify the correction type (text change, status change, structural change) and extract the target + new value. ~$0.001/correction.
  2. **Regenerate** (Sonnet): apply the parsed change and regenerate the affected output. ~$0.03/regeneration.
- This cuts the per-correction cost roughly in half when corrections are simple (most will be)
- For complex corrections that Haiku can't parse confidently, fall back to Sonnet for both steps

**Files to modify:** `processors/weekly_review_session.py` (correction parsing), `core/weekly_review_prompt.py` (add Haiku prompt for correction classification)

---

## 7. MEDIUM — Silent Digest Fallback Should Notify Eyal

**Current behavior:** If no weekly review calendar event is found this week, the `weekly_digest_scheduler` runs on Friday as a fallback, generating a simpler digest instead of the full interactive review.

**Problem:** The two outputs are very different — the digest is a basic summary, the review is a 5-section compiled analysis with HTML report, PPTX, interactive session, and Gantt proposals. If Eyal expects a review (because he usually does one) and gets a bare digest because the calendar event was accidentally deleted, renamed, or created with a non-matching title, that's a confusing experience. The system silently downgraded without explanation.

**Required change:**
- When the weekly review scheduler detects no review event on the expected day (Friday, or whatever day is configured), send a Telegram DM:
  "I don't see a Weekly Review event on your calendar this week. Want me to run a review anyway? [Run full review] [Just send digest] [Skip this week]"
- If no response within 4 hours → default to sending the digest (current behavior, but now Eyal was notified)
- Log the reason: "Weekly review skipped — no calendar event found. Digest sent as fallback."

**Files to modify:** `schedulers/weekly_review_scheduler.py` (_check_cycle), `main.py` (coexistence logic)

---

## 8. MEDIUM — Incomplete Review Session Expiry

**Current behavior:** Sessions can be resumed from the last completed part. "Same-week" sessions resume, "different-week" sessions are cancelled.

**Problem:** The definition of "same-week" vs "different-week" is ambiguous. If the review event is Friday and Eyal starts but doesn't finish, then returns Sunday — is that the same week? In ISO 8601, Monday is day 1, so Friday and Sunday are the same week. But in the Israeli work week, Sunday is the first day of the NEW work week. The system could resume a stale Friday session on Sunday when Eyal expects a fresh state.

**Required change:**
- Use a simple time-based expiry instead of week-boundary logic: sessions expire **48 hours** after creation, regardless of week boundaries
- On `/review` with an expired session: "Your previous review session from [date] has expired. Starting a fresh review."
- Configurable via `WEEKLY_REVIEW_SESSION_EXPIRY_HOURS = 48` in settings
- The orphan_cleanup_scheduler should mark expired review sessions as `status='expired'`

**Files to modify:** `processors/weekly_review_session.py` (start_weekly_review expiry check), `schedulers/orphan_cleanup_scheduler.py` (session expiry sweep), `config/settings.py` (expiry setting)

---

## 9. MEDIUM — Review Data in Telegram Should Be Condensed

**Current concern:** The 3-part Telegram flow generates substantial messages for each part. Part 1 alone includes week stats, attention items, and horizon check — potentially 30+ lines of text. With inline buttons interspersed, scrolling through this on a phone is a noisy experience.

**Connection to meeting prep brainstorm:** This is the same "Telegram is for deciding, Drive is for reading" principle we identified for meeting prep. The HTML report already exists and is pre-generated — it's the ideal place for detailed data presentation.

**Proposal to consider:**
- Parts 1-2 in Telegram should be condensed briefing cards (10-15 lines each) with a "Full details in report" link to the HTML report
- The interactive decisions (Gantt approve/reject in Part 2) still happen in Telegram — those are actions, not reading
- Part 3 (outputs + corrections) stays as-is since it's inherently interactive
- This reduces Telegram message bloat while keeping the interactive decision-making where it belongs

**This is a UX improvement, not a bug fix — consider addressing after live QA confirms the core flow works.**

**Files to modify (if adopted):** `processors/weekly_review_session.py` (Telegram formatting for Parts 1-2)

---

## 10. MEDIUM — Scheduler Coexistence Needs Explicit Mutex

**Current behavior:** Both `weekly_review_scheduler` and `weekly_digest_scheduler` run as background asyncio tasks. The doc mentions "scheduler coexistence logic" in `main.py` but doesn't specify the mechanism.

**Problem:** If the review scheduler detects an event and creates a session, but the digest scheduler's Friday 14:00 trigger fires before the review is complete, both could produce outputs for the same week. Or if the review session is started but never completed (Eyal skips it), the digest should fire as fallback — but how does it know the review was abandoned vs. still in progress?

**Required change:**
- Add an explicit flag: `review_active_this_week` stored in Supabase (or derived from `weekly_review_sessions` with `status IN ('ready', 'in_progress')` for the current week)
- Digest scheduler checks: if a review session exists for this week (any status except 'expired' or 'cancelled'), skip the digest
- If the review session is completed (`status='completed'`), the digest is redundant — skip
- If the review session is expired or cancelled AND no digest was sent → send the digest
- Document this logic clearly in code comments

**Files to modify:** `main.py` (coexistence logic), `schedulers/weekly_digest_scheduler.py` (check for active review), `schedulers/weekly_review_scheduler.py` (set review_active flag)

---

## 11. LOW — Morning Brief Should Check Calendar Directly for Review Events

**Current behavior:** The morning brief surfaces pending review sessions by querying `weekly_review_sessions` in Supabase.

**Problem:** If the review event is added to the calendar at 08:00 Friday (after the 07:00 morning brief ran), the morning brief won't mention "you have a review today." The 15-minute review scheduler will eventually catch it, but the morning brief — Eyal's primary daily orientation — misses it.

**Suggestion:** In the morning brief compilation, also check the calendar directly for review events today (don't rely solely on the review scheduler having already created a session). If a review event is found but no session exists yet: "You have a Weekly Review at [time] today. Gianluigi will start prep 3 hours before."

**Files to modify:** `processors/morning_brief.py` (add calendar check for review events)

---

## 12. LOW — Hebrew Calendar Event Title Testing

**Current behavior:** `_find_review_event()` has three matching strategies: exact title match, fuzzy word match (60%+), and Haiku fallback for non-Latin titles.

**Concern:** If Eyal creates the calendar event with a Hebrew title (plausible — he operates in Israel), the fuzzy word match will fail on Hebrew characters, and Haiku needs to correctly determine that a Hebrew title means "weekly review." This is a legitimate edge case that should be tested during live QA.

**Action:** Add to the live QA checklist: create a review event with a Hebrew title (e.g., "סקירה שבועית עם ג'אנלואיג'י" or similar) and verify the Haiku fallback correctly identifies it. If it fails, add the Hebrew title to the exact-match list in settings.

---

## Summary — Suggested Execution Order

1. **Item 1** — Gantt backup before write (critical data safety, small code change)
2. **Item 2** — Session stack persistence (critical restart resilience)
3. **Item 3** — Calendar re-verification (critical for correct timing)
4. **Item 5** — PPTX to Drive instead of Supabase bytes (prevents storage limit issues)
5. **Item 4** — HTML report token expiry (security hardening)
6. **Item 6** — Haiku for correction parsing (cost optimization)
7. **Item 10** — Scheduler coexistence mutex (prevents duplicate outputs)
8. **Item 7** — Silent fallback notification (UX improvement)
9. **Item 8** — Session expiry logic (operational clarity)
10. **Item 9** — Telegram condensed briefing cards (UX, defer to post-QA)
11. **Item 11** — Morning brief calendar check (nice-to-have)
12. **Item 12** — Hebrew title testing (QA checklist item)

---

*Add this document to the repo (e.g., `docs/qa/PHASE6_REVIEW_ISSUES.md`) and reference from CLAUDE.md. Address items 1-3 before live QA testing.*
