# Phase 7 QA Hardening — Revised Plan (13 Issues + 3 New)

**Date:** March 22, 2026
**Status:** Ready for implementation
**Prerequisite:** All 1348+ existing tests must pass before starting
**Goal:** Clean baseline before Phase 7.5 (weekly review migration)

---

## Context

Phase 7 live QA surfaced 6 issues. Deep investigation found 7 more. Architecture review added 3 more. This document is the revised hardening plan incorporating decisions made during review:

**Key design decisions made:**
1. **Commitments are being eliminated as a separate concept.** Everything becomes "action items" (tasks). The commitments Sheets tab, extraction category, and code paths are deprecated.
2. **Meeting summary distribution is being redesigned.** Team notifications become short teasers with a Drive link, not walls of text.
3. **System failure alerts are being added.** Critical errors → immediate Telegram DM to Eyal; warnings → batched daily.

---

## Batch 0: Database & Schema Alignment Audit (Do First)

**WHY:** Before changing extraction logic, Sheets writes, and MCP tools, verify that the current code aligns with the actual Supabase schema and Google Sheets structure. Misalignment here causes silent data loss or runtime crashes.

### Step 0a — Supabase Schema Audit

Compare the actual Supabase tables against what the code expects. For each table used in the codebase, verify:

1. **`tasks` table** — Check all columns the code reads/writes: `title`, `assignee`, `deadline`, `status`, `priority`, `meeting_id`, `transcript_timestamp`, `category`, `created_at`, `updated_at`. Confirm column names and types match what `supabase_client.py` uses.
2. **`decisions` table** — Check columns: `description`, `context`, `participants_involved`, `meeting_id`, `transcript_timestamp`. This table is about to get a Sheets export (Batch 6), so it must be correct.
3. **`commitments` table (if it exists)** — Document its current schema. We're deprecating commitments as a separate concept (Batch 1), but need to know what's there for migration.
4. **`meetings` table** — Verify `approval_status`, `sensitivity`, `summary` columns exist and match code expectations.
5. **`embeddings` table** — Verify `source_type` enum values include all current types.
6. **Any other tables** the code references — `open_questions`, `follow_up_meetings`, `documents`, `audit_log`, `entity_registry`, etc.

**Action:** Run a query against Supabase to get actual table schemas:
```sql
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;
```

Compare output against what the code expects. Log any mismatches.

### Step 0b — Google Sheets Tab Audit

Before fixing Batch 4 (stakeholder tab) or deprecating the commitments tab (Batch 1), verify actual tab names in the spreadsheet.

1. **Task Tracker spreadsheet** — What tabs exist? What are their exact names? What are the column headers in each?
2. **Stakeholder Tracker spreadsheet** — Is the tab named "Stakeholder Tracker", "Stakeholders", or something else?
3. **Commitments tab** — Does it exist? What spreadsheet is it in? What are the headers?

**Action:** Use Google Sheets API to list all sheets and their properties for both spreadsheet IDs in the config. Log the exact tab names.

### Step 0c — Cross-Reference Config Constants

Check `config/settings.py` and `config/team.py` for:
- `TASK_TRACKER_SHEET_ID` and `STAKEHOLDER_TRACKER_SHEET_ID` — are these correct?
- Any hardcoded tab names or ranges in `services/google_sheets.py`
- Any hardcoded Supabase table names in `services/supabase_client.py`

**Output:** A brief alignment report listing any mismatches found. Fix critical mismatches before proceeding to Batch 1.

**Tests:** No new tests needed — this is an audit step. But if mismatches are found, fixes should include tests.

---

## Batch 1: Simplify Extraction — Deprecate Commitments, Fix Prompts (Issues #1, #2, #5)

**File:** `processors/transcript_processor.py` (lines ~378-464, the extraction prompt)

This batch combines three original issues into one coherent change: simplify the extraction model by eliminating the task/commitment distinction and improving task quality.

### Change 1a — Remove commitments from extraction schema

In the extraction prompt, remove the `commitments` output field entirely. Everything that would have been a commitment becomes a task (action item).

Replace the current task + commitment extraction instructions with:

```
ACTION ITEM EXTRACTION RULES:
- Extract ACTION ITEMS: anything a participant agreed to do, was asked to do, or volunteered to do.
- Include both formally assigned tasks ("Eyal, can you draft the abstract?") and verbal promises ("I'll send that over").
- Each action item needs: title, assignee, deadline (if stated), priority, transcript reference.
- CONSOLIDATION: Combine related sub-tasks into one higher-level action item. If multiple items serve the same deliverable, merge them. Aim for 3-7 action items per meeting, not 10-15.
  Example: "set up AWS account", "configure IAM roles", "prepare AWS budget estimate" → consolidate into: "Prepare AWS infrastructure (account, IAM, budget)"
- DEADLINE: Only set a deadline if the transcript explicitly mentions a specific date, day of the week, or relative timeframe (e.g., "by Friday", "next week", "March 30"). Vague urgency terms like "ASAP", "soon", or "as early as possible" are NOT deadlines — set to null. If no deadline is mentioned, set to null. Do NOT infer deadlines from context or urgency.
- DEDUPLICATION: Never extract the same action as two separate items. If someone says "I'll do X" and is later formally assigned X, extract only once.
```

### Change 1b — Remove commitments from the extraction output JSON schema

In the expected output format (the JSON schema the prompt asks Claude to produce), remove the `commitments` array. Tasks/action items are the only action-oriented output.

If the current schema has:
```json
{
  "decisions": [...],
  "tasks": [...],
  "commitments": [...],
  ...
}
```

Change to:
```json
{
  "decisions": [...],
  "tasks": [...],
  ...
}
```

### Change 1c — Deprecate commitments code paths

**File: `guardrails/approval_flow.py`** (~line 1444)

Find the block that writes commitments to Google Sheets after approval. Comment it out or remove it:
```python
# DEPRECATED: Commitments are now extracted as tasks (action items).
# The commitments Sheets tab is no longer written to.
# Previously: await sheets_service.add_commitments_batch_to_sheet(...)
```

**File: `services/google_sheets.py`**

Mark `ensure_commitments_tab()` and `add_commitments_batch_to_sheet()` as deprecated. Don't delete yet (other code may reference them), but add:
```python
# DEPRECATED — commitments are now extracted as tasks.
# Kept for backward compatibility. Will be removed in next cleanup pass.
```

**File: `services/supabase_client.py`**

If there are methods like `add_commitments()`, `get_commitments()` — mark as deprecated but don't remove. The MCP server may reference them (handle in Batch 7).

### Change 1d — Update MCP tools that reference commitments

**File: `services/mcp_server.py`**

If there's a `get_commitments()` MCP tool:
- Keep it working (backward compatibility with Claude.ai sessions) but have it return a note: `"Note: Commitments have been merged into tasks. Use get_tasks() for all action items."`
- Update `get_full_status()` (Batch 7) to skip the commitments call or redirect to tasks.

### Tests:
- Prompt content assertion: verify the new ACTION ITEM EXTRACTION RULES string exists in the prompt
- Verify commitments array is NOT in the expected output schema
- Verify approval flow does NOT call `add_commitments_batch_to_sheet`
- Verify extraction of a sample transcript produces tasks only (no commitments field)
- Regression: existing task extraction tests must still pass

---

## Batch 2: Silent Logging Fixes (Issues #9, #12)

Trivial, zero risk. Replace bare `except: pass` with logged warnings.

### Issue #9 — `services/mcp_server.py` ~line 168

```python
# FROM:
except (ValueError, TypeError):
    pass
# TO:
except (ValueError, TypeError) as e:
    logger.warning(f"Failed to parse expires_at: {e}")
```

### Issue #12 — `guardrails/approval_flow.py` ~line 2347

```python
# FROM:
except Exception:
    pass
# TO:
except Exception as e:
    logger.warning(f"Failed to append report URL: {e}")
```

### Tests: Verify logging calls with mocks (minimal).

---

## Batch 3: Summary Distribution Redesign (Replaces Original Issue #4)

**This replaces the truncation fix from the original plan.** Instead of arguing about 500 vs 1500 char limits, we're redesigning how summaries reach the team.

### Design

**Telegram group message (post-approval distribution)** becomes a short teaser:

```
📋 Meeting Summary: [Title] ([Date])
Participants: [Names]

[N] decisions · [M] action items · [K] follow-up meetings

Key decisions:
• [Decision 1 — max ~80 chars]
• [Decision 2]
• [Decision 3]

Top action items:
• [Assignee] [Task title] — [deadline or "no deadline"]
• [Assignee] [Task title] — [deadline or "no deadline"]
• [Assignee] [Task title] — [deadline or "no deadline"]

📄 Full summary: [Google Drive link]
```

Rules:
- Max 3 decisions shown (if more, add "... and N more in full summary")
- Max 3-5 action items shown, prioritized by priority field (H first)
- NO discussion summary, NO open questions, NO stakeholder mentions in the teaser
- Always end with the Drive link
- Total message should be under ~800 chars naturally (no truncation logic needed)

**Email (post-approval distribution):**
- Same teaser structure as the email body
- Full summary document attached as .docx (existing behavior)
- Google Drive link included
- Subject: "Meeting Summary: [Title] — [Date]"

**Telegram DM to Eyal (approval preview):**
- Keep the current full preview — Eyal needs to see everything to approve
- But also remove the caller-side truncation that was in the original Batch 3:

```python
# In approval_flow.py line 1485:
# FROM:
summary=summary[:500] + "..." if len(summary) > 500 else summary,
# TO:
summary=summary,
```

The Telegram bot's `_split_message()` handles the 4096 API limit.

### Implementation

**File 1: `services/telegram_bot.py`**

Add a new method `format_summary_teaser(content, drive_link)` that takes the extracted content dict and produces the teaser format above. This is used for team distribution only.

Keep the existing full-format method for the approval preview.

**File 2: `guardrails/approval_flow.py`**

In the distribution step (after approval), change the Telegram group message to use the teaser format instead of the full summary.

Find the code that sends to the Telegram group after approval and replace:
```python
# OLD: sends full summary or truncated summary to group
# NEW: sends teaser via format_summary_teaser()
```

The email distribution should also use the teaser as the email body, with the .docx as attachment and Drive link.

**File 3: `processors/transcript_processor.py`** (or wherever the Drive link is generated)

Ensure the Google Drive link for the summary document is available at distribution time and passed to the teaser formatter.

### Tests:
- Test teaser formatting with various content sizes (0 decisions, 10 decisions, 0 tasks, 15 tasks)
- Verify teaser is under 1000 chars for typical meetings
- Verify Drive link is always present in the teaser
- Verify Eyal's approval preview still shows full content
- Verify email body uses teaser format with attachment

---

## Batch 4: Stakeholder Sheets Tab Fix (Issue #3)

**Prerequisite:** Batch 0b must confirm the exact tab name.

**File: `services/google_sheets.py`**

### Change 1 — Add tab name to append (~line 787)

```python
# FROM:
range="A:P",
# TO:
range=f"'{STAKEHOLDER_TAB_NAME}'!A:P",
```

Where `STAKEHOLDER_TAB_NAME` is a constant. Prefer defining it in `config/settings.py`:
```python
STAKEHOLDER_TAB_NAME = "Stakeholder Tracker"  # Verify against actual spreadsheet in Batch 0b
```

### Change 2 — Same fix for `get_all_stakeholders()` (~line 556)

```python
# FROM:
range_name="A:P"
# TO:
range_name=f"'{STAKEHOLDER_TAB_NAME}'!A:P"
```

### Change 3 — Add debug logging for dedup (~line 757)

```python
if not name or name.lower() in existing_names:
    if name:
        logger.debug(f"Stakeholder '{name}' already exists, skipping")
    continue
```

### Tests:
- Mock Sheets API, assert range string includes tab name prefix
- Test with tab name containing spaces (needs single quotes in range)

---

## Batch 5: Timezone Fixes (Issues #10, #11 + all schedulers)

Replace `datetime.now()` → `datetime.now(ZoneInfo("Asia/Jerusalem"))` in all scheduler files.

### Files and approximate lines:

1. `schedulers/weekly_digest_scheduler.py` — lines 78, 181
2. `schedulers/weekly_review_scheduler.py` — lines 167, 281
3. `schedulers/alert_scheduler.py` — line 59
4. `schedulers/orphan_cleanup_scheduler.py` — lines 212, 251
5. `schedulers/task_reminder_scheduler.py` — lines 86, 126, 405
6. `schedulers/transcript_watcher.py` — line 416

### Pattern for each file:

```python
from zoneinfo import ZoneInfo

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# Then replace every occurrence:
# datetime.now() → datetime.now(_ISRAEL_TZ)
```

`_ISRAEL_TZ` at module level is safe — it's a timezone object, not a time value.

### Additional check:
**Grep for `datetime.utcnow()`** across the entire codebase. If found in any scheduler or time-sensitive code, replace with `datetime.now(ZoneInfo("UTC"))` or `datetime.now(_ISRAEL_TZ)` depending on context. `utcnow()` is equally problematic as `now()` for timezone-aware scheduling.

### Tests:
- Mock `datetime.now`, verify Israel timezone is passed in each scheduler
- Test that scheduling decisions (e.g., "is it business hours?") use the correct timezone

---

## Batch 6: Decisions Export to Google Sheets (Issue #7)

### File 1: `services/google_sheets.py`

Add two methods following the existing patterns (model after the task sheet methods):

**`ensure_decisions_tab()`**
- Create "Decisions" tab if missing
- Headers: `["Decision", "Context", "Participants", "Source Meeting", "Meeting Date", "Status", "Timestamp"]`
- Note: "Status" column added (values: Active / Superseded / Revisited) — enables tracking decision evolution across meetings

**`add_decisions_batch_to_sheet(decisions, source_meeting, meeting_date)`**
- Append rows to "Decisions" tab
- Map fields from extraction output to sheet columns
- Default Status = "Active"
- Skip empty decisions lists (no-op)

### File 2: `guardrails/approval_flow.py`

Add step 2c between current task writing and follow-up handling (the commitments step is now removed per Batch 1, so slot into that gap):

```python
# 2c. Add decisions to Decisions tab
try:
    decisions = content.get("decisions", [])
    if decisions:
        await sheets_service.ensure_decisions_tab()
        await sheets_service.add_decisions_batch_to_sheet(
            decisions=decisions,
            source_meeting=meeting_title,
            meeting_date=meeting_date,
        )
        results["decisions_added"] = len(decisions)
except Exception as e:
    logger.error(f"Error adding decisions to Sheets: {e}")
```

### Tests:
- Tab creation (mock Sheets API)
- Field mapping correctness
- Empty list → no-op
- Status column defaults to "Active"
- Approval flow integration test

---

## Batch 7: System Failure Alerts to Telegram (NEW)

### Design

Create a tiered alerting system that notifies Eyal of system failures via Telegram.

**Severity levels:**

| Level | Routing | Examples |
|-------|---------|---------|
| CRITICAL | Immediate Telegram DM to Eyal | Transcript processing failure, approval flow error, Sheets write failure, Supabase connection failure, auth token expiry |
| WARNING | Batched daily (include in morning health message) | Scheduler missed window, Google API retries needed, zero tasks extracted from 60+ min meeting |
| INFO | Log only (no Telegram) | Normal operations, successful retries |

### Implementation

**New file: `services/alerting.py`**

```python
import logging
from enum import Enum
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


class AlertSeverity(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# In-memory buffer for warnings (flushed during morning health message)
_warning_buffer: list[dict] = []


async def send_system_alert(
    severity: AlertSeverity,
    component: str,
    message: str,
    error: Exception | None = None,
):
    """
    Route system alerts based on severity.
    CRITICAL → immediate Telegram DM to Eyal
    WARNING → buffer for daily batch
    INFO → log only
    """
    timestamp = datetime.now(_ISRAEL_TZ).strftime("%H:%M")
    
    if severity == AlertSeverity.CRITICAL:
        alert_text = (
            f"⚠️ System Alert ({timestamp})\n"
            f"Component: {component}\n"
            f"Error: {message}"
        )
        if error:
            alert_text += f"\nDetails: {type(error).__name__}: {str(error)[:200]}"
        
        # Import here to avoid circular imports
        from services.telegram_bot import send_direct_message
        try:
            await send_direct_message(EYAL_CHAT_ID, alert_text)
        except Exception as e:
            logger.critical(f"CANNOT SEND ALERT TO TELEGRAM: {e} | Original: {message}")
    
    elif severity == AlertSeverity.WARNING:
        _warning_buffer.append({
            "timestamp": timestamp,
            "component": component,
            "message": message,
        })
    
    # Always log regardless of severity
    log_msg = f"[{severity.value.upper()}] [{component}] {message}"
    if severity == AlertSeverity.CRITICAL:
        logger.error(log_msg)
    elif severity == AlertSeverity.WARNING:
        logger.warning(log_msg)
    else:
        logger.info(log_msg)


def get_and_flush_warnings() -> list[dict]:
    """Called by morning health heartbeat to include warnings in daily report."""
    warnings = _warning_buffer.copy()
    _warning_buffer.clear()
    return warnings
```

### Wiring into existing code

Add `send_system_alert` calls at key failure points:

**`processors/transcript_processor.py`** — wrap the main extraction call:
```python
except Exception as e:
    await send_system_alert(AlertSeverity.CRITICAL, "transcript_processor", f"Failed to process transcript '{title}': {e}", error=e)
    raise
```

**`guardrails/approval_flow.py`** — wrap the post-approval distribution:
```python
except Exception as e:
    await send_system_alert(AlertSeverity.CRITICAL, "approval_flow", f"Failed to distribute approved summary '{title}': {e}", error=e)
```

**`services/google_sheets.py`** — wrap Sheets write operations:
```python
except Exception as e:
    await send_system_alert(AlertSeverity.CRITICAL, "google_sheets", f"Sheets write failed for range '{range_name}': {e}", error=e)
    raise
```

**`services/supabase_client.py`** — connection failures:
```python
except Exception as e:
    await send_system_alert(AlertSeverity.CRITICAL, "supabase", f"Database operation failed: {e}", error=e)
    raise
```

**All scheduler files** — if a scheduled job fails:
```python
except Exception as e:
    await send_system_alert(AlertSeverity.WARNING, "scheduler_name", f"Scheduled job failed: {e}", error=e)
```

**Quality gate** — after extraction, if zero tasks from a long meeting:
```python
if len(tasks) == 0 and meeting_duration_minutes > 45:
    await send_system_alert(AlertSeverity.WARNING, "extraction_quality", f"Zero tasks extracted from {meeting_duration_minutes}-min meeting '{title}' — likely extraction issue")
```

### Tests:
- CRITICAL alert sends Telegram DM (mock `send_direct_message`)
- WARNING alert buffers but does NOT send Telegram
- `get_and_flush_warnings()` returns buffered warnings and clears buffer
- Alert formatting includes component and message
- Telegram send failure in alerting doesn't crash (logged as CRITICAL to stderr)

---

## Batch 8: MCP Improvements (Issues #6, #8, #13)

### Issue #13 — Add `warnings` param to `_success()` helper

**File: `services/mcp_server.py`**

```python
def _success(data, source="supabase", record_count=None, warnings=None):
    meta = {
        "source": source,
        "timestamp": datetime.now(_ISRAEL_TZ).isoformat(),
    }
    if record_count is not None:
        meta["record_count"] = record_count
    if warnings:
        meta["warnings"] = warnings
    return {"status": "success", "data": data, "metadata": meta}
```

### Issue #6 — Add `get_full_status()` composite tool

New 16th tool in `services/mcp_server.py`. Calls in one request:
- `supabase_client.get_tasks(status="pending")` (sync)
- `gantt_manager.get_gantt_status()` (async)
- `supabase_client.get_commitments(status="open")` (sync) — **redirect to `get_tasks(status="pending")`** since commitments are deprecated
- `supabase_client.get_pending_approval_summary()` (sync)
- `calendar_service.get_upcoming_events(days=7)` (async)
- `generate_alerts()` (sync)

**Each sub-call wrapped in try/except with per-call timeout:**
```python
import asyncio

async def get_full_status():
    warnings = []
    result = {}
    
    # Sync calls
    try:
        result["tasks"] = supabase_client.get_tasks(status="pending")
    except Exception as e:
        warnings.append(f"Tasks unavailable: {e}")
        result["tasks"] = []
    
    # Async calls with timeout
    try:
        result["gantt"] = await asyncio.wait_for(
            gantt_manager.get_gantt_status(), timeout=10
        )
    except asyncio.TimeoutError:
        warnings.append("Gantt status timed out (10s)")
        result["gantt"] = None
    except Exception as e:
        warnings.append(f"Gantt unavailable: {e}")
        result["gantt"] = None
    
    # ... same pattern for calendar, alerts, approvals
    
    return _success(result, warnings=warnings if warnings else None)
```

**Update MCP system instructions** to mention `get_full_status()`. Update tool count (15 → 16).

### Issue #8 — Add report URL to `get_weekly_summary()`

After `compile_weekly_review_data()`, check for existing report:
```python
report = supabase_client.get_weekly_report(week_number, year)
if report and report.get("access_token"):
    data["report_url"] = f"{base_url}/reports/weekly/{report['access_token']}"
```

### Tests:
- Composite tool with all services mocked — returns combined data
- Partial failure — returns available data + warnings for failed sub-calls
- Timeout on one service doesn't block others
- Report URL present when report exists, absent when it doesn't
- Tool count = 16 in server registration

---

## Execution Order

| Order | Batch | Description | Risk | Est. Effort |
|-------|-------|-------------|------|-------------|
| 0 | DB & Sheets Audit | Verify alignment before changes | None | 30 min |
| 1 | Extraction Simplification | Deprecate commitments, fix prompts | Low-Med | 2-3 hrs |
| 2 | Silent Logging | Add warning logs to bare excepts | None | 15 min |
| 3 | Summary Teaser | Redesign distribution format | Low | 1-2 hrs |
| 4 | Stakeholder Tab | Fix Sheets range targeting | Low | 30 min |
| 5 | Timezone Fixes | Israel TZ in all schedulers | Medium | 1 hr |
| 6 | Decisions Export | New Decisions tab in Sheets | Low | 1-2 hrs |
| 7 | System Alerts | Telegram alerts for failures | Low-Med | 1-2 hrs |
| 8 | MCP Improvements | Composite tool, warnings, report URL | Medium | 2-3 hrs |

**Total estimated: ~10-14 hours of focused work**

---

## Verification Checklist (After All Batches)

1. **Run full test suite:** `python -m pytest tests/ -x -q` — all existing tests must pass
2. **New tests:** ~35-45 new tests across all batches
3. **Manual checks:**
   - [ ] Read the raw extraction prompt — verify no mention of "commitments" in extraction output
   - [ ] Verify MCP tool count = 16 in server registration
   - [ ] Verify `get_full_status()` appears in MCP system instructions
   - [ ] Grep codebase for `datetime.now()` without timezone — should be zero in schedulers
   - [ ] Grep codebase for `datetime.utcnow()` — should be zero or justified
   - [ ] Grep codebase for bare `except.*pass` — should be zero (all should log)
   - [ ] Verify Stakeholder tab name matches actual spreadsheet
4. **DB alignment:** Batch 0 audit report reviewed and all mismatches resolved
5. **Commitments deprecation:** No active code path writes to commitments Sheets tab

---

## Notes for Claude Code

- **Supabase methods are SYNC** — never `await` them. Only Google API calls and Telegram sends are async.
- **Don't delete deprecated code yet** — mark with comments and `# DEPRECATED`. Cleanup is a separate pass.
- **The extraction prompt is the most sensitive change.** Read the full prompt string before and after modification. A small formatting error can break the JSON schema that downstream code parses.
- **Test each batch independently.** Commit after each batch passes. Don't batch commits across multiple changes.
- **When adding alerting (Batch 7)**, be careful with circular imports — `alerting.py` needs to import from `telegram_bot.py`, and some files that need alerting may be imported by `telegram_bot.py`. Use lazy imports (`from x import y` inside the function) if needed.
- **For timezone fixes (Batch 5)**, verify the import works: `from zoneinfo import ZoneInfo` is stdlib in Python 3.9+. No pip install needed.
