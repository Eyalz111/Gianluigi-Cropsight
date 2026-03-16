# Gianluigi v1.0 — Post-Phase 4 Architecture Review
# Issues, Fixes & Enhancements for Claude Code

**Date:** March 16, 2026
**Context:** Architecture review of system state after completing Phases 0–4 (933 tests passing). These items were identified by reviewing `system_architecture_v1.md` against the V1_DESIGN.md spec and examining failure points and operational risks.
**Priority:** Items are ordered by severity — address critical items before moving to Phase 5.

---

## 1. CRITICAL — Disable Auto-Publish on Meeting Summaries

**Current behavior:** Meeting summaries auto-publish to the full team (Telegram group + Gmail + Drive + Sheets) after 60 minutes if Eyal doesn't respond to the approval request.

**Problem:** At this maturity level, Opus extraction can still hallucinate tasks, misattribute decisions, or let personal content slip through the tone guardrails. Auto-publishing unreviewed content to Yoram and Paolo — who aren't technical and will take Gianluigi outputs at face value — is a trust risk. One bad auto-published summary could undermine team confidence in the entire system.

**Required change:**
- Remove the 60-minute auto-publish timer from `guardrails/approval_flow.py`
- Meeting summaries should remain in `pending` state indefinitely until Eyal explicitly approves or rejects
- Add a gentle reminder instead: if no response after 2 hours, send a follow-up Telegram DM: "Reminder: meeting summary for '[title]' is still waiting for your review"
- Repeat reminder once more at 6 hours, then stop
- Keep the auto-publish mechanism in code (behind a feature flag / config setting in `config/settings.py`) for re-enabling later when quality is proven after 10–15 clean manual approvals
- Add a config variable: `AUTO_PUBLISH_ENABLED = False` (default) and `AUTO_PUBLISH_TIMEOUT_MINUTES = 360` (for when re-enabled)

**Files to modify:** `guardrails/approval_flow.py`, `config/settings.py`

---

## 2. CRITICAL — Conversation Memory Drift in Multi-Turn Flows

**Known issue from v0.5:** Multi-turn conversations (approval editing, debrief sessions, Q&A follow-ups) accumulate formatting artifacts and data handling errors as conversation history grows.

**What to verify and fix:**
- Confirm that the debrief processor (Phase 3, `processors/debrief.py`) uses **structured session state** (the `debrief_sessions` Supabase table with `items_captured`, `pending_questions`, etc.) rather than raw message history. The V1_DESIGN.md specifies this pattern explicitly — verify it was implemented correctly.
- Check the **approval edit flow** in `guardrails/approval_flow.py` — when Eyal sends edit instructions like "Change task 3 deadline to March 5", does the system pass the full raw conversation history to Claude, or does it pass only: (a) the current structured draft, (b) the edit instruction, and (c) minimal context? It should be the latter.
- Check the **Telegram conversation agent** flow — when a user has a multi-turn Q&A, does `services/conversation_memory.py` cap history length? It should maintain a sliding window of the last 3–5 exchanges maximum, plus the structured context from tool calls.
- If any of these are using unbounded raw message history, refactor to structured state + recent-messages-only pattern.

**Design principle (from V1_DESIGN.md Section 4.3.3):** "Each turn, the Conversation Agent receives: (1) the structured session state (NOT full conversation history), (2) the last 2-3 messages (for conversational continuity), (3) relevant context data." This pattern should apply to ALL multi-turn flows, not just debriefs.

**Files to check:** `processors/debrief.py`, `guardrails/approval_flow.py`, `services/conversation_memory.py`, `core/agent.py`

---

## 3. CRITICAL — System Health Monitoring, Error Alerting & QC Tracing

**Problem:** The system has 8 background schedulers, 4 external API dependencies (Google, Supabase, Anthropic, OpenAI), and multiple processing pipelines running autonomously. If any component fails silently — a scheduler crashes, an API token expires, a Supabase query times out — there's no proactive alerting mechanism to notify Eyal. There's also no easy way to trace errors back through the processing pipeline for quality control.

**Required implementation — three layers:**

### Layer 1: Health Monitoring Dashboard (daily Telegram report)
Every morning (as part of the morning brief or as a separate 07:00 message), Gianluigi should send a brief system health status:
```
System status — all green ✓
├─ Schedulers: 8/8 ran on schedule
├─ Last transcript processed: 3 hours ago
├─ Last email scan: 6:55 AM
├─ Supabase: connected, 0.2% of free tier (1.0 MB / 500 MB)
├─ Google APIs: all tokens valid
├─ Pending approvals: 1 (meeting summary from yesterday)
└─ Errors last 24h: 0
```
If any component is unhealthy, elevate to an **immediate alert** (don't wait for morning brief).

### Layer 2: Error Alerting (real-time Telegram DMs)
When critical errors occur, send an immediate Telegram DM to Eyal:
- **API failures:** "⚠️ Google Drive API returned 403 — token may need refresh. Transcript watcher paused."
- **Scheduler failures:** "⚠️ personal_email_scanner failed to run at 07:00 — error: [brief description]. Morning brief will be missing personal email data."
- **Processing failures:** "⚠️ Transcript processing failed for 'Moldova Technical Review' — Claude API timeout. File queued for retry."
- **Token expiry:** "⚠️ Personal Gmail OAuth token is invalid. Daily email scan disabled until re-authenticated. Run: [instructions to refresh]"

Implement with a centralized error handler that wraps all scheduler and processor functions. Store errors in a new `system_errors` table or extend the existing `action_log` with an error severity field.

### Layer 3: QC Tracing (error chain reconstruction)
When something goes wrong in the pipeline (e.g., a task was created with wrong assignee, or an email was misclassified), Eyal needs to be able to trace back:
- What was the source input? (transcript, email, debrief)
- What did the LLM extract? (raw extraction JSON)
- What did cross-reference change? (dedup decisions)
- What was approved? (approval record)
- What was distributed? (distribution log)

This requires:
- Storing the **raw LLM extraction output** (the full JSON response from Opus/Sonnet) alongside the processed records in Supabase, linked by a `processing_run_id`
- A `/trace <item_id>` Telegram command that shows the full chain: source → extraction → cross-reference → approval → distribution
- Logging enough context in `action_log` that each step can be reconstructed

**New Supabase table suggestion:**
```sql
CREATE TABLE processing_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL,  -- 'transcript', 'email', 'debrief', 'document'
    source_id UUID,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT DEFAULT 'running',  -- 'running', 'completed', 'failed', 'partial'
    raw_extraction JSONB,  -- full LLM response
    cross_reference_changes JSONB,  -- what dedup/inference changed
    items_created JSONB,  -- IDs of tasks, decisions, etc. created
    error_details TEXT,
    model_used TEXT,
    tokens_used INTEGER
);
```

**Files to create/modify:** New `core/health_monitor.py`, new `core/error_handler.py`, extend `services/telegram_bot.py` with `/trace` command, extend `services/supabase_client.py` with `processing_runs` table operations, extend all scheduler files to wrap execution in error handler.

---

## 4. HIGH — OAuth Token Health Monitoring (Personal Gmail)

**Problem:** Eyal's personal Gmail uses a separate OAuth flow with `gmail.readonly` scope. Google OAuth refresh tokens can expire or get revoked if:
- The token isn't used for 6 months (Google's inactivity policy)
- Eyal changes his Google password
- Eyal revokes access from Google Account settings
- Google's OAuth consent screen is still in "testing" mode (tokens expire after 7 days for test users — verify this has been moved to production or the 4 team members are added as test users)

If the token silently fails, the morning brief loses its most valuable data source and Eyal won't know.

**Required changes:**
- In `schedulers/personal_email_scanner.py`: wrap the Gmail API call in a try/except that specifically catches `google.auth.exceptions.RefreshError` and `HttpError 401/403`
- On token failure: immediately send Telegram DM alert (see item #3 above), disable the personal scanner gracefully, and include a note in the morning brief: "⚠️ Personal email scan unavailable — OAuth token needs refresh"
- In the daily health check (item #3): validate both OAuth tokens (Gianluigi's Gmail + Eyal's personal) by making a lightweight API call (e.g., `users.getProfile`)
- Store token health status in memory or a simple config table so it's available for the `/status` command

**Also check:** Is the Google OAuth consent screen still in "testing" mode? If so, refresh tokens expire after 7 days. This needs to be moved to production or all test users need to be re-added periodically. This is a common gotcha that can cause sudden silent failures.

**Files to modify:** `schedulers/personal_email_scanner.py`, `services/gmail.py`, add token validation to health monitor

---

## 5. HIGH — Relax All Polling Intervals (Capacity-Appropriate)

**Problem:** The current polling intervals are far too aggressive for CropSight's actual capacity:
- Transcript watcher: 30 seconds (= 2,880 Drive API calls/day)
- Email watcher: 5 minutes
- Document watcher: 5 minutes
- Meeting prep scheduler: every hour

Google Drive API free tier allows 12,000 requests/day. The transcript watcher alone consumes ~24% of that quota before any actual file operations. With the document watcher adding another ~288/day, plus all read/write operations, you're burning API quota unnecessarily.

More importantly: CropSight has ~10 meetings/month and ~50 emails/day. There's no scenario where a 30-second polling cycle adds meaningful value — Tactiq uploads aren't time-sensitive.

**Recommended intervals:**
| Scheduler | Current | Recommended | Reasoning |
|-----------|---------|-------------|-----------|
| transcript_watcher | 30s | **3 minutes** | Tactiq upload takes time anyway. 3 min is still responsive. Cuts API calls from 2,880 to 480/day. |
| email_watcher | 5 min | **10 minutes** | Emails aren't urgent. 10 min delay is imperceptible for this use case. |
| document_watcher | 5 min | **15 minutes** | Document uploads are rare and never time-critical. |
| meeting_prep_scheduler | 1 hour | **2 hours** | Prep docs are generated ~24 hours before meetings. 2-hour check is more than sufficient. |
| orphan_cleanup | 6 hours | **12 hours** | Twice daily is enough for DB hygiene. |

**Implementation:**
- Make ALL intervals configurable via `config/settings.py` environment variables (e.g., `TRANSCRIPT_POLL_INTERVAL_SECONDS=180`)
- Set sensible defaults as above
- Document the API quota math in a comment so future changes can be evaluated
- Consider a future migration to Google Drive push notifications (webhooks) for the transcript watcher — this would eliminate polling entirely, but it's a bigger change

**Longer-term:** Once the system is stable, consider switching the transcript and document watchers to Google Drive push notifications (change notifications via `files.watch`). This eliminates polling entirely and is more efficient, but requires a webhook endpoint on Cloud Run.

**Files to modify:** `config/settings.py`, `schedulers/transcript_watcher.py`, `schedulers/document_watcher.py`, `schedulers/email_watcher.py`, `schedulers/meeting_prep_scheduler.py`, `schedulers/orphan_cleanup_scheduler.py`

---

## 6. HIGH — Cloud Run Instance Resilience

**Current state:** Single Cloud Run instance (`min-instances=1`). If the instance crashes or restarts during a multi-step operation (mid-debrief, mid-Gantt-write, mid-transcript-processing), in-flight state could be lost.

**What's already good:** Pending approvals are persisted in Supabase with timer reconstruction on restart. Debrief sessions (if correctly implemented per V1_DESIGN) have session state in Supabase.

**What needs verification and hardening:**
1. **Verify debrief session persistence:** Confirm that `debrief_sessions` table is updated after EVERY user turn in the debrief conversation, not just at the end. If the instance restarts mid-debrief, Eyal should be able to type `/debrief` again and resume where he left off ("I see you had an active debrief session. You had captured 3 items. Want to continue?")
2. **Transcript processing atomicity:** If the instance crashes between Step 4 (store to Supabase) and Step 8 (submit for approval), the meeting record exists in the DB but was never sent for approval. On restart, the system should scan for meetings with `approval_status='pending'` that don't have a corresponding `pending_approvals` record, and re-submit them.
3. **Gantt write atomicity:** If the instance crashes after snapshot but before write, or after partial write — verify that the rollback mechanism can handle this. The `gantt_snapshots` table should have a `write_completed` boolean that's set only after all cells are written.
4. **Scheduler state on restart:** Verify that all 8 schedulers properly reconstruct their state on Cloud Run cold start. Specifically: does the morning brief know it already ran today? Does the weekly digest know it already sent this week? Use Supabase `action_log` to check "was this already done today/this week?" before running.

**Implementation:**
- Add a `startup_recovery()` function to `main.py` that runs on every boot:
  - Check for orphaned processing runs (started but not completed)
  - Check for pending approvals that were never sent
  - Check for active debrief sessions that need resumption
  - Check scheduler last-run times and adjust schedules accordingly
- Log Cloud Run instance lifecycle events (startup, shutdown signal) to `action_log`

**Files to modify:** `main.py` (add startup recovery), `processors/debrief.py` (verify per-turn persistence), `processors/transcript_processor.py` (verify orphan detection), `services/gantt_manager.py` (verify write atomicity)

---

## 7. HIGH — Unify Schedulers into Configurable Heartbeat System

**Current state:** 8 separate scheduler files, each with their own asyncio loop, timing logic, and error handling. This creates several problems:
- No guaranteed execution order (morning brief and personal email scanner both trigger at 07:00 — email scan MUST complete before brief compilation)
- No centralized health tracking (each scheduler fails independently)
- Timing conflicts possible under load
- Adding new scheduled tasks requires creating a new file and wiring it into `main.py`

**V1_DESIGN.md (Section 5) specifies a unified heartbeat system.** This should be implemented now, before adding more scheduled capabilities.

**Required implementation:**

Create `schedulers/heartbeat.py` — a single scheduler that manages all rhythms:

```python
# Conceptual structure (not exact code)
class HeartbeatScheduler:
    rhythms = {
        "pulse": {
            "interval_seconds": 180,  # configurable via settings
            "tasks": [
                ("transcript_watcher", transcript_watcher.check),
                ("document_watcher", document_watcher.check),
                ("email_watcher", email_watcher.check),
            ],
            "parallel": True  # these can run concurrently
        },
        "morning": {
            "cron": "0 7 * * *",
            "tasks": [
                ("personal_email_scan", personal_email_scanner.run),  # MUST run first
                ("morning_brief", morning_brief.compile_and_send),    # runs after scan
            ],
            "parallel": False,  # sequential — order matters
            "timezone": "Asia/Jerusalem"
        },
        # ... etc
    }
```

Key requirements:
- **Sequential execution where order matters** (morning: email scan → brief compilation)
- **Parallel execution where independent** (pulse: transcript + document + email watchers)
- **Configurable intervals** — all timing values from `config/settings.py`, not hardcoded
- **Health tracking** — record last-run time and status for each task in memory (and periodically to Supabase)
- **Error isolation** — if one task in a rhythm fails, others still run. Failed task logged + alert sent (ties into item #3)
- **Skip-if-already-ran** — on restart, check if a daily/weekly task already ran today/this week before re-running

The existing 8 scheduler files can be preserved as the task implementations, but their asyncio loop/timing logic gets removed — they become simple functions called by the heartbeat.

**Files to create:** `schedulers/heartbeat.py`
**Files to modify:** All 8 existing scheduler files (remove loop/timing, keep logic), `main.py` (start heartbeat instead of 8 separate tasks), `config/settings.py` (add all interval config variables)

---

## 8. MEDIUM — Email Thread Cross-Layer Deduplication Edge Cases

**Current behavior:** The daily scan layer (Eyal's Gmail) checks for thread overlap with the constant layer (Gianluigi inbox) using `thread_id` to avoid processing the same email twice.

**Edge cases not covered:**
1. **Forwarded threads:** If Eyal forwards a personal Gmail thread to `gianluigi@cropsight`, the forwarded email gets a NEW thread_id in Gianluigi's inbox. The constant layer processes it. Then the daily scan also finds the original thread in Eyal's Gmail. Same content, different thread IDs → processed twice.
2. **CC vs. direct:** If someone CCs both Eyal and Gianluigi on the same email, both layers receive it with different thread contexts.
3. **Reply chains:** Eyal replies to a thread from his personal Gmail. The constant layer sees the reply arrive in Gianluigi's inbox (if Gianluigi was CC'd). The daily scan sees it in Eyal's sent mail. Same content, potentially processed differently.

**Required fix:**
- Add a **secondary dedup check** beyond thread_id: match on `subject_normalized + sender + approximate_timestamp` (within 5 minutes)
- In `personal_email_scanner.py`, after the thread_id check, also check: "Is there an `email_scans` record from the constant layer within the last 24 hours with a similar subject line and same sender?"
- Use fuzzy subject matching (strip "Re:", "Fwd:", "FW:" prefixes, lowercase, trim) to catch forwarded/replied versions
- If a duplicate is detected, skip processing but log it: "Skipped — already processed by constant layer (email_scan_id: xxx)"

**Also look for additional edge cases:** Review the full email processing flow for other scenarios where the same information could enter the system through multiple paths. Document any that are found and handle them.

**Files to modify:** `schedulers/personal_email_scanner.py`, potentially `schedulers/email_watcher.py`

---

## 9. MEDIUM — RAG Source Priority Weights

**V1_DESIGN.md (Section 6.2) specifies source priority ranking with specific weights:**
- Debrief: 1.5× (highest — Eyal's direct input)
- Meeting decisions: 1.3×
- Email intelligence: 1.0×
- Document content: 0.9×
- Gantt history: 0.7×

**Current state (from system_architecture_v1.md):** "Source priority: meetings > emails > documents" — the ordering exists but specific weights don't appear to be implemented.

**Required change:**
- In `services/embeddings.py` or wherever the RAG retrieval scoring happens: add a `source_weight` multiplier to the RRF fusion scoring
- Each embedding record has a `source_type` field — use it to look up the weight
- Apply the weight as: `final_score = rrf_score * source_weight * time_weight`
- Make weights configurable in `config/settings.py`

This matters because as debrief sessions produce embeddings alongside meeting transcripts, the system needs to know that "Eyal said in a debrief 2 days ago" should outrank "someone mentioned in a meeting 3 weeks ago" when the information conflicts.

**Files to modify:** `services/embeddings.py` (or wherever RAG retrieval scoring lives), `config/settings.py`

---

## 10. LOW — Daily System Health Telegram Summary

**Separate from the error alerting in item #3** — this is a proactive daily health pulse even when nothing is wrong.

Add to the morning heartbeat (after the morning brief, or as a separate concise message):

```
☀️ Gianluigi daily health — March 16
✓ 8/8 schedulers healthy
✓ Last transcript: 'Moldova Technical Review' (14h ago)
✓ Emails scanned: 12 (3 relevant, queued for brief)
✓ Supabase: 1.0 MB / 500 MB (0.2%)
✓ Google tokens: all valid
✓ Pending approvals: 1
✓ Open tasks: 14 (2 overdue)
```

If anything is unhealthy, the corresponding line shows ⚠️ instead of ✓ with a brief explanation.

This should be:
- Sent to Eyal's Telegram DM only (not the group)
- Configurable on/off via `DAILY_HEALTH_REPORT_ENABLED=True`
- Part of the morning heartbeat rhythm (runs after morning brief)

**Files to create:** `core/health_monitor.py` (shared with item #3)
**Files to modify:** `schedulers/heartbeat.py` (add to morning rhythm)

---

## 11. HIGH — Weekly Digest Schedule: Friday, Not Sunday

**Problem:** The weekly digest is currently scheduled for Sunday 18:00 IST. CropSight operates on the Israeli work week where Sunday is the first business day. The work week ends on Friday. A "weekly digest" sent on Sunday evening is recapping a week that's already a day old and arrives right as the new week starts — the timing undermines its usefulness.

**Required change:**
- Move the weekly digest to **Friday 14:00 IST** (or configurable via `WEEKLY_DIGEST_DAY` and `WEEKLY_DIGEST_HOUR` in `config/settings.py`)
- Friday early afternoon gives Eyal time to review and approve before the weekend
- The digest should recap Sunday–Friday of the current week
- Update all references in code comments, system prompts, and documentation from "Sunday" to "Friday"
- Note: Paolo is based in Italy (standard Mon–Fri work week). The Friday timing works well for him too — he'll see it before his weekend.

**Also check:** Are there any other hardcoded day-of-week assumptions? The morning brief runs "daily" — does that include Saturday? In Israel, Saturday (Shabbat) is a rest day. Consider making the morning brief skip Saturdays by default (configurable: `MORNING_BRIEF_SKIP_DAYS=["Saturday"]`).

**Files to modify:** `schedulers/weekly_digest_scheduler.py` (or heartbeat once unified), `config/settings.py`, any system prompt or docstring referencing "Sunday"

---

## 12. HIGH — Non-Response Resilience (Approval Queue Doesn't Block the System)

**Problem:** Multiple Gianluigi flows depend on Eyal responding to approval requests or interactive prompts. If Eyal is busy, in back-to-back meetings, traveling, or simply not checking Telegram for several hours, the system must not:
- Crash or enter an error state
- Block subsequent scheduled operations
- Lose data that was queued for approval
- Create confusing overlapping conversations in Telegram

This is especially critical when **a new scheduled hook fires while a previous one is still waiting for response.** For example:
- 07:00: Morning brief sent for approval. Eyal doesn't respond.
- 10:30: A new transcript is processed. Meeting summary sent for approval.
- 18:00: Eyal types `/debrief` — but has 2 unanswered approval requests in the queue.
- Next 07:00: A new morning brief fires. Yesterday's morning brief was never approved or dismissed.

**Required behavior — design principles:**

### Principle 1: Approvals queue gracefully, never block
- Every approval request is a self-contained entry in `pending_approvals` — it doesn't depend on previous approvals being resolved
- Multiple pending approvals can coexist without conflict
- Each approval message in Telegram should be identifiable (include timestamp and content type in the message)
- New approval requests are sent regardless of how many are already pending

### Principle 2: Stale approvals expire gracefully
- Morning briefs: if not approved by the next morning, automatically mark as `expired` (the data is stale — yesterday's morning brief is useless today). The items remain in `email_scans` as `approved=False` but are NOT re-included in the next morning brief (avoid duplicate surfacing). Log the expiry.
- Meeting summaries: remain pending indefinitely (content doesn't go stale), but send reminders at 2h and 6h (see item #1)
- Weekly digest: if not approved by Sunday morning (next work week start), mark as `expired` — the moment has passed
- Gantt updates: remain pending indefinitely (structural changes don't expire)
- Debrief items: remain pending for 48 hours, then expire with a Telegram note: "Your debrief from [date] expired without approval. The items were not injected. Type /debrief to re-capture if needed."

### Principle 3: New hooks acknowledge the queue
When a scheduled hook fires and there are pending unanswered items, the new message should acknowledge them briefly:
- Morning brief (with yesterday's brief still pending): "Good morning. Note: yesterday's morning brief was not reviewed and has expired. Here's today's brief: ..."
- EOD debrief (with pending approvals): At the start of the debrief, Gianluigi should say: "Before we start — you have 2 pending approvals (meeting summary from 10:30, morning brief from 07:00). Want to handle those first, or proceed with the debrief?"
- This prevents Eyal from losing track of what's in the queue

### Principle 4: Never send overlapping interactive sessions
Only ONE interactive session (debrief, approval editing) can be active at a time in the Telegram DM. If Eyal is mid-debrief and an approval request arrives:
- Queue the approval notification — don't interrupt the debrief
- After the debrief completes, surface the pending approval: "Debrief complete. By the way, a meeting summary for 'Moldova Review' is waiting for your approval."
If Eyal tries to start a debrief while mid-approval-editing:
- "You're currently reviewing the Moldova meeting summary. Want to finish that first, or save your edits and switch to the debrief?"

### Principle 5: `/status` shows the full queue
The `/status` Telegram command should show all pending items:
```
Pending approvals: 3
├─ Morning brief (today 07:00) — awaiting review
├─ Meeting summary: 'Moldova Review' (today 10:30) — awaiting review, reminder sent
└─ Gantt update: 'Move pilot to week 22' (yesterday 15:00) — awaiting review
Active sessions: none
```

**Implementation notes:**
- The `pending_approvals` table already exists — add columns: `expires_at TIMESTAMPTZ` (nullable, set per content type), `status` values should include `expired` alongside `pending`/`approved`/`rejected`
- Add an expiry check to the heartbeat: every cycle, scan for expired pending approvals and update their status
- In `services/telegram_bot.py`: add a session lock mechanism — a simple `active_interactive_session` flag in memory (with Supabase fallback for restart recovery) that prevents overlapping interactive flows
- In each scheduler that creates an approval: check for and acknowledge existing pending items of the same type before sending the new one

**Files to modify:** `guardrails/approval_flow.py` (expiry logic, queue-aware messaging), `services/telegram_bot.py` (session locking, queue display in /status), `services/supabase_client.py` (add expires_at column, expiry queries), `schedulers/morning_brief_scheduler.py` (handle expired previous briefs), `processors/debrief.py` (check queue before starting), `config/settings.py` (expiry timeouts per content type)

---

## Summary — Suggested Execution Order

1. **Item 1** — Disable auto-publish (quick config change, critical trust issue)
2. **Item 12** — Non-response resilience (critical operational stability — the system must handle Eyal being unavailable)
3. **Item 3** — Health monitoring + error alerting + QC tracing (foundational infrastructure)
4. **Item 4** — OAuth token health (ties into item 3, prevents silent failures)
5. **Item 2** — Verify conversation memory patterns (prevents user-facing quality issues)
6. **Item 11** — Weekly digest to Friday (quick schedule fix, Israeli work week alignment)
7. **Item 5** — Relax polling intervals (prevents API quota issues, quick config changes)
8. **Item 7** — Unified heartbeat (structural improvement, enables everything else)
9. **Item 6** — Cloud Run resilience (startup recovery, orphan detection)
10. **Item 8** — Email dedup edge cases (medium priority, prevents data quality issues)
11. **Item 9** — RAG source weights (improves search quality)
12. **Item 10** — Daily health summary (nice-to-have, builds on item 3)

---

*This document should be added to the repo root and referenced from CLAUDE.md for context during implementation.*
