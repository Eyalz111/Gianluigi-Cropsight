# Phase 8 Review Notes — Architecture & Implementation Concerns

**Date:** March 22, 2026
**Context:** Review notes on the Phase 8 plan (Operational Intelligence & Production Readiness). Read alongside the plan. Address each point during implementation or explain why it's not needed.

---

## Track A: Extraction Intelligence

### A1 — Task Continuity

**Task selection strategy for the 30-task cap.**
If there are 50+ open tasks, which 30 go into the prompt? Don't just take the most recent 30 — prioritize by relevance to the meeting being processed:
1. Tasks assigned to meeting participants (highest relevance — if Roye is in the meeting, his tasks are most likely to be referenced)
2. Tasks from recent meetings (last 2 weeks — more likely to be discussed)
3. High-priority tasks regardless of assignee

Implement as a simple sort: participant tasks first, then by created_at desc, truncate at 30. This keeps the prompt focused without complex scoring.

**Graceful degradation.**
If `get_tasks()` fails (Supabase timeout, connection error), extraction must proceed without task context — not fail entirely. The prompt builder should handle `existing_tasks=None` or `existing_tasks=[]` cleanly:
```python
existing_tasks_section = ""
if existing_tasks:
    existing_tasks_section = "EXISTING OPEN TASKS:\n" + format_tasks(existing_tasks)
# If empty/None, the section is just omitted from the prompt
```
Don't let a monitoring/enrichment feature become a new failure mode for the core pipeline.

**"UPDATE:" prefix downstream handling.**
The plan introduces an "UPDATE:" prefix convention for tasks that reference existing ones. But what consumes this prefix downstream? The cross-reference code? The Sheets writer? The approval flow formatter? If nothing parses the "UPDATE:" prefix, it's just a hint that improves deduplication but doesn't change behavior. That's fine — but document whether it's meant to be machine-parsed or just a signal for the LLM's own dedup reasoning within the same extraction call.

### A2 — Team Roles

**Role description content.**
The plan says "build team roles string from config/team.py:TEAM_MEMBERS." Make sure the role descriptions are operationally useful, not just titles:
- Good: "Roye Tadmor — CTO. Owns all technical execution: ML models, data pipeline, cloud infrastructure, accuracy metrics."
- Too thin: "Roye Tadmor — CTO"
- Too much: "Roye Tadmor — CTO, bioinformatics background, PhD from Tel Aviv University, previously at..." (irrelevant for task assignment)

The goal is: given an ambiguous task, Claude can figure out who should own it. 2-3 sentences per person covering their domain of responsibility.

**Config location.**
The role descriptions should live in `config/team.py` alongside the existing TEAM_MEMBERS dict, not hardcoded in the prompt. This makes it easy to update when responsibilities shift (e.g., if you hire someone).

### A3 — Escalation Rules

**Priority-aware tier thresholds.**
The plan defines time-based tiers (1-3d low, 4-7d medium, etc.). But a high-priority task that's 3 days overdue is more urgent than a low-priority task that's 10 days overdue. Consider making thresholds priority-sensitive:

| Priority | Low (mention) | Medium (attention) | High (alert) | Critical (escalation) |
|----------|--------------|-------------------|--------------|----------------------|
| H | 1-2 days | 3-5 days | 6-10 days | 11+ days |
| M | 1-3 days | 4-7 days | 8-14 days | 15+ days |
| L | 3-7 days | 8-14 days | 15-21 days | 22+ days |

This doesn't require complex code — a dict of `{priority: {tier: threshold_days}}` and a lookup. Much more operationally useful than flat thresholds.

**Alert scheduler status.**
The plan notes "Alert scheduler currently disabled" as mitigation for A3's risk. Make sure A3 doesn't depend on the alert scheduler being enabled to be useful. The escalation tiers should also feed into the weekly review's attention_needed section (which A3 mentions), so even if proactive alerts are disabled, the information surfaces during the Friday review.

### A4 — Weekly Review Hygiene

**No concerns.** Clean, simple, valuable. The two new query methods directly feed the weekly review compilation. The only thing: make sure the unassigned/no-deadline tasks are presented as actionable prompts in the Claude.ai project prompt — not just data. Something like: "These 3 tasks have no assignee. During the review, ask Eyal to assign them or mark them as cancelled."

### D2 — Hebrew Extraction

**No concerns.** Necessary, additive, 20 minutes. One note: the instruction says "Translate Hebrew titles, descriptions, assignees to English." For assignee names, these are proper nouns (Roye, Paolo, Eyal, Yoram) — they should stay as-is, which the "keep proper nouns" rule covers. Just make sure the instruction is clear that person names are proper nouns and should not be translated or transliterated differently.

---

## Track B: MCP Write Tools

### B1 — Task CRUD

**Google Sheets sync.**
When a task is updated via MCP (e.g., status changed to "done", deadline moved), does the Google Sheets Task Tracker also get updated? Two scenarios:
1. If `supabase_client.update_task()` only writes to Supabase → the MCP tool also needs to trigger a Sheets update, otherwise DB and Sheets drift apart.
2. If task updates already have a Sheets sync path (e.g., in the approval flow or a hook), verify that path is reachable from the MCP context.

Check the existing `update_task` flow in `supabase_client.py` and trace whether Sheets gets updated. If not, add a Sheets sync call in the MCP tool after the Supabase write. This is easy to miss and would create a confusing inconsistency where Eyal updates a task in Claude.ai but the Sheets tracker still shows the old status.

**Status transition validation.**
Allow all transitions (including done → pending for reopening), but add a note in the response for unusual ones:
- done → pending: "Reopening completed task."
- pending → done (with no intermediate): fine, tasks often go straight to done.
- Any → cancelled: fine.

Don't block transitions — just surface them so Eyal knows what happened.

**Deadline parsing.**
The plan mentions "parse deadline string to date." Be explicit about accepted formats in the MCP tool description: "Accepts: 'March 30', 'next Friday', '2026-04-15', 'end of month'. Relative dates resolved against today." Claude.ai will pass natural language dates — the parser needs to handle them. Consider using `dateutil.parser` with a fallback to manual parsing of relative terms.

### B2 — Quick Inject

**Edit before confirm flow.**
The two-step pattern (extract → confirm) is right. But there's a gap: what if Eyal wants to edit the extraction before confirming? "Change the assignee to Paolo" or "That's not a task, that's just information."

The `confirm_quick_inject(items)` tool accepts items as input — so Claude.ai can modify the items based on Eyal's verbal feedback before calling confirm. But the project prompt needs to guide this explicitly:

```
Quick Inject workflow:
1. Call quick_inject(text) → returns extracted items
2. Present items to Eyal: "I extracted: [task] Schedule call with Milano contact, assigned to Paolo. [decision] Postpone product demo to April. Correct?"
3. If Eyal requests changes, modify the items dict accordingly
4. Only call confirm_quick_inject(items) after Eyal approves
5. NEVER auto-confirm — always present and wait for approval
```

Without this prompt guidance, Claude.ai might call extract and immediately confirm in the same turn.

**Items schema.**
The plan says Tool 22 takes `items` — define the schema clearly in the tool description so Claude.ai knows what to pass. Should match whatever `process_quick_injection()` returns: likely `[{type: "task"|"decision"|"info"|"gantt_update", text: str, assignee?: str, ...}]`.

### B3 — Gantt Propose

**Changes schema definition.**
The `changes` parameter needs a clear schema in the MCP tool description. Claude.ai will construct this payload, so it needs to know the exact structure:
```json
{
  "changes": [
    {
      "section": "Product & Technology",
      "subsection": "Execution",
      "item": "ML Pipeline",
      "week": 14,
      "old_value": "Active",
      "new_value": "Completed",
    }
  ],
  "reason": "Completed per discussion in Founders Technical Review (March 20)"
}
```

If the schema is underspecified in the tool description, Claude.ai will guess and produce malformed proposals that `gantt_manager` rejects.

**Approval response detail.**
After `approve_gantt_proposal`, the response should include what actually changed — not just "executed." Something like:
```json
{
  "executed": true,
  "changes_applied": [
    "Product & Tech → Execution → ML Pipeline: 'Active' → 'Completed' (W12)"
  ],
  "snapshot_id": "uuid"
}
```
This gives Eyal confirmation of what happened and a reference for rollback if needed.

**Conflict UX.**
The plan mentions "non-empty cells → needs_confirmation." Make sure the MCP tool response clearly explains the conflict: "Cell W14 in ML Pipeline currently contains 'Active'. Your proposal would change it to 'Completed'. Confirm?" Don't just return `needs_confirmation: true` without context — Claude.ai needs enough information to explain the conflict to Eyal.

---

## Track C: Monitoring

### C1 — Health Monitoring

**Heartbeat writes must be fire-and-forget.**
The one-line heartbeat call in each scheduler must never crash the scheduler if it fails:
```python
try:
    supabase_client.upsert_scheduler_heartbeat(name, status="ok")
except Exception:
    pass  # Never let monitoring kill the thing being monitored
```
This is one of the rare cases where a bare `except: pass` is correct — the heartbeat is monitoring, not business logic.

**Stale detection threshold.**
The plan says "2x interval" for stale detection. Make sure this accounts for scheduler variation:
- Pulse scheduler (5 min interval) → stale after 10 min. Fine.
- Weekly review scheduler → stale after 2 weeks? That's too long. Weekly schedulers should have a "last expected run" based on calendar events, not just 2x interval.

For non-periodic schedulers (like weekly_prep which is calendar-driven), stale detection should compare against the last calendar event time, not against a fixed interval.

**Wire into existing alerting.**
The health data should feed into the Phase 7 alerting system. If a scheduler is stale in the morning health check, include it as a WARNING in the daily alert:
```
⚠️ Scheduler 'transcript_watcher' last ran 25 minutes ago (expected: every 5 min)
```
The `get_system_health()` MCP tool is for on-demand checks; the alerting system handles proactive notification.

### C2 — Cost Monitoring

**Prompt caching discount.**
The pricing table should include the cache discount multiplier. You're using `cache_control: {"type": "ephemeral"}` on system prompts — cached input tokens are 90% cheaper. If the cost calculator doesn't account for this, it will overestimate costs. Check whether `token_usage` records distinguish cached vs. uncached input tokens. If not, you may need to estimate (e.g., assume 70% cache hit rate for Opus calls with system prompt caching).

**Pricing table maintenance.**
Add a comment in `cost_calculator.py`:
```python
# Pricing as of March 2026. Verify against Anthropic's pricing page if numbers seem off.
# https://docs.anthropic.com/en/docs/about-claude/models
# Includes: prompt caching multipliers (cache write: 1.25x, cache read: 0.1x)
```

**Daily cost alert integration.**
The $5/day threshold alert should use the Phase 7 alerting system:
```python
if daily_cost > DAILY_COST_ALERT_THRESHOLD:
    await send_system_alert(
        AlertSeverity.WARNING,
        "cost_monitor",
        f"Daily API cost ${daily_cost:.2f} exceeds ${DAILY_COST_ALERT_THRESHOLD} threshold"
    )
```
Where does this check run? During the morning heartbeat makes sense — check yesterday's cost, alert if over threshold.

**Weekly review cost display.**
The plan includes cost_summary in the weekly review data. Make sure the project prompt tells Claude.ai how to present it: "API costs this week: $18.40 (Opus: $12.10, Sonnet: $5.20, Haiku: $1.10). Trend: up 15% from last week ($16.00)." Don't just dump raw numbers — context (trend, per-model breakdown) makes it actionable.

---

## Track D: DB Hardening

### D1 — tsvector Hebrew Fix

**Migration impact.**
Dropping and recreating generated tsvector columns will briefly lock the affected tables (embeddings, decisions). At your current data volume this is fast (seconds), but:
- Run during a quiet period (not during a meeting processing or weekly review)
- The `search_embeddings_fulltext` RPC function update must happen in the same migration — if the RPC still uses `plainto_tsquery('english', ...)` after the column switches to `'simple'`, search results will be inconsistent

**Verify RPC function update.**
The plan mentions updating `search_embeddings_fulltext` to use `plainto_tsquery('simple', ...)`. Check if there are other RPC functions or direct SQL queries in `supabase_client.py` that reference tsvector with `'english'` config. Grep for `'english'` and `ts_query` and `tsquery` across the codebase.

### D3 — Indexes

**No concerns.** `IF NOT EXISTS` is safe. The two composite indexes directly support the queries introduced in A4 and existing MCP task filtering. Run alongside D1 in a single migration.

---

## Cross-Cutting Concerns

### Tool count and discoverability (18 → 26 tools)

26 tools is a lot for Claude.ai to manage. Without clear guidance, it will confuse similar tools or forget specialized ones exist. The MCP system instructions and project prompt need tool grouping:

```
TOOL REFERENCE (26 tools):

Memory & Search:
  search_memory, get_meeting_history, get_open_questions, get_stakeholder_info

Tasks:
  get_tasks, create_task, update_task

Gantt:
  get_gantt_status, get_gantt_horizon, propose_gantt_update, approve_gantt_proposal

Weekly Review:
  get_weekly_summary, start_weekly_review, confirm_weekly_review

Quick Operations:
  quick_inject, confirm_quick_inject

System:
  get_system_context, get_full_status, get_system_health, get_cost_summary,
  get_pending_approvals, get_upcoming_meetings

Session:
  get_last_session_summary, save_session_summary, get_decisions, get_commitments (deprecated → use get_tasks)
```

Add a "when to use what" section in the project prompt:
- "Eyal says 'update that task' → update_task()"
- "Eyal says 'log this decision' → quick_inject()"
- "Eyal says 'change the Gantt' → propose_gantt_update()"
- "Eyal says 'what's the status' → get_full_status() for overview, get_tasks()/get_gantt_status() for specifics"

### Migration coordination (Phase 8b)

Three SQL changes in Phase 8b: tsvector recreation, new indexes, heartbeats table. Run them as a single migration script in order:
1. Heartbeats table (CREATE TABLE — no dependencies)
2. Indexes on tasks (CREATE INDEX — no dependencies)
3. tsvector drop + recreate + RPC update (schema change — do last, briefly locks tables)

Run during a quiet period. Verify with a simple search query after migration that full-text search still returns results.

### Project prompt update (do as a final step)

After all Phase 8 tools are deployed, the Claude.ai project prompt needs a comprehensive update:
- 8 new tool descriptions with parameter schemas
- Updated tool count (26)
- Quick inject workflow walkthrough (extract → review → edit → confirm)
- Gantt propose workflow walkthrough (propose → review conflict → approve)
- Task hygiene guidance (surface unassigned/no-deadline tasks during review, prompt Eyal to resolve)
- Cost summary presentation guidance
- Health check guidance ("If Eyal asks 'is everything working' → get_system_health()")

Don't treat this as an afterthought — the prompt quality directly determines whether the 8 new tools get used correctly or cause confusion.

### Google Sheets sync for MCP write operations

This applies to B1 (task CRUD), B2 (quick inject), and B3 (Gantt propose). Every write that changes data in Supabase must also update the corresponding Google Sheet if one exists:
- Task created/updated → Task Tracker sheet
- Decision injected → Decisions sheet (from Phase 7 hardening Batch 6)
- Gantt proposal executed → Gantt sheet (already handled by gantt_manager)

Trace each write path and verify Sheets sync happens. The approval flow handles this for transcript processing, but MCP write tools might bypass that flow and go directly to Supabase. If so, add explicit Sheets sync calls.

---

## Summary of Required Actions

**Before implementation:**
1. Decide on task selection strategy for A1's 30-task cap (participant-first recommended)
2. Write team role descriptions in config/team.py (2-3 sentences per person, operationally focused)
3. Verify existing task update flow — does supabase_client.update_task() also sync to Sheets?
4. Define changes schema for B3 Gantt propose tool

**During 8a implementation:**
5. A1: Handle existing_tasks=None gracefully in prompt builder
6. A1: Document whether "UPDATE:" prefix is machine-parsed or LLM-hint-only
7. A2: Put role descriptions in config/team.py, not hardcoded in prompt
8. A3: Consider priority-aware escalation thresholds
9. B1: Add Sheets sync if not already in update_task path
10. B1: Handle natural language date parsing for deadlines
11. B2: Add edit-before-confirm guidance to project prompt
12. B3: Include clear changes schema in MCP tool description
13. B3: Return detailed change descriptions in approve response
14. D2: Ensure person names treated as proper nouns (not translated)

**During 8b implementation:**
15. C1: Heartbeat writes must be fire-and-forget (never crash the scheduler)
16. C1: Handle non-periodic schedulers in stale detection (calendar-based, not interval-based)
17. C1: Wire stale schedulers into Phase 7 alerting system
18. C2: Account for prompt caching discount in pricing table
19. C2: Wire daily cost alert into alerting system
20. D1: Run all migrations in single script, quiet period, verify search after
21. D1: Grep for 'english' tsquery references across entire codebase

**After deployment:**
22. Update Claude.ai project prompt with all 8 new tools, workflows, and groupings
23. Test quick_inject flow end-to-end in Claude.ai (extract → edit → confirm)
24. Test propose_gantt_update with a conflict scenario
25. Verify Sheets sync for task updates made via MCP
26. Run a cost summary and verify numbers look reasonable against actual Anthropic billing
