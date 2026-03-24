# Phase 9 Review Notes — Architecture & Implementation Concerns

**Date:** March 24, 2026
**Context:** Review of the Phase 9 "Operational Excellence" plan. Read alongside the Phase 9 spec. Address each point during implementation or explain why it's not needed.

---

## 1. Reconcile Phase Numbering with V1_DESIGN.md

The original Phase 9 in V1_DESIGN.md was "MCP Phase 2" (Days 31-33): write tools, weekly review via Claude.ai, session continuity, Gantt slides. That work has already been absorbed into Phases 7.5 and 8 — Gianluigi now has 26 MCP tools including writes, the weekly review works via Claude.ai, and session continuity exists.

**Action:** Add a note at the top of the Phase 9 spec:
> "Original Phase 9 (MCP Phase 2 per V1_DESIGN.md) was completed within Phases 7.5-8. This Phase 9 replaces it with Operational Excellence — memory, cross-meeting intelligence, Gantt analytics, and distribution polish."

This prevents confusion when cross-referencing V1_DESIGN.md during implementation.

---

## 2. Reorder 9B Internally: B1 → B3 → B2

9B is labeled "THE BIG ONE" at 5-7 days. It contains three independent features with different risk profiles:

- **B1 (meeting-to-meeting continuity):** Low risk, high value. Context injection before extraction using existing data. Safe to build first.
- **B3 (compressed operational snapshot):** Standalone feature, no dependency on B2. Immediate UX win — replaces 7+ MCP calls for session onboarding. Independent of topic threading.
- **B2 (topic threading):** Most ambitious, most risky. Fuzzy matching, Haiku disambiguation, auto-thread creation, evolution narratives. Most places where output quality needs iteration.

**Recommended build order:** B1 → B3 → B2

**Rationale:** B3 gives the compressed onboarding context immediately. B2 (topic threading) needs the most tuning and iteration, so it should come last within 9B — if it takes longer than expected, B3 and 9C aren't blocked.

---

## 3. Topic Threading Needs Human Correction Tools

The plan has topic creation happening automatically after extraction. Fuzzy matching will inevitably create duplicates or miscategorize threads. Without correction tools, Eyal will need to go into Supabase directly to fix data quality issues.

**Action:** Add 2 MCP tools (not in current plan):

```
merge_topic_threads(source_id: str, target_id: str) → confirmation
  # Merges two threads into one, re-links all mentions from source to target, deletes source

rename_topic_thread(topic_id: str, new_name: str) → confirmation
  # Updates topic name + topic_name_lower, useful when auto-generated name is wrong
```

These are small tools but high-leverage for keeping topic threading usable over time. Add to the 9B tool count.

---

## 4. Tool Grouping — Do It in 9D, Not Phase 10

The risk table says "Grouped reference in project prompt (Phase 10)" for the 36-tool discoverability problem. But Gianluigi will have 36 tools registered well before Phase 10. Claude.ai already shows tool selection issues at 26 tools.

**Action (9D):** Prefix all MCP tool descriptions with their category:

```
"[MEMORY] Search Gianluigi's memory using hybrid RAG..."
"[TASKS] Query tasks with optional filters..."
"[GANTT] Get current Gantt chart status..."
"[DECISIONS] Update decision status and review date..."
"[TOPICS] Get topic thread evolution across meetings..."
"[REVIEW] Start or resume the weekly CEO review session..."
"[SYSTEM] Get system health and scheduler status..."
```

This gives Claude.ai natural clustering cues without a full prompt rewrite.

**Also consider collapsing tools where possible:**
- `get_now_next_later()` → could be `get_gantt_status(view="now_next_later")` instead of a separate tool
- `get_meeting_effectiveness()` → could be a section in `get_weekly_summary(include_effectiveness=true)`
- `generate_operational_snapshot()` → could be a parameter on `get_system_context(refresh=true)`

Each collapsed tool saves Claude.ai a routing decision. Target: 36 → ~32 tools.

---

## 5. Define the "Label" Field Explicitly in 9A

Several downstream features depend on a `label` field (decisions get labels, tasks get `source_decision_id`, topic threading uses labels for linking). But the plan doesn't define what a label is, how it's extracted, or how consistency is maintained.

**Problem:** If one transcript labels something "Moldova delivery" and another says "Gagauzia wheat PoC," topic threading won't connect them.

**Action (9A, in the extraction prompt update):**

1. Add a canonical project name list to `config/settings.py` or `config/projects.py`:
```python
CANONICAL_PROJECT_NAMES = [
    "Moldova Pilot",
    "Pre-Seed Fundraising",
    "SatYield Accuracy Model",
    "Operational Tooling",
    "Coffee/Cocoa Expansion",
    # Add as CropSight's portfolio grows
]
```

2. Add label extraction rules to the system prompt in `core/system_prompt.py`:
```
LABEL EXTRACTION RULES:
- Every decision and task MUST have a label field
- Use canonical project names where possible: {canonical_names}
- If a new project/topic appears that doesn't match any canonical name, create a short descriptive label (2-4 words)
- Normalize variations: "Moldova PoC", "Gagauzia project", "Moldova wheat" → "Moldova Pilot"
```

3. Pass the canonical list into the extraction prompt so it's available at extraction time.

This is the foundation that topic threading builds on. If labels are inconsistent, every downstream feature suffers.

---

## 6. Use Sonnet (Not Haiku) for Supersession Detection (A3)

The plan says supersession detection "uses Haiku for classification." Detecting when a new decision contradicts or updates an old one is a judgment call with real consequences — if a decision is incorrectly marked as superseded, it disappears from the active view.

**Problem:** "Does decision A contradict decision B?" is inherently ambiguous. Haiku is optimized for clear-cut classification (routing, intent detection), not nuanced reasoning about semantic contradiction.

**Action:** Use Sonnet for `detect_supersessions()` in `processors/cross_reference.py`. The cost difference is negligible — you're processing at most a handful of decisions per meeting (~$0.01-0.03 per call). The accuracy difference matters.

Keep Haiku for topic name fuzzy matching in B2 (that's a clearer classification task). Use Sonnet for supersession detection (that's a reasoning task).

---

## 7. Defer Risk Register (C4) to Phase 10

C4 adds a new extraction field (`risks`), a new table, and 2 new MCP tools for something that CropSight likely surfaces in maybe 1 in 10 meetings.

**Concerns:**
- Each new extraction field increases the chance of hallucinated extraction — Claude finds "risks" where none were explicitly discussed because the prompt tells it to look for them
- The extraction prompt is already doing a lot: decisions (with new fields), tasks, open questions, labels
- At CropSight's stage and team size, risks are discussed informally and don't need a formal register yet

**Action:** Remove C4 from Phase 9. If risk tracking becomes valuable after using 9A-9C for a few weeks, it's a clean standalone addition in Phase 10. The saved effort (~1 day) serves as buffer for B2 topic threading tuning.

---

## 8. Deprioritize Meeting Effectiveness Scoring (C2)

`decisions_per_hour` and `tasks_per_hour` sound useful in theory. In practice:
- A 2-hour strategic discussion that produces 1 major decision is more valuable than a 30-minute standup that produces 10 trivial tasks
- The scoring will consistently rate Monthly Strategic Reviews lower than weekly syncs, which is the opposite of reality
- Without weighting by decision confidence and task priority, the metric is misleading

**Action:** Either skip C2 entirely (recommended — saves ~0.5 days), or if kept, weight the scoring by `decision.confidence` and `task.priority` so strategic decisions count more than routine ones. Don't ship an unweighted `effectiveness_score` — it'll produce counterintuitive results that erode trust in Gianluigi's analytics.

---

## 9. Correct the Daily Snapshot Cost Estimate

The risk table says "$0.50-1/day, Sonnet not Opus" for the operational snapshot. This seems high and might be accidentally assuming Opus pricing.

**Actual estimate:** The snapshot reads tasks + decisions + topics + Gantt + recent meetings (~8-10K input tokens) and generates 3-5 paragraphs (~1-2K output tokens). At Sonnet pricing: ~$0.05 per call, so ~$0.05/day or ~$1.50/month. Not a cost concern at all — just correct the number so it's accurate.

---

## 10. Build in a Time Buffer for B2

Estimated total effort of 13-18 days is realistic but tight. The risk is specifically in B2 (topic threading): fuzzy matching + LLM-based disambiguation + evolution narrative generation = many places where output quality needs iteration and tuning.

**Action:** Plan for 15-20 days total. The 2-day buffer comes from deferring C4 (risk register, ~1 day) and deprioritizing C2 (meeting effectiveness, ~0.5 day). Use the saved time for B2 quality tuning — the topic threading feature is worth getting right since it's the centerpiece of the "cross-meeting intelligence" value proposition.

---

## Summary: Revised Implementation Order

```
9A (3-4 days) — Decision Intelligence + Task Archival
  - A1: Decision schema enhancement + label extraction rules + canonical project names
  - A2: Task archival (completed → Archive tab)
  - A3: Supersession detection (use Sonnet, not Haiku)
  - A4: Decision review triggers (30-day reminders)

9B (6-8 days) — Memory & Cross-Meeting Intelligence (reordered)
  - B1: Meeting-to-meeting continuity (context injection)
  - B3: Compressed operational snapshot (daily "State of CropSight")  ← moved before B2
  - B2: Topic threading (the hard one — fuzzy matching, evolution narratives)
       + Add merge_topic_threads() and rename_topic_thread() correction tools

9C (2-3 days) — Gantt Intelligence (trimmed)
  - C1: Gantt computed metrics (velocity, slippage, milestone risk, now-next-later)
  - C3: Follow-up tracking (stale task detection)
  - C2: Meeting effectiveness — SKIP or implement with weighted scoring only
  - C4: Risk register — DEFER to Phase 10

9D (2-3 days) — Distribution Polish + Integration
  - D1: Email + Word doc polish
  - D2: End-to-end Gantt verification
  - D3: Weekly review enhancement (integrate all Phase 9 features)
  - D4: Tool grouping — prefix all 32-36 MCP tool descriptions with category tags
```

**Revised totals:**
- New MCP tools: 10-12 (including merge/rename topic tools, minus deferred risk tools)
- New tables: 4 (topic_threads, topic_thread_mentions, operational_snapshots, meeting_metrics) — risk_register deferred
- Estimated effort: 15-20 days (with buffer for B2 tuning)

---

## Risk Assessment (Updated)

| Risk | Level | Mitigation |
|------|-------|------------|
| Topic fuzzy matching creates duplicates | Medium | Exact match first, Sonnet fallback, canonical name list, merge/rename correction tools |
| Label inconsistency across meetings | Medium | Canonical project names in config, normalization rules in extraction prompt |
| 36 tools too many for Claude.ai | Medium | Category prefixes on tool descriptions (9D), collapse redundant tools to ~32 |
| B2 takes longer than estimated | Medium | Build B1 → B3 first (safe wins), B2 last with time buffer |
| Extraction prompt too large with meeting context | Low | 2000 token cap on meeting_history_context |
| DB migrations on running system | Low | All additive (new columns with defaults, new tables) |
| Supersession detection false positives | Low | Use Sonnet not Haiku for reasoning-heavy classification |

---

*Generated from Claude.ai architecture review session, March 24, 2026*
*Send to Claude Code before starting Phase 9 implementation*
