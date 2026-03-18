# Phase 5 Implementation Plan — Review Feedback

Please address these issues before starting implementation. Items are ordered by severity.

---

## 1. Emergency timeline mode must NOT skip the outline

**Current plan:** 2-6h = "emergency" → Auto-generate immediately, skip outline.

**Required:** The outline proposal must ALWAYS happen. Change emergency mode to: send outline immediately AND start background generation simultaneously. If Eyal responds to outline before generation completes → cancel background, regenerate with his input. If generation completes first → submit for approval with note "Generated with defaults — outline was sent but you didn't respond in time." This was explicitly specified in the design doc: "The outline proposal stage should ALWAYS happen — even if compressed."

---

## 2. "Add focus" session state must survive Cloud Run restarts

**Current plan:** Uses `context.user_data["prep_focus_approval_id"]` (in-memory python-telegram-bot dict).

**Problem:** Cloud Run restart mid-focus-conversation loses the flag. Eyal's next message routes to the regular agent instead of the focus handler.

**Fix:** Store `focus_active: true` in the `pending_approvals` record's `content` JSONB field (Supabase). In `_handle_message`, before debrief/review routing, query: `SELECT * FROM pending_approvals WHERE content_type='prep_outline' AND status='pending' AND content->>'focus_active' = 'true'`. This survives restarts. Clear the flag after processing or after 30-minute timeout. You can keep `context.user_data` as a fast-path cache but Supabase is the source of truth.

---

## 3. "Ask Eyal to confirm meeting type" flow is unimplemented

**Current plan:** `classify_meeting_type()` returns `("meeting_type", "ask")` at score == 2. Sub-Phase 5.4 says "reuse ask_eyal_about_meeting pattern" but no detail anywhere.

**Required implementation:** Collapse the classification question INTO the outline proposal. Instead of a separate interaction step, the outline message says: "I think tomorrow's 'Team call' is a Founders Technical Review (matched by participants: E/R/P/Y). Here's what I'd include: [outline]. [Generate as-is] [Add focus] [Wrong meeting type] [Skip]." If Eyal taps "Wrong meeting type" → show inline options for available templates → store correction in `calendar_classifications` → regenerate outline with correct template. This avoids a separate message waiting for response and keeps the interaction count low.

Add a fourth callback: `prep_reclassify:{approval_id}` → show template options → on selection, update meeting_type, regenerate outline.

---

## 4. SQL migration is missing `meeting_type` column on `meetings` table

The design doc specifies: "Add a `meeting_type TEXT` column to `meetings` table — set during transcript processing when matched to a cadence entry."

This is needed for `"scope": "last_meeting_same_type"` data queries.

Add to `migrate_phase5.sql`:
```sql
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS meeting_type TEXT;
CREATE INDEX IF NOT EXISTS idx_meetings_type ON meetings(meeting_type);
```

Also add a step: when transcript_processor processes a meeting that was matched to a calendar event with a known meeting_type, store the type on the meetings record.

---

## 5. Data queries in outline generation need graceful degradation

If `get_gantt_section("Product & Technology")` fails (API error, section name mismatch, token expired), the entire outline generation shouldn't crash.

Wrap each data query in try/except. On failure, include the section in the outline as "unavailable" rather than crashing:
```python
for query in template["data_queries"]:
    try:
        result = await execute_data_query(query)
        sections.append({"name": query["type"], "data": result, "status": "ok"})
    except Exception as e:
        sections.append({"name": query["type"], "data": None, "status": f"unavailable: {str(e)}"})
        logger.warning(f"Prep data query failed: {query['type']} — {e}")
```

The outline should show: "• Gantt snapshot: ⚠️ unavailable (API timeout)" — still useful with other sections.

---

## 6. Prep reminder timers must survive Cloud Run restarts

**Current plan:** `_pending_prep_timers: dict[str, list[asyncio.Task]]` — in-memory asyncio tasks.

**Problem:** These die on restart, same issue as ARCHITECTURE_REVIEW_ISSUES.md Item 6.

**Fix:** Store `next_reminder_at` timestamp in `pending_approvals.content` JSONB. On startup, `startup_recovery()` reconstructs prep reminder timers from all pending prep_outline records that have a future `next_reminder_at`. Reuse the existing timer reconstruction pattern from `approval_flow.py`.

---

## 7. Prep distribution must check sensitivity classification

Sub-Phase 5.5 distributes to all meeting participants. But if the meeting is classified as sensitive (investor/legal), the prep doc may contain sensitive content that shouldn't go to all participants.

**Fix:** Before distribution, check meeting sensitivity. If sensitive → distribute to Eyal only + Drive. Add a note: "This prep contains sensitive content — not distributed to other participants. Forward manually if appropriate."

---

## 8. Store `event_start_time` in prep outline approval records

Sub-Phase 5.6 says orphan cleanup should auto-generate for future meetings and expire for past meetings. But the cleanup handler needs to know the meeting time.

**Fix:** Include `event_start_time` as a top-level field in `pending_approvals.content` JSONB for all prep_outline records. The expiry check becomes: `if content['event_start_time'] > now() → auto-generate; else → expire silently`.

---

## 9. Handle meeting cancellation and rescheduling

**Missing from plan entirely.** If a calendar event is deleted or moved after an outline was sent:
- Deleted event → Eyal gets "Generate prep for tomorrow's Tech Review" for a meeting that no longer exists
- Rescheduled event → timeline calculations and outline timing are wrong

**Fix:** Add to the scheduler's check cycle: for each pending prep outline, re-verify the calendar event. If deleted → auto-expire with Telegram note: "Tech Review was removed from calendar — prep outline cancelled." If time shifted >2 hours → recalculate timeline mode, send update note. Store the original `event_id` from Google Calendar in the approval record for lookup.

---

## 10. Reject email responses for `prep_outline` content type

Outlines are Telegram-only. But the existing approval flow handles Gmail replies too. If `process_response()` receives an email response for a `prep_outline`, it should return a message: "Prep outlines can only be managed via Telegram" — not try to process it.

---

## 11. Test coverage is too thin — target 120-150 tests, not 85

Current estimate of ~85 tests is light for this complexity. Missing test categories:
- Meeting cancellation/reschedule handling
- Sensitivity-aware distribution
- Focus session persistence across restarts (mock Supabase, not just context.user_data)
- "Ask/reclassify" confidence flow (score == 2 path)
- Data query graceful degradation (individual query failures)
- Full pipeline integration tests (outline → focus → generate → approve → distribute)
- Edge cases: two meetings on same day, back-to-back meetings, same meeting type scheduled twice in a week
- Timer reconstruction on restart

---

## Summary of changes needed:

| # | Severity | Sub-Phase | Change |
|---|----------|-----------|--------|
| 1 | Critical | 5.4 | Emergency mode keeps outline, adds background generation |
| 2 | Critical | 5.3 | Focus session state in Supabase, not just context.user_data |
| 3 | High | 5.3, 5.4 | "Ask" classification merged into outline message + reclassify button |
| 4 | High | 5.0 | Add meeting_type column to meetings table |
| 5 | High | 5.2 | Try/except per data query with graceful degradation |
| 6 | High | 5.4 | Reminder timers persisted in Supabase for restart recovery |
| 7 | Medium | 5.5 | Sensitivity check before distribution |
| 8 | Medium | 5.6 | Store event_start_time in approval records |
| 9 | Medium | 5.4 | Calendar event cancellation/reschedule detection |
| 10 | Low | 5.3 | Guard against email responses for prep_outline |
| 11 | Low | All | Increase test target to 120-150 |
