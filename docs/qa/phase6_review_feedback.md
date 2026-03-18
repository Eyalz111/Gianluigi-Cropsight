# Phase 6 Plan Review — 7 Issues + Open Question Answers

Good plan overall — the sub-phase breakdown is clean, Phase 5 lessons are well-applied, data compilation reuses existing functions heavily, and MCP forward-compatibility is thoughtful. These issues are about practical usability and avoiding rabbit holes.

---

## Issue 1: Condense 7-Part Agenda to 3 Parts for Telegram

Walking through 7 sequential parts on Telegram — each with navigation buttons, inline proposals, corrections — will feel like a wizard, not a meeting. Each part generates a long message, and scrolling through 7 of them on a phone is not a 15-minute review — it's a 30-minute ordeal.

The full 7-part experience is designed for Claude.ai MCP (Phase 7), where you have screen space and rich formatting. On Telegram, condense to 3 parts:

**Part 1: "Here's your week"** (merges current Parts 1 + 3 + 5)
- Week stats (meetings, decisions, tasks completed/overdue)
- Attention needed (overdue items, stale tasks, approaching milestones)
- Horizon check (strategic milestones, red flags)
- One combined message. Eyal reads and can ask questions.

**Part 2: "Decisions needed"** (merges current Parts 2 + 4)
- Gantt update proposals — approve/reject inline
- Next week preview — priorities, calendar, deadlines
- Eyal's input: add items, reprioritize, flag concerns

**Part 3: "Outputs"** (merges current Parts 6 + 7)
- Generate PPTX slide + HTML report + digest
- Show previews
- Corrections if needed
- Final approve → distribute

This is still comprehensive — all the same data — but fits Telegram's linear flow. Navigation becomes simple: [Continue] [Go back] [End review] — no complex 7-way navigation.

The data compilation (Sub-Phase 6.1) stays the same — it compiles all 5 data sections regardless. Only the presentation layer (Sub-Phase 6.2) changes to present 3 consolidated parts instead of 7.

Keep the 7-part structure in the data model and MCP-ready format for Phase 7. On Telegram, it's 3 parts.

---

## Issue 2: Per-Report Access Tokens for HTML Reports

The URL pattern `/reports/weekly/{token}/{week}` uses a single `REPORTS_SECRET_TOKEN` for all reports. If it leaks, all historical reports are exposed.

**Solution:** Generate a unique access token per report:
```python
import secrets
access_token = secrets.token_urlsafe(32)
```

Store in `weekly_reports.access_token`. URL becomes `/reports/weekly/{access_token}` (no week in URL — the token identifies the report). Each report has its own unguessable URL. If one URL is shared or leaked, only that week's report is exposed.

---

## Issue 3: Start with Simplified PPTX Layout

Building a pixel-perfect replica of the reference PPTX (150+ positioned shapes with exact EMU coordinates) is a multi-day rabbit hole. Small coordinate errors = overlapping text, misaligned bars, invisible elements.

**Start with a clean simplified layout:**
- Title header (week, date, "Gianluigi-generated")
- One table per section (rows = items, columns = weeks, cells colored by status)
- Current week column highlighted
- Milestone annotations below each section
- Owner legend
- Footer with metadata

This is still useful and professional for the Friday review. The positioned-shape layout can be a future refinement — or it becomes unnecessary once the HTML report and Claude.ai MCP interface (Phase 7) serve the same visual purpose better.

The PPTX is primarily for sharing with the team or attaching to investor updates. A clean table layout serves that purpose well.

---

## Issue 4: Distribution Must Be Atomic with Gantt Execution

The distribution pipeline runs: upload PPTX → upload digest → backup Gantt → **execute Gantt proposals** → email team → Telegram notify.

If Gantt execution fails mid-pipeline (Sheets API error, protected row), the team gets a digest saying "MVP moved to W22" but the Gantt still shows W20. That's a trust-breaking inconsistency.

**Solution:** Execute Gantt proposals FIRST (before any distribution):
```
1. Execute approved Gantt proposals
2. If any fail → hold everything, notify Eyal: "Gantt update failed. Distribute anyway or fix first?"
3. If all succeed → backup Gantt (post-write) → upload PPTX → upload digest → email → Telegram
```

The Gantt backup should be AFTER the write (so the backup reflects the current state, which now includes the executed proposals). A pre-write backup was already done at T-3h during prep.

---

## Issue 5: Allow Debrief to Interrupt Weekly Review

The `_active_interactive_session` lock prevents starting a debrief during a weekly review. But the real scenario: you're in the Friday review and realize "Wait, I need to mention something from today's meeting."

**Solution:** Allow debrief to pause the review:
1. Save review session state to Supabase (it's already there)
2. Start debrief session
3. On debrief completion, offer to resume review: "Debrief complete. Resume your weekly review?"
4. If resumed, refresh the review data (the debrief may have added tasks/decisions/Gantt proposals that should appear in the review)

Implementation: change `_active_interactive_session` from a lock to a stack:
```python
self._session_stack = []  # ["weekly_review", "debrief"]
```
Current mode is always `_session_stack[-1]`. Pop when session ends. Previous session resumes.

---

## Issue 6: Calendar Event Edge Cases

Add handling for:
- **Cancelled events:** Check event status. If cancelled, don't trigger prep. This is important for holidays or weeks you skip.
- **Renamed events:** The Haiku fallback handles this (good), but also do a fuzzy match on the base string before calling Haiku — saves the API call for minor variations like "Weekly Review with G" vs "CropSight: Weekly Review with Gianluigi".
- **Moved occurrences:** For recurring events with exceptions (one occurrence moved to Thursday), the scheduler should use the occurrence's actual start time, not the series recurrence pattern. Google Calendar API returns this correctly in `event.start.dateTime` even for modified occurrences.
- **Eyal manually starts review:** If Eyal sends `/review` before the scheduler triggers, the session should start immediately with a fresh data compilation. Don't require the calendar event — it's a convenience trigger, not a gate.

---

## Issue 7: Answers to Open Questions

**Q1 — Gantt slide reference file:** The reference PPTX was analyzed in the planning session. It uses auto_shapes (not tables), 16-17 week columns, 5 section rows, diamond/star/bullet milestone markers. But per Issue 3, start with a simplified table-based layout rather than trying to replicate the positioned-shape layout. The reference is good for visual inspiration, not for pixel-perfect reproduction.

**Q2 — Milestone identification:** Milestones are in the "Strategic Milestones" section (rows 7-9 in the 2026-2027 sheet). They use special marker characters in cell text:
- `★` = major technology milestones (e.g., "★ MVP Product Delivery (Q3 2026)")
- `●` = commercial milestones (e.g., "● Gagauzia Alpha Delivery (Aug 2026)")
- `◆` = funding milestones (e.g., "◆ Raising funds: Pre-Seed Round #1")
The schema parser should tag these rows as `subsection_type="milestone"`. The cell parser (from Phase 2) already handles these markers.

**Q3 — Old digest scheduler:** Yes, disable old WeeklyDigestScheduler when `WEEKLY_REVIEW_ENABLED=True`. The weekly review generates the digest as one of its outputs (Part 3/Outputs). No need for both. The old scheduler serves as fallback when the feature flag is off.

---

## Summary

Issue 1 (condense to 3 parts) is the most impactful for usability — it's the difference between a review you'll actually do every Friday and one you'll start skipping after week 2. Issue 4 (atomic distribution) prevents trust-breaking inconsistencies. Issue 5 (debrief interruption) matches real workflow. Issues 2, 3, 6 are quality improvements.

The data compilation layer (Sub-Phase 6.1) is solid and doesn't need changes. The session layer (Sub-Phase 6.2) needs the 3-part condensation. The PPTX (Sub-Phase 6.4) should start simplified. Everything else is good to build.
