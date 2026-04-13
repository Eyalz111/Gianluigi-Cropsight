# Gianluigi v2.3 — Implementation Plan (Revised)

**Session date:** April 13, 2026
**Prepared by:** Eyal (CEO) + Claude (Architecture)
**Status:** Ready for Claude Code implementation
**Revision:** 2026-04-13 — pivoted scope after brainstorm. HyDE dropped, living topic-state summaries added, RAG cap deferred to v2.4. See "What changed from the original plan" at bottom.

---

## Context

v2.3 delivers two things in parallel:

1. **Operational learning infrastructure + deadline UX** — observation capture, deadline confidence tiers, inline deadline buttons. Concrete pain relief.
2. **Living topic-state summaries** — the data-layer foundation for the "office manager" role. Each canonical topic (Moldova Pilot, Legal, WEU Marketing, etc.) maintains a continuously-updated structured state that's queryable at any time. v2.4 layers agentic synthesis on top of this; v2.3 ships the data layer alone.

**Why this cut:** The April 1 strategic plan flagged synthesis questions ("where are we on legal?", "how's WEU marketing?") as the long-pole for Gianluigi's office-manager ambition. Current RAG returns chunks; it can't synthesize state from fragments reliably. v2.3 populates a high-signal, structured topic-state that immediately becomes the best single source the LLM can lean on. v2.4 will wrap agentic multi-pass retrieval around it.

**Current version:** v2.2 post Telegram Comms Overhaul, production revision `gianluigi-00066-tf8`
**Test baseline:** ~1,968 tests passing, 19 pre-existing failures baselined — do not regress.

### Mandatory patterns (do not deviate)
- All Supabase calls are **synchronous** — never `await` them
- All LLM calls go through `core/llm.py::call_llm()`
- MCP tools use `_success()` / `_error()` response pattern
- New tables **must** have `ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;` immediately after `CREATE TABLE`
- Scheduler classes follow the existing class-based pattern (see `schedulers/qa_scheduler.py`)
- MCP server changes are **append-only** additions to `services/mcp_server.py`
- Model tiers: Haiku for classification/routing/incremental-updates, Sonnet for conversation/drafting/narrative, Opus for accuracy-critical extraction

### Files to read before starting
1. `models/schemas.py` — all Pydantic models
2. `services/supabase_client.py` — all DB operations, understand existing patterns
3. `processors/topic_threading.py` — **critical for PR 4** — existing topic thread logic, evolution_summary generation, canonical matching
4. `guardrails/approval_flow.py` — approval flow, where to hook observation capture
5. `services/telegram_bot.py` — bot handlers, existing inline button patterns (v2.2 Session 2)
6. `schedulers/task_reminder_scheduler.py` — task reminder logic and message formatting
7. `services/mcp_server.py` — existing `get_topic_thread` / `list_topic_threads` / `merge_topic_threads` / `rename_topic_thread` tools
8. `core/system_prompt.py` — extraction prompt, specifically `get_summary_extraction_prompt()`
9. `config/settings.py` — env vars, understand `settings.*` pattern
10. `processors/intelligence_signal_agent.py` — **read-only reference** — pattern we're NOT extracting yet, but v2.4 will. Understand the multi-step shape.

---

## PR Sequence

Build and merge in this order. Each PR is independently deployable but 2/3/4/5 depend on 1 being migrated.

```
PR 1 — DB Migration                        (schema only, ~30 min)
PR 2 — Deadline confidence tiers           (~half day)
PR 3 — Observation capture layer           (~1 day)
PR 4 — Living topic-state summaries        (~1.5-2 days) ★ new star feature
PR 5 — Inline deadline buttons             (~1 day)
```

Total estimate: ~5-6 working days.

---

## PR 1 — DB Migration

**File:** `scripts/migrate_v2_3.sql`

```sql
-- ============================================================
-- v2.3 Migration: approval_observations + deadline_confidence + topic state
-- ============================================================

-- 1. approval_observations table
-- Captures every approve/edit/reject decision Eyal makes.
-- Zero LLM cost. Foundation for v2.4 thematic-query observation
-- logging and v2.5 graduated autonomy.

CREATE TABLE IF NOT EXISTS approval_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_type TEXT NOT NULL,
    -- 'meeting_summary' | 'task_extraction' | 'gantt_proposal'
    -- | 'intelligence_signal' | 'meeting_prep' | 'sheets_sync'
    -- | 'quick_inject' | 'deadline_update'
    content_id UUID,                    -- FK to relevant record (optional)
    action TEXT NOT NULL,               -- 'approved' | 'edited' | 'rejected'
    original_content JSONB,             -- what Gianluigi proposed
    final_content JSONB,                -- what Eyal accepted (null if rejected)
    edit_distance_pct FLOAT,            -- 0.0-1.0, null if not edited
    context JSONB,                      -- free bag: meeting_id, signal_id, etc.
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE approval_observations ENABLE ROW LEVEL SECURITY;

CREATE INDEX idx_approval_obs_content_type ON approval_observations(content_type);
CREATE INDEX idx_approval_obs_action ON approval_observations(action);
CREATE INDEX idx_approval_obs_created_at ON approval_observations(created_at DESC);


-- 2. Add deadline_confidence to tasks table
-- 'EXPLICIT'  = user said "by March 15" — trustworthy
-- 'INFERRED'  = LLM guessed from context — noisy
-- 'NONE'      = no deadline was mentioned

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS deadline_confidence TEXT
    DEFAULT 'NONE'
    CHECK (deadline_confidence IN ('EXPLICIT', 'INFERRED', 'NONE'));

CREATE INDEX idx_tasks_deadline_confidence
    ON tasks(deadline_confidence)
    WHERE approval_status = 'approved';


-- 3. Add state_json to topic_threads (PR 4 foundation)
-- Structured, continuously-updated snapshot of each topic's current state.
-- Queryable as a high-signal chunk; sits alongside the existing prose
-- evolution_summary. Populated incrementally by Haiku as new meetings
-- mention the topic.

ALTER TABLE topic_threads
    ADD COLUMN IF NOT EXISTS state_json JSONB DEFAULT NULL;

ALTER TABLE topic_threads
    ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ DEFAULT NULL;

-- Optional: index a few hot fields for morning-brief-style queries
CREATE INDEX IF NOT EXISTS idx_topic_threads_state_status
    ON topic_threads ((state_json->>'current_status'))
    WHERE state_json IS NOT NULL;
```

**Verification after running:**
```sql
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'tasks' AND column_name = 'deadline_confidence';

SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'topic_threads' AND column_name = 'state_json';

SELECT COUNT(*) FROM approval_observations;  -- should be 0

SELECT tablename, rowsecurity FROM pg_tables
WHERE tablename = 'approval_observations';   -- rowsecurity = true
```

**RLS test:** `tests/test_rls_coverage.py` will auto-catch if RLS is missing. Run after migration.

---

## PR 2 — Deadline Confidence Tiers

**Goal:** Tag every extracted deadline as EXPLICIT, INFERRED, or NONE at extraction time. Use the tag to suppress noisy reminders and alerts.

*(Unchanged from original plan — scope, implementation, and tests carry over in full.)*

### 2.1 Update Pydantic schema
**File:** `models/schemas.py`

```python
class DeadlineConfidence(str, Enum):
    EXPLICIT = "EXPLICIT"   # verbatim date/timeframe said by a participant
    INFERRED = "INFERRED"   # LLM estimated from context
    NONE = "NONE"           # no deadline mentioned

class Task(BaseModel):
    # ... existing fields ...
    deadline: Optional[str] = None
    deadline_confidence: DeadlineConfidence = DeadlineConfidence.NONE
```

Also update `TaskCreate` and `TaskUpdate`.

### 2.2 Update extraction prompt
**File:** `core/system_prompt.py` → `get_summary_extraction_prompt()`

In the "ACTION ITEMS / TASKS" section, add after the existing deadline instruction:

```
   - For each task, classify deadline_confidence:
     * EXPLICIT — a specific date, week number, or timeframe was stated
       verbatim by a participant. Examples: "by March 15", "before W22",
       "next Tuesday", "end of this week", "in two weeks".
     * INFERRED — no date was stated but context implies urgency or timing.
       Examples: the task follows a milestone, it was described as "urgent",
       it relates to an imminent meeting.
     * NONE — no timing signal whatsoever. This is the default.

   IMPORTANT: When in doubt, use INFERRED or NONE. Never promote a vague
   impression to EXPLICIT. Only mark EXPLICIT when you can point to a
   specific utterance.
```

Mirror the change in the YAML prompt at `config/prompts/system.yaml` — both must stay in sync.

Update the JSON output schema example to include `"deadline_confidence": "EXPLICIT"`.

### 2.3 Supabase task creation + helper

**File:** `services/supabase_client.py`

Ensure `create_task()` and `update_task()` pass `deadline_confidence` through. Add:

```python
def update_task_deadline(
    self,
    task_id: str,
    deadline: Optional[str],
    confidence: str = "EXPLICIT"
) -> dict:
    """
    Update task deadline with explicit confidence tagging.
    Used by Telegram inline buttons — always sets EXPLICIT since
    Eyal actively chose the new date.
    """
    return (
        self.client.table("tasks")
        .update({
            "deadline": deadline,
            "deadline_confidence": confidence,
            "updated_at": datetime.utcnow().isoformat()
        })
        .eq("id", task_id)
        .eq("approval_status", "approved")
        .execute()
    )
```

### 2.4 Filter reminders and alerts to EXPLICIT only

**File:** `schedulers/task_reminder_scheduler.py`

```python
# Only remind on deadlines explicitly committed to. INFERRED deadlines
# are LLM guesses and too noisy to interrupt with.
.eq("deadline_confidence", "EXPLICIT")
```

Same filter in `processors/proactive_alerts.py` for CRITICAL overdue alerts.

**Do not filter** the morning brief task list or MCP `get_tasks()` — only filter notification paths.

### 2.5 Visual differentiation in morning brief

**File:** `processors/morning_brief.py` (and any formatter that renders task rows)

- `EXPLICIT`: `Due: Mar 15`
- `INFERRED`: `Due: ~Mar 15 (estimated)`
- `NONE`: *(no deadline shown)*

### 2.6 Backfill existing tasks

One-time script `scripts/backfill_deadline_confidence.py`:
- Tasks with `deadline IS NOT NULL` → `'INFERRED'` (we can't know if they were explicit)
- Tasks with `deadline IS NULL` → `'NONE'`

### Tests for PR 2
**File:** `tests/test_deadline_confidence.py`
- Extraction correctness on sample snippets ("deliver by Friday" → EXPLICIT, "urgent" → INFERRED, "investigate X" → NONE)
- `update_task_deadline()` defaults to EXPLICIT
- Reminder scheduler excludes INFERRED/NONE
- Morning brief shows `~` prefix for INFERRED

---

## PR 3 — Observation Capture Layer

**Goal:** Write a row to `approval_observations` at every approval decision point. Zero LLM cost. Fire-and-forget, never blocks the approval flow.

*(Unchanged from original plan. Full scope carries over.)*

### 3.1 Supabase helper

**File:** `services/supabase_client.py`

```python
def log_approval_observation(
    self,
    content_type: str,
    action: str,                          # 'approved' | 'edited' | 'rejected'
    content_id: Optional[str] = None,
    original_content: Optional[dict] = None,
    final_content: Optional[dict] = None,
    context: Optional[dict] = None,
) -> None:
    """
    Log an approval decision observation.
    Fires-and-forgets — never raises. Failures are logged but
    do not interrupt the approval flow.
    """
    edit_distance_pct = None
    if original_content and final_content and action == "edited":
        orig_str = str(original_content)
        final_str = str(final_content)
        if orig_str:
            from difflib import SequenceMatcher
            ratio = SequenceMatcher(None, orig_str, final_str).ratio()
            edit_distance_pct = round(1.0 - ratio, 3)

    try:
        self.client.table("approval_observations").insert({
            "content_type": content_type,
            "action": action,
            "content_id": content_id,
            "original_content": original_content,
            "final_content": final_content,
            "edit_distance_pct": edit_distance_pct,
            "context": context or {},
        }).execute()
    except Exception as e:
        logger.warning(f"[observation] failed to log {content_type}/{action}: {e}")
        # Never propagate — observations are non-critical
```

### 3.2 Hook into approval flow

**File:** `guardrails/approval_flow.py`

After each primary action succeeds (never before, never blocking):

| Decision point | `content_type` | `action` |
|---|---|---|
| Meeting summary approved unchanged | `meeting_summary` | `approved` |
| Meeting summary approved after edits | `meeting_summary` | `edited` |
| Meeting summary rejected | `meeting_summary` | `rejected` |
| Gantt proposal approved | `gantt_proposal` | `approved` |
| Gantt proposal rejected | `gantt_proposal` | `rejected` |
| Intelligence signal approved | `intelligence_signal` | `approved` |
| Intelligence signal rejected | `intelligence_signal` | `rejected` |
| Meeting prep approved | `meeting_prep` | `approved` |
| Meeting prep rejected | `meeting_prep` | `rejected` |

Pass `content_id` = the relevant UUID. Pass `context` = `{"meeting_title": ..., "sensitivity": ...}` where available.

### 3.3 Hook into quick inject

**File:** `processors/debrief.py` (after `confirm_quick_inject` saves)

### 3.4 Hook into Sheets sync

**File:** wherever `sync_from_sheets(apply=True)` executes.

### 3.5 MCP tool: `get_approval_stats`

**File:** `services/mcp_server.py` (append-only)

```python
@mcp.tool()
def get_approval_stats(days: int = 30) -> dict:
    """
    [SYSTEM] Approval pattern stats from the observation log.
    Shows approval rates, edit rates, and rejection rates by content type.
    """
```

### Tests for PR 3
**File:** `tests/test_approval_observations.py`
- Row written correctly
- Fire-and-forget: DB failure does NOT raise
- `edit_distance_pct` computed correctly
- Observation logged on approve/reject for meetings, signals, gantt

---

## PR 4 — Living Topic-State Summaries ★ NEW

**Goal:** Every canonical topic (Moldova Pilot, Legal, WEU Marketing, etc.) maintains a continuously-updated structured state in `topic_threads.state_json`. Updated incrementally as each new meeting mentions the topic. Exposed via MCP so Eyal (and eventually v2.4's agentic synthesis) can query "what's the state of X?" and get both structured data and prose narrative.

**Why this matters:** Current `topic_threads.evolution_summary` is generated once per thread and is narrative-only. It decays fast and has no structure for the LLM to lean on. A structured state that tracks current status, stakeholders, open items, blockers, and last decision — updated on every mention — becomes the highest-signal chunk in the entire corpus for thematic questions.

**Files touched:**
- `models/schemas.py` — new `TopicState` Pydantic model
- `processors/topic_threading.py` — **main work**, add `update_topic_state()`
- `services/supabase_client.py` — helpers to read/write state_json
- `services/mcp_server.py` — update `get_topic_thread` + `list_topic_threads` to return state
- `processors/morning_brief.py` — optional: surface 1-2 hot-topic state deltas
- `main.py` or `processors/transcript_processor.py` — wire state update into post-extraction pipeline

### 4.1 Pydantic model for topic state

**File:** `models/schemas.py`

```python
class TopicStatus(str, Enum):
    ACTIVE = "active"              # progressing normally
    BLOCKED = "blocked"            # waiting on external action
    PENDING_DECISION = "pending_decision"  # open decision point
    STALE = "stale"                # no mention in 30+ days
    CLOSED = "closed"              # explicitly resolved

class OpenItem(BaseModel):
    """A single unresolved item attached to a topic state."""
    kind: str                      # 'task' | 'question' | 'blocker'
    description: str
    owner: Optional[str] = None
    source_meeting_id: Optional[str] = None

class LastDecision(BaseModel):
    text: str
    date: str                      # ISO date
    meeting_id: str
    meeting_title: Optional[str] = None

class TopicState(BaseModel):
    """
    Structured, continuously-updated state for a topic thread.
    Stored in topic_threads.state_json.
    """
    current_status: TopicStatus = TopicStatus.ACTIVE
    summary: str                   # 2-3 sentence current-state narrative
    stakeholders: list[str] = []   # people actively involved
    open_items: list[OpenItem] = []
    last_decision: Optional[LastDecision] = None
    key_facts: list[str] = []      # durable facts (e.g., "Moldova PoC targeted Q3 2026")
    last_activity_date: Optional[str] = None
    version: int = 1               # bumped on each update
```

### 4.2 Incremental state update function

**File:** `processors/topic_threading.py`

New async function `update_topic_state(topic_id, meeting_id, decisions, tasks, open_questions)`:

**When to call:** from `link_meeting_to_topics()`, immediately after `_update_thread_for_meeting()` or `_create_thread()` returns a thread. Call it for every thread the meeting touched.

**What it does:**
1. Load existing `state_json` (if any) + this meeting's metadata (title, date, summary).
2. Load the decisions/tasks/open_questions linked to this topic in this meeting.
3. Send a Haiku prompt: `(previous_state, new_meeting_context) → new_state`.
4. Parse the returned JSON against `TopicState` schema.
5. Save to `topic_threads.state_json` + bump `state_updated_at`.
6. On failure: log warning, keep previous state, do not raise.

**Haiku prompt (target 300-400 input tokens, 250-350 output):**

```
You maintain structured state for a CropSight topic thread.

Previous state (may be empty — this could be a brand-new topic):
{previous_state_json}

New meeting just happened:
- Date: {meeting_date}
- Title: {meeting_title}
- Decisions this meeting: {decisions_summary}
- Tasks this meeting: {tasks_summary}
- Open questions this meeting: {open_questions_summary}

Update the topic state. Return JSON matching this schema:
{
  "current_status": "active" | "blocked" | "pending_decision" | "stale" | "closed",
  "summary": "2-3 sentence current-state narrative",
  "stakeholders": ["names of people actively involved"],
  "open_items": [
    {"kind": "task"|"question"|"blocker", "description": "...", "owner": "name or null"}
  ],
  "last_decision": {"text": "...", "date": "YYYY-MM-DD", "meeting_id": "...", "meeting_title": "..."} or null,
  "key_facts": ["durable facts about this topic — milestones, targets, structural decisions"],
  "last_activity_date": "YYYY-MM-DD"
}

Rules:
- Preserve key_facts from previous state unless explicitly contradicted.
- Replace last_decision only if this meeting made a new decision on this topic.
- Remove open_items that were resolved in this meeting.
- Add new open_items from this meeting's tasks/questions.
- Set current_status = blocked if an explicit blocker was mentioned, pending_decision if open question dominates, else active.
- Keep summary to 2-3 sentences. Focus on current state, not history.
```

**Cost:** ~$0.0002 per mention (Haiku, small input/output). For CropSight's cadence (~10 meetings/week, ~3 topic mentions per meeting), that's $0.006/week — negligible.

### 4.3 Canonical-project seed

**File:** new script `scripts/seed_canonical_projects.py` (one-time)

For each row in `canonical_projects`, ensure a `topic_threads` row exists with matching name. This gives us a stable anchor for the top ~10 strategic topics. State will populate as meetings come in.

### 4.4 Backfill from existing threads

**File:** new script `scripts/backfill_topic_state.py` (one-time)

For each existing topic_thread with `meeting_count > 0`:
- Load all its mentions (with linked decisions/tasks)
- Run a single Sonnet pass (not Haiku — backfill gets one high-quality shot): construct a TopicState from the full timeline
- Write to `state_json`

One-time cost: ~50 active topics × 1 Sonnet call ≈ $0.15. Done.

### 4.5 Wire update into transcript pipeline

**File:** `processors/transcript_processor.py` (or wherever `link_meeting_to_topics()` is called after approval)

After `link_meeting_to_topics()` returns, iterate the linked threads and call `update_topic_state()` for each. Run these **after approval** (not at extraction time) — we only want to update state from approved meetings, mirroring the T3.1 `approval_status` gating pattern.

### 4.6 Expose state via MCP

**File:** `services/mcp_server.py`

Update (don't replace) `get_topic_thread` and `list_topic_threads`:

```python
@mcp.tool()
def get_topic_thread(topic_name: str, include_state: bool = True) -> dict:
    """
    Get a topic thread by name. If include_state=True (default), returns
    the structured state_json alongside the prose evolution_summary.
    """
    # ... existing logic ...
    # Then add state_json to the returned payload
```

```python
@mcp.tool()
def list_topic_threads(
    status: Optional[str] = None,
    include_state: bool = False,
) -> dict:
    """
    List topic threads. include_state=True adds state_json per thread
    (heavier payload, use when you need structured state).
    """
```

### 4.7 Morning brief integration (light touch)

**File:** `processors/morning_brief.py`

If any topic's `state_json.current_status == 'blocked'` or `last_activity_date` is >14 days old for a topic with >3 meeting mentions, surface a one-liner in the "Needs attention" section:

`"Legal: blocked on Yoram's signature for 12 days."`

Cap at 2 topic-state lines per brief — do not spam. Skip entirely if none qualify.

### 4.8 Staleness sweep (cheap daily job)

**File:** `schedulers/qa_scheduler.py` (extend existing scheduler, do not add new one)

Once daily, for every topic_thread with `last_updated < now - 30 days` and `status = 'active'`, update `state_json.current_status = 'stale'` (metadata-only, no LLM call). This keeps the structured-state surface accurate without cost.

### Tests for PR 4

**File:** `tests/test_topic_state.py`

- `update_topic_state()` on empty previous-state creates a valid `TopicState`
- Incremental update: previous state's `key_facts` preserved unless contradicted
- Meeting resolving a task removes it from `open_items`
- Haiku returns malformed JSON → previous state preserved, warning logged, no raise
- MCP `get_topic_thread(include_state=True)` returns state_json in payload
- MCP `get_topic_thread(include_state=False)` excludes state_json
- Morning brief includes blocked-topic line when applicable, skips otherwise

**File:** `tests/test_topic_state_backfill.py`
- Backfill script generates state for all threads with mentions
- Idempotent (running twice does not duplicate or corrupt)

### Manual smoke test after deploy

1. Run backfill script. Spot-check 3 state_json rows for sensible content.
2. Ingest a new transcript. Verify state_json updates on its linked topics.
3. Via MCP: `get_topic_thread("Moldova Pilot")` — verify state + narrative both present.
4. Check morning brief the next day — if any topic is blocked, verify it shows up.

---

## PR 5 — Inline Deadline Buttons

**Goal:** Add `[+1 week]` and `[Clear date]` inline buttons to task reminder Telegrams and the overdue section of the morning brief. One tap to fix a stale deadline. Logs an `approval_observation` on tap.

*(Unchanged from original plan — scope carries over. One voice-alignment note added to confirmation messages per the v2.2 Telegram Comms Overhaul.)*

### 5.1 Callback data format

**File:** `services/telegram_bot.py`

```python
# Callback data format:
# "deadline_plus_week:{task_id}"
# "deadline_clear:{task_id}"
```

### 5.2 Buttons on task reminders

**File:** `schedulers/task_reminder_scheduler.py`

```python
def _build_task_reminder_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✓ Done", callback_data=f"task_done:{task_id}"),
            InlineKeyboardButton("→ In Progress", callback_data=f"task_progress:{task_id}"),
        ],
        [
            InlineKeyboardButton("⏰ +1 week", callback_data=f"deadline_plus_week:{task_id}"),
            InlineKeyboardButton("🗑 Clear date", callback_data=f"deadline_clear:{task_id}"),
        ],
    ])
```

### 5.3 Buttons on morning brief overdue section

For each overdue task: if `deadline_confidence == 'EXPLICIT'` → both buttons. If `'INFERRED'` → only `[🗑 Clear date]` (offering `+1 week` on a guess propagates the guess).

### 5.4 Callback handlers

**File:** `services/telegram_bot.py`

Both handlers must:
1. Fetch task via `get_task_by_id()`
2. Compute new deadline (now + 7 days, or task.deadline + 7 days if present)
3. Call `update_task_deadline(task_id, new_deadline, confidence="EXPLICIT")`
4. Call `log_approval_observation(content_type="deadline_update", action="edited", ...)` (from PR 3)
5. Edit the original message with a confirmation

**Voice for confirmation messages** (align with v2.2 Telegram Comms Overhaul — no system-speak):
- Plus-week: `"Pushed to {date} — {task_title}"` (not `"⏰ Deadline pushed to..."`)
- Clear: `"Date cleared — {task_title}"` (not `"🗑 Deadline cleared"`)
- Task not found: `"Can't find that task."` (not `"Task not found."`)
- Error: `"Couldn't update — try again?"` (not `"Failed to update deadline. Try again."`)

Use `parse_mode="HTML"` (matches the v2.2 parse_mode migration).

Register both handlers:
```python
application.add_handler(CallbackQueryHandler(_handle_deadline_plus_week, pattern=r"^deadline_plus_week:"))
application.add_handler(CallbackQueryHandler(_handle_deadline_clear, pattern=r"^deadline_clear:"))
```

### 5.5 `get_task_by_id` helper if missing

**File:** `services/supabase_client.py`

```python
def get_task_by_id(self, task_id: str) -> Optional[dict]:
    result = (
        self.client.table("tasks")
        .select("*")
        .eq("id", task_id)
        .eq("approval_status", "approved")
        .single()
        .execute()
    )
    return result.data if result.data else None
```

### Tests for PR 5

**File:** `tests/test_deadline_buttons.py`
- `+1 week` pushes by exactly 7 days
- `+1 week` on no-deadline task creates one 7 days out
- `Clear date` sets deadline=None, confidence=NONE
- Both edit the original message (mock Telegram query)
- Both log an `approval_observation` row
- Non-existent task_id returns graceful voice-aligned error
- Confirmation messages match voice guidelines (no emoji-first system-speak)

---

## Deployment Checklist

After all PRs merged:

```bash
# 1. Run DB migration
psql $SUPABASE_DB_URL -f scripts/migrate_v2_3.sql

# 2. Seed canonical project threads (if any are missing)
python scripts/seed_canonical_projects.py

# 3. Backfill deadline confidence
python scripts/backfill_deadline_confidence.py

# 4. Backfill topic state (one-time Sonnet pass per active thread)
python scripts/backfill_topic_state.py

# 5. Run full test suite
pytest --tb=short 2>&1 | tail -30
# Expect same 19 pre-existing failures, nothing new

# 6. Deploy to Cloud Run
gcloud run deploy gianluigi --source . --region europe-west1

# 7. Smoke tests
#  - Send a task reminder, verify [+1 week] / [Clear date] buttons appear
#  - Tap [+1 week], verify DB update + confidence=EXPLICIT + observation row
#  - Via MCP: get_topic_thread("Moldova Pilot", include_state=True) — state present
#  - Ingest a transcript that touches a known topic — verify state_json updated
#  - Approve/reject a meeting — verify approval_observations row written
#  - Next morning, check brief — INFERRED deadlines prefixed with ~, any blocked topics surfaced
```

---

## CLAUDE.md Updates (after all PRs merge)

```
**Current Version:** v2.3 (... + Operational Learning Infrastructure + Deadline Confidence + Living Topic-State)

**v2.3:** Observation capture layer (approval_observations table, fire-and-forget logging at all approval decision points), deadline confidence tiers (EXPLICIT/INFERRED/NONE on tasks, extraction-time classification, reminders filter to EXPLICIT only), living topic-state summaries (topic_threads.state_json, Haiku-powered incremental updates per mention, structured state with status/stakeholders/open_items/last_decision/key_facts, MCP get_topic_thread returns state + narrative, morning brief surfaces blocked/stale topics), inline deadline buttons ([+1 week] / [Clear date] on task reminders and morning brief overdue section, voice-aligned confirmations, sets EXPLICIT confidence on tap).
```

---

## What is NOT in v2.3 (explicitly deferred to v2.4)

- **HyDE** — dropped outright. Agentic multi-pass retrieval in v2.4 supersedes the question→answer embedding trick. The topic-state `state_json` from PR 4 already gives synthesis questions a high-signal chunk to lean on without HyDE.
- **Agentic synthesis pipeline** — decompose thematic queries into sub-questions, multi-pass retrieval against topic-state + chunks + linked tasks/decisions, Sonnet/Opus synthesis. Full v2.4 scope.
- **Thematic-query observation logging** — expand `approval_observations` to log queries, topics resolved, and answer feedback. v2.4 (needs agentic synthesis to produce the observations).
- **Nightly memory consolidation job** — rewrites topic-state summaries from the week's meetings. v2.4 extension of PR 4's on-mention updates.
- **RAG 550-token context cap** — deferred. By v2.4 we'll have logged query patterns + context sizes and can set the cap empirically instead of guessing. Not needed now.
- **Canonical project resolution at query time** — currently used only at extraction. v2.4 agentic path will resolve `query → canonical project → topic-state + scoped chunks`.

---

## What changed from the original v2.3 plan

Original plan (6 PRs): migration, deadline confidence, observations, **HyDE**, deadline buttons, **RAG cap**.

Revised plan (5 PRs): migration, deadline confidence, observations, **living topic-state**, deadline buttons.

**Pivot rationale (from 2026-04-13 brainstorm):**
- Eyal's stated v2.3 driver: Gianluigi failing on synthesis questions ("where are we on legal?", "how's WEU marketing?") — the core gap for the office-manager role.
- HyDE optimizes single-shot chunk retrieval. On a small corpus, retrieval isn't the bottleneck — synthesis from fragments is. HyDE would also risk Haiku hallucinating CropSight-specific details (fake dates, fake stakeholders) that pull retrieval away from ground truth.
- RAG context cap was premature — original plan called for audit first; on a small corpus it's almost certainly a no-op.
- Living topic-state gives the LLM a continuously-updated, structured, high-signal chunk per topic. Most of the synthesis gap is "chunks are too fragmentary to answer thematic questions" — state_json fixes that at the data layer.
- v2.4 adds agentic query decomposition + multi-pass retrieval on top — by then, topic-state has 4-6 weeks of incremental data and isn't cold-starting.

**Roadmap continuity:** v2.4 scope documented in memory (`project_v2_4_roadmap.md`) — do not re-brainstorm.
