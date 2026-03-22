# Phase 7.5 Review Notes — Architecture & Implementation Concerns

**Date:** March 22, 2026
**Context:** These are review notes on the Phase 7.5 plan (Weekly Review Migration to Claude.ai MCP). Read alongside the plan itself. Address each point during implementation or explain why it's not needed.

---

## Tool 17: start_weekly_review(force_fresh?)

### Clarify force_fresh scope
`force_fresh` should only recompile the data (fast — Supabase queries + Sheets reads). It should NOT regenerate the HTML report or PPTX slide, which are slow operations (10-20 seconds). Report/PPTX regeneration should happen in `confirm_weekly_review` or during the T-3h prep pipeline only. If `force_fresh` triggers a full regeneration cycle, every mid-review refresh becomes painfully slow.

### Define staleness threshold
The plan includes `stale_warning` but doesn't define the threshold. Suggest: flag as stale if `compiled_at` is more than 4 hours old. This covers the normal case (T-3h prep ran, review happens on schedule) while flagging delayed reviews.

### Return payload — verify commitments are gone
After Phase 7 hardening, commitments were deprecated. But `compile_weekly_review_data()` almost certainly still includes a commitments section (V1 design spec Part 1 lists "Commitments: fulfilled / still open / new"). This needs to be updated:
- Either remove the commitments section from compiled data entirely
- Or rename it to task completion tracking (completed / still open / new this week)

Check `processors/weekly_review_session.py` and `processors/weekly_review.py` for any references to commitments in the compilation logic. This is a small change but important for consistency with the Phase 7 deprecation.

---

## Tool 18: confirm_weekly_review(session_id, approve_gantt?, cancel?)

### MCP timeout risk
This tool chains: `finalize_review()` (HTML + PPTX generation) → `confirm_review()` (Gantt backup → Gantt execution → distribution). Total wall time could be 20-40 seconds. MCP tool calls have timeouts — if the total exceeds Cloud Run's response timeout or Claude.ai's MCP client timeout, the call appears to fail even if it completed server-side.

Mitigation options (pick one):
1. **Skip re-generation if already done.** If `finalize_review()` already ran during T-3h prep and session data hasn't changed since, skip it in `confirm_weekly_review`. Only regenerate if session was updated (e.g., `force_fresh` was called). This keeps the confirm call to ~5-10 seconds (Gantt + distribution only).
2. **Return early, complete async.** Return a "confirmation started" response immediately, complete the work async, and send a Telegram notification when done. More complex but more resilient.

Recommend option 1 — simpler and sufficient.

### Partial failure handling
What happens if confirm partially fails? Example: Gantt backup succeeds, Gantt execution succeeds, but PPTX upload to Drive fails, or email distribution fails.

Rules:
- Never roll back Gantt because distribution failed — those are independent operations
- Return success with warnings using the `_success(warnings=...)` pattern from Phase 7 hardening
- Include per-step status in the response: `{gantt_executed: true, distribution: {pptx: true, digest: true, email: false, telegram: true}, warnings: ["Email distribution failed: SMTP timeout"]}`
- Log failures via `send_system_alert(AlertSeverity.WARNING, ...)` from Phase 7 Batch 7

### Double-session edge case
The plan says the tool "creates or resumes" a session. Verify: if Eyal starts a review in Telegram (creating a session), then opens Claude.ai and calls `start_weekly_review()`, does it resume the same session? It should — keyed by week_number + year. Confirm the session lookup logic uses week/year, not just "latest pending session."

---

## Scheduler Notification Update

### Handle missing report URL
The notification includes a report preview link. If the T-3h prep pipeline's report generation failed (Claude API error, template issue), the notification should still send — just without the preview link.

```
# Don't do:
f"Preview report: {url}"  # crashes if url is None

# Do:
report_line = f"Preview report: {url}\n" if url else ""
```

Small thing, but a None URL in an f-string will print "None" literally in the Telegram message.

---

## Telegram /review Redirect

### Avoid accidental dual session starts
The plan says: show redirect message, then start Telegram flow as fallback. But this means every `/review` command creates a session — even if Eyal just wanted to check if the review was ready, or if he then goes to Claude.ai instead.

Two options:
1. **Prompt before starting:** "The weekly review works best in Claude.ai (CropSight Ops project). Want to continue here in Telegram instead?" Wait for explicit yes before starting the Telegram session.
2. **Rely on session resume:** If the Telegram flow creates a session and Eyal then goes to Claude.ai, `start_weekly_review()` resumes the same session (same week/year key). This works but means the Telegram session is left in a dangling state.

Option 1 is cleaner UX. Option 2 works but is messier. Either way, document the expected behavior.

---

## Claude.ai Project Prompt — Needs Significant Enrichment

The project prompt section in the plan gives the mechanical steps but not the conversational guidance that makes the Claude.ai experience actually good. This is the highest-leverage piece of the whole migration.

### Add: Presentation flow guidance

```
When presenting weekly review data, don't dump everything at once.

Start with a 2-3 sentence executive summary:
"This week you had [N] meetings, [M] decisions captured, [K] tasks completed
([J] overdue). [Highlight: biggest attention item or win]."

Then ask what Eyal wants to dig into. Common flow:
1. Week stats and highlights
2. Attention items (overdue tasks, stale items, alerts)
3. Gantt proposals from this week's meetings
4. Next week preview (meetings, deadlines, prep status)
5. Horizon check (strategic milestones, red flags)

But follow Eyal's lead — if he jumps to a specific topic, go there.
Don't force a sequential walkthrough.
```

### Add: Approval language recognition

```
Eyal may approve using various phrases: "approve", "looks good", "ship it",
"distribute", "go ahead", "✅", "yalla", or similar. Hebrew affirmatives
also count.

On any clear approval intent, ALWAYS confirm before calling confirm_weekly_review:
"I'll approve the review and distribute outputs. This includes [N] Gantt
proposals that will be executed. Proceed?"

If Eyal says "approve but skip Gantt" or "distribute without Gantt changes",
call confirm_weekly_review(session_id, approve_gantt=False).
```

### Add: Error recovery guidance

```
If start_weekly_review() fails:
- Tell Eyal what went wrong (don't hide the error)
- Suggest: (1) retry with force_fresh=True, (2) fall back to Telegram /review,
  (3) check system status with get_full_status()

If confirm_weekly_review() fails:
- Report which step failed (Gantt? Distribution? Report generation?)
- If Gantt executed but distribution failed: "Gantt changes applied successfully.
  Distribution had an issue — I can retry or you can check Telegram."
- Never suggest re-running confirm if Gantt already executed (double-write risk)
```

### Add: Mid-review data refresh

```
If Eyal says "I just completed that task" or "refresh the data":
- For spot checks: use individual tools (get_tasks(), get_gantt_status())
- For full refresh: call start_weekly_review(force_fresh=True)
- Don't call force_fresh for every small question — it recompiles everything

If Eyal makes changes during the review (completes tasks, updates priorities):
- Those changes are in Supabase immediately
- The compiled review data is now stale for those specific items
- Note this: "The task is updated. The review summary still shows the old status
  but the change is saved."
```

### Add: Session summary guidance

```
At the end of the weekly review, call save_session_summary() with:
- Key topics discussed during the review (not the compiled data — what Eyal
  actually focused on)
- Any NEW decisions made during the review conversation itself
- Items Eyal wants to follow up on next week
- Any concerns or priorities Eyal mentioned

Keep it concise — 3-5 bullet points, not a transcript of the conversation.
```

---

## Missing from the Plan

### A. Commitments cleanup in compilation
As noted above — `compile_weekly_review_data()` likely still references commitments. Grep for "commitment" in:
- `processors/weekly_review_session.py`
- `processors/weekly_review.py`
- `processors/weekly_report.py` (HTML report template may have a commitments section)

Update to align with Phase 7's action-items-only model.

### B. Verify actual tool count before setting assertion
The plan says 16 → 18. But confirm the actual current count after Phase 7 hardening deployed. Phase 7 added `get_full_status()` as tool 16. If there were already write tools (propose_gantt_update, approve_gantt_update, etc.), the count might be different. Run the MCP server locally and count registered tools before setting the test assertion.

### C. Test: Claude.ai project prompt in practice
After deploying the code changes, do a dry run of the weekly review in Claude.ai BEFORE the actual Friday review. Call `start_weekly_review()`, walk through the data presentation, and call `confirm_weekly_review(cancel=True)` to test the flow without executing. This catches project prompt issues (Claude.ai doesn't present data well, misinterprets approval language, etc.) before it matters.

### D. Alerting integration
The new MCP tools should integrate with the Phase 7 alerting system (services/alerting.py). If `confirm_weekly_review` fails during Gantt execution, that's a CRITICAL alert. If `start_weekly_review` compilation fails, that's also CRITICAL. Wire `send_system_alert()` into the error handlers of both tools.

### E. Log MCP weekly review actions in audit_log
Both tools should log to the audit_log table:
- `start_weekly_review` → action: "weekly_review_started", details: {session_id, week, force_fresh, source: "mcp"}
- `confirm_weekly_review` → action: "weekly_review_confirmed", details: {session_id, gantt_executed, distribution_status}

This gives you a record of which reviews happened via MCP vs Telegram, useful for understanding usage patterns.

---

## Risk I'd Add to the Table

| Risk | Level | Mitigation |
|------|-------|------------|
| confirm_weekly_review exceeds MCP timeout | Medium | Skip finalize if already done during T-3h prep. Keep confirm to Gantt + distribution only (~5-10s). |
| Commitments references in compiled data | Low | Grep and update. Small code change. |
| Project prompt too thin → bad Claude.ai UX | Medium | Enrich prompt before first real review. Dry run with cancel=True first. |

---

## Summary of Required Actions

**Before implementation:**
1. Grep codebase for "commitment" in weekly review compilation code
2. Verify actual MCP tool count post-Phase-7 deployment
3. Decide on `force_fresh` scope (data-only vs full regeneration)

**During implementation:**
4. Add partial failure handling to confirm_weekly_review (warnings pattern)
5. Add timeout mitigation (skip finalize if already done)
6. Handle None report URL in scheduler notification
7. Wire alerting + audit_log into both new tools
8. Enrich project prompt with presentation, approval, error, and session summary guidance

**After deployment, before first real review:**
9. Dry run in Claude.ai with cancel=True to test the full flow
10. Verify Telegram /review redirect + fallback works
11. Verify scheduler notification mentions Claude.ai
