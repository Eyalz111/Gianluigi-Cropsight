# Intelligence Signal — Architecture Review

**Reviewer:** Claude (Architecture Layer, CropSight Ops Project)  
**Date:** April 2, 2026  
**Document reviewed:** CropSight Intelligence Signal — Implementation Plan  
**For:** Claude Code — review before implementation begins  
**Status:** 5 required fixes + 4 enhancements. Do not start coding until fixes are resolved.

---

## Overall Assessment

The plan is solid. The Claude Code brainstorming session correctly identified and fixed
several codebase-specific patterns from the original spec (SYNC call discipline, correct
telegram/gmail service names, class-based scheduler pattern, Drive methods returning dict
not tuple). The step ordering and verification checkpoints are right.

However there are 5 issues that need to be resolved before implementation starts —
two affect the schema, two affect the agent pipeline, and one affects a design principle.
There are also 4 enhancements worth building in from the start rather than retrofitting.

---

## Required Fixes (resolve before starting)

---

### Fix 1 — Schema: Add `flags JSONB` column to `intelligence_signals`

**Severity:** HIGH — functional gap

**Problem:** `signal_content` is stored as `TEXT` (the full written report). But the
Telegram notification and email formatting both call:

```
format_telegram_notification(signal_id, drive_link, week_number, flags)
format_email_html(signal_content, drive_link, week_number, year)
```

Where do `flags` come from at distribution time? If the report is plain text, there
is no structured way to extract the flags list. The agent would have to re-parse the
text, which is fragile.

**Fix:** Add a `flags JSONB` column to `intelligence_signals` in the migration:

```sql
-- Add to intelligence_signals table
flags JSONB,   -- [{flag: str, urgency: high|medium}] — max 3
```

The agent stores flags immediately after synthesis (before Drive upload), then reads
them at distribution time for Telegram and email formatting.

---

### Fix 2 — Agent: Write `signal_content` to DB before Drive upload

**Severity:** HIGH — content loss risk

**Problem:** The current pipeline order is:

```
Opus synthesizes → Drive upload → DB update with content + links
```

If Drive upload fails after Opus runs (~$0.15 spent), the synthesized content is
lost. The signal record stays in `status='generating'` with no content, and the
retry will re-run Opus from scratch.

**Fix:** Change the order inside `generate_intelligence_signal()`:

```
Opus synthesizes
  → store signal_content + flags in DB immediately (status stays 'generating')
  → Drive upload
  → update DB with drive_doc_id + drive_doc_url
  → update status to 'pending_approval'
```

This way if Drive upload fails, the content is safe and the agent can retry the
upload without re-running Opus. The retry chain only re-runs Perplexity and Opus
if `signal_content` is null.

---

### Fix 3 — Competitor auto-curation: surface changes, don't silently write

**Severity:** MEDIUM — design principle violation

**Problem:** The plan specifies zero Eyal intervention on competitor watchlist updates:

- 3+ appearances → auto-promote to `watching`  
- 4 weeks silent → auto-deactivate

This violates Gianluigi's core contract: **Gianluigi proposes, Eyal approves.**
Concretely: a real competitor could get auto-deactivated during a quiet 4-week period
(they were busy, not gone). A Perplexity hallucination could get auto-promoted to
`watching` without Eyal seeing it.

**Fix:** Keep the auto-curation logic exactly as designed — it runs correctly. Add
one output: log all curation events to a `watchlist_changes` list, and include a
brief summary line in the Telegram notification when the signal is generated:

```
📡 Intelligence Signal W14/2026 ready for review.
Top flag: [...]

Watchlist: 1 competitor auto-promoted (Aydi), 1 auto-deactivated (Gro Intelligence).
Approve via CropSight Ops when ready.
```

No approval required. Just visibility. Eyal can check `get_competitor_watchlist`
via MCP if he wants to review. This satisfies the principle without adding friction.

Implementation: `_update_competitor_watchlist()` returns a `dict` of changes made.
The agent includes this in the approval content stored in `pending_approvals`.
`format_telegram_notification()` accepts optional `watchlist_changes` param.

---

### Fix 4 — Add timeout guard around Opus synthesis call

**Severity:** MEDIUM — reliability

**Problem:** An 8192 max_tokens Opus call with a complex multi-section prompt can
take 60–90 seconds. `call_llm()` is SYNC, so it blocks the event loop for that
window. If the call hangs (network issue, rate limit), the scheduler task stalls
with no recovery path.

**Fix:** Wrap the Opus synthesis in a thread executor with a timeout:

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

async def _run_synthesis_with_timeout(prompt, system, timeout_seconds=120):
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = loop.run_in_executor(
            executor,
            lambda: call_llm(
                prompt=prompt,
                model=settings.model_extraction,
                max_tokens=8192,
                call_site="intelligence_signal_synthesis",
                system=system,
            )
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            raise RuntimeError("Opus synthesis timed out after 120s")
```

On timeout: set signal status to `error`, log via `log_action()`, send Telegram
alert to Eyal. The retry chain does not trigger on timeout (this is a different
failure mode from Perplexity being unavailable).

---

### Fix 5 — Remove `INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE` setting

**Severity:** MEDIUM — design risk

**Problem:** `INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE=False` is listed as a day-one
setting. Having it in `config/settings.py` from the start creates a temptation to
enable it before signal quality is proven. "Skip approval gate" is a significant
deviation from the system's design contract, and the approval flow during the first
weeks is how you build trust in the output quality.

**Fix:** Remove this setting entirely. Do not implement auto-distribute. The MCP
tool `approve_intelligence_signal()` makes approval a single tool call — it is not
a meaningful friction point. If auto-distribute is genuinely needed after 2–3 months
of proven signal quality, add it then as a deliberate Phase 2 feature with its own
review.

Remove from `config/settings.py`:
```python
# DELETE this line:
INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE: bool = Field(default=False, ...)
```

Remove any references in `intelligence_signal_agent.py` that check this setting.

---

## Enhancements (worth building in from the start)

---

### Enhancement 1 — Exploration Corner as an explicit output section

**Problem:** The context builder generates 2–3 rotating exploration queries (adjacent
markets, wild card crops, unexplored geographies). These run through Perplexity.
But the synthesis prompt section list does not explicitly name "Exploration Corner"
as a required output section — it will likely get silently dropped when Perplexity
returns sparse results on those queries.

**Enhancement:** Add "Exploration Corner" explicitly to the synthesis prompt structure,
positioned between "Science & Tech Signals" and "This Week's Angle":

```
Exploration Corner — 1–2 items from the out-of-scope rotating queries.
If nothing interesting came back this week, say so in one sentence and move on.
Do not pad. Honest empty is better than forced content.
```

This section is what prevents the brief from becoming a filter bubble. It needs
explicit real estate in the output or it will disappear under pressure to
synthesize a coherent document.

---

### Enhancement 2 — Retry chain transparency in Telegram notification

**Problem:** The 2h retry window means: Perplexity fails at 18:00 → retry at
20:00 → Claude search fallback at ~20:30. On a bad day the team gets the signal
Friday morning. The Telegram notification currently doesn't distinguish between
"generated cleanly" and "generated via fallback."

**Enhancement:** `format_telegram_notification()` accepts a `research_source` param
(already in the schema as a column: `perplexity | perplexity_retry | claude_search`).
When source is not `perplexity`, add a single line to the notification:

```
⚠️ Generated via backup research (Perplexity unavailable). Quality may vary.
```

This is transparency, not an apology — Eyal should know what he's approving.

---

### Enhancement 3 — Plan for prompt calibration before team distribution

**Not a code change.** A process note for the verification plan.

The news anchor character prompt is strong, but the first 2–3 times Opus runs
with it, the output may over-index on one tone (too journalistic, too dry,
too breathless). The verification plan says "Check the Google Doc looks good"
after Step 6 — this is necessary but not sufficient.

**Enhancement:** Add an explicit calibration step to the verification plan:

```
After Step 6 — Manual Calibration (before enabling for team)

1. Trigger 3 manual signal generations on different weeks
2. Read each Google Doc output completely
3. For each: note sections that feel off-tone, padded, or generic
4. Iterate the character prompt in intelligence_signal_prompts.py
   until output consistently reads like an engaged journalist
5. Only after 2+ consecutive outputs pass this bar:
   set INTELLIGENCE_SIGNAL_RECIPIENTS to full team list
```

Keep `INTELLIGENCE_SIGNAL_RECIPIENTS=""` (Eyal only) during calibration.
The current verification plan skips from "Step 6 checkpoint" to "Step 8 full
integration" without a calibration gate — that gap should be closed.

---

### Enhancement 4 — `research_results JSONB` size management

**Not a blocker, but worth noting.**

12+ Perplexity queries × 2–4KB per result = 30–50KB per `research_results` JSONB
record. Not a problem at CropSight's scale (one record per week), but the raw
Perplexity responses can be verbose.

**Enhancement:** In `_update_intelligence_signal()`, truncate each result's
`content` field before storing:

```python
# Truncate Perplexity content before DB storage
for section, result in research_results.items():
    if result.get("content") and len(result["content"]) > 3000:
        result["content"] = result["content"][:3000] + "... [truncated]"
```

Keep full content only in `signal_content` (the synthesized report). The raw
`research_results` are stored for debugging, not for re-use — truncation at 3KB
per result is fine.

---

## Summary Table

| # | Type | Item | Severity | Action |
|---|------|------|----------|--------|
| 1 | Fix | Add `flags JSONB` column to schema | HIGH | Update migration SQL before running |
| 2 | Fix | Write `signal_content` to DB before Drive upload | HIGH | Reorder agent pipeline in Step 6 |
| 3 | Fix | Surface auto-curation changes in Telegram notification | MEDIUM | Add `watchlist_changes` to notification |
| 4 | Fix | Add timeout guard around Opus synthesis | MEDIUM | Wrap `call_llm` in thread executor |
| 5 | Fix | Remove `INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE` setting | MEDIUM | Delete from settings + agent |
| 6 | Enhancement | Exploration Corner as explicit output section | — | Add to synthesis prompt |
| 7 | Enhancement | Research source transparency in Telegram | — | Use existing `research_source` column |
| 8 | Enhancement | Prompt calibration gate before team distribution | — | Add to verification plan |
| 9 | Enhancement | Truncate `research_results` JSONB before storage | — | Add in supabase client methods |

---

## What Does Not Need to Change

Everything else in the plan is correct and ready to implement:

- DB schema (with the `flags` column addition above)
- Seed script — 7 competitors, upsert on name, correct
- Settings structure and all other env vars
- `PerplexityResult` dataclass and client pattern
- Context builder defaults and keyword extraction approach
- Exploration query rotation via `week_number % len(pool)` — clever
- Class-based scheduler matching `MorningBriefScheduler` — correct pattern
- `ZoneInfo("Asia/Jerusalem")` for DST-aware scheduling — correct
- MCP tool set (38→43) — all 5 tools are right, `trigger_intelligence_signal` is a
  valuable addition
- Drive methods returning `dict` — correct
- `call_llm()` SYNC throughout — correct
- `gmail_service.send_email(html_body=...)` — correct
- `telegram_bot.send_to_eyal()` — correct
- Step ordering: Foundation → Components (parallel) → Agent → Integration layer
- Verification plan structure
- `INTELLIGENCE_SIGNAL_ENABLED=False` default — correct and safe
- Video pipeline built disabled — correct
- PIL + Inter font with graceful fallback — correct
- `ffmpeg` in Dockerfile production stage — correct
- `Pillow>=10.4.0` in requirements.txt — correct
- Test count target ~90 new tests → ~1540 total

---

## Estimated Impact of Fixes on Scope

All 5 fixes are small. None require new files or new tables (Fix 1 adds one column
to an existing table in the migration). Total additional implementation time: ~2–3
hours across the full build. Do not let the fixes delay starting Step 1 — the
migration fix (Fix 1) is the only one that must be done before writing any code.

---

*Architecture review complete. Proceed to implementation after resolving Fix 1.*  
*Reviewer: Claude (Architecture Layer, CropSight Ops Project) — April 2, 2026*
