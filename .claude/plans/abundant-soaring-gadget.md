# CropSight Intelligence Signal — Implementation Plan

## Context

CropSight is a 4-person pre-revenue AgTech startup. Nobody on the team has time to systematically scan commodity markets, competitor moves, regulatory changes, and science signals weekly. The Intelligence Signal turns Gianluigi from a backward-looking system (what happened in our meetings) into a forward-looking one (what's happening in our market). It gives the team a shared information baseline before Friday's management meeting.

Cost: ~$0.60/week (Perplexity + Opus). Annual with video: ~$40/year.

Key brainstorming decisions:
- Name: "CropSight Intelligence Signal" everywhere (no "brief")
- Retry chain: Perplexity → Perplexity (+2h) → Claude search fallback (built from start)
- 2-3 rotating exploration queries to avoid filter bubble
- Competitor watchlist auto-curates (3+ appearances = promote, 4 weeks silent = deactivate, zero Eyal intervention) with visibility in Telegram notification
- First week: email only to Eyal. Auto-distribute setting available but defaults to False.
- Approval via MCP tool. Telegram is notification-only with Drive link.
- Video: PIL + bundled Inter font + ffmpeg. "News flash" style. Built disabled.
- Character: news anchor / editor-in-chief. Engaged journalist. No opinions or recommendations.
- Written output: Google Doc on Drive. HTML email with flags + link.
- RAG integration deferred. Store signals in own table, query last 3-4 directly for continuity.

Architecture review fixes incorporated (from docs/qa/INTELLIGENCE_SIGNAL_REVIEW.md):
1. flags JSONB column added to schema (structured flags for Telegram/email formatting)
2. Pipeline reordered: signal_content saved to DB before Drive upload (prevents content loss)
3. Competitor auto-curation changes surfaced in Telegram notification (non-blocking visibility)
4. Opus synthesis wrapped in ThreadPoolExecutor with 120s timeout (prevents event loop blocking)
5. Auto-distribute setting KEPT (disagreed with reviewer) — defaults to False, dormant until needed
6. "Exploration Corner" as explicit named section in synthesis prompt
7. Research source transparency in Telegram notification (fallback warning)
8. Prompt calibration gate added to verification plan
9. research_results JSONB truncated to 3KB per result before storage

---
## Files Overview

### New files (9)

| File | Purpose |
|------|---------|
| scripts/migrate_intelligence_signal.sql | DB tables (intelligence_signals + competitor_watchlist) |
| scripts/seed_competitor_watchlist.py | One-time seed of known competitors |
| services/perplexity_client.py | Perplexity API + retry chain + Claude search fallback |
| processors/intelligence_signal_context.py | Context packet builder (Supabase reads + exploration queries) |
| processors/intelligence_signal_prompts.py | All prompts (news anchor character, synthesis, script, email) |
| processors/intelligence_signal_agent.py | Main orchestration pipeline |
| schedulers/intelligence_signal_scheduler.py | Thursday 18:00 IST trigger (class pattern) |
| services/elevenlabs_client.py | ElevenLabs TTS (built, disabled) |
| services/video_assembler.py | PIL slides + ffmpeg video assembly (built, disabled) |

### Modified files (7, append-only)

| File | Change |
|------|--------|
| config/settings.py | 12 new env vars + 1 property |
| services/google_drive.py | 3 new methods: create_subfolder(), save_intelligence_signal(), save_intelligence_signal_video() |
| services/supabase_client.py | 8 new methods for intelligence_signals + competitor_watchlist CRUD |
| services/mcp_server.py | 5 new MCP tools (append after sync_from_sheets) |
| main.py | Scheduler registration in start_services() + stop_services() |
| Dockerfile | Add ffmpeg to production stage apt-get |
| requirements.txt | Add Pillow>=10.4.0 |

### New assets

| Path | Purpose |
|------|---------|
| assets/fonts/Inter-Regular.ttf | Font for video slides (OFL license, ~300KB) |
| assets/fonts/Inter-Bold.ttf | Bold font for video slide headers |

---
## Step 1: Foundation — DB + Settings + Dependencies

### 1A: Migration (scripts/migrate_intelligence_signal.sql)

```sql
-- intelligence_signals table
CREATE TABLE IF NOT EXISTS intelligence_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id TEXT UNIQUE NOT NULL,        -- "signal-w14-2026"
    week_number INTEGER NOT NULL,
    year INTEGER NOT NULL,
    status TEXT DEFAULT 'generating',      -- generating | pending_approval | approved | distributed | error
    context_snapshot JSONB,
    research_results JSONB,
    signal_content TEXT,                   -- full written report
    flags JSONB,                           -- [{flag: str, urgency: "high"|"medium"}] max 3
    script_text TEXT,                      -- video narration script
    drive_doc_id TEXT,
    drive_doc_url TEXT,
    drive_video_id TEXT,
    drive_video_url TEXT,
    approval_id TEXT,
    recipients TEXT[],
    distributed_at TIMESTAMPTZ,
    research_source TEXT,                  -- perplexity | perplexity_retry | claude_search
    perplexity_queries_run INTEGER DEFAULT 0,
    generation_cost_usd NUMERIC(10,4),
    token_usage JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intelligence_signals_week_year
    ON intelligence_signals(week_number, year);
CREATE INDEX IF NOT EXISTS idx_intelligence_signals_status
    ON intelligence_signals(status);

-- competitor_watchlist table
CREATE TABLE IF NOT EXISTS competitor_watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    category TEXT DEFAULT 'known',         -- known | discovered | watching
    funding TEXT,
    target_customer TEXT,
    key_limitation TEXT,
    notes TEXT,
    appearance_count INTEGER DEFAULT 0,
    last_seen_week INTEGER,
    last_seen_year INTEGER,
    added_by TEXT DEFAULT 'system',        -- system | eyal | auto_discovered
    is_active BOOLEAN DEFAULT TRUE,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_competitor_watchlist_active
    ON competitor_watchlist(is_active);
```

### 1B: Seed script (scripts/seed_competitor_watchlist.py)

7 known competitors from the spec: EOSDA, SatYield, CropProphet, SeeTree, Gro Intelligence, aWhere/DTN, Cropin. Uses supabase_client.client.table("competitor_watchlist").upsert(..., on_conflict="name").

### 1C: Settings (config/settings.py)

```python
# Intelligence Signal
INTELLIGENCE_SIGNAL_ENABLED: bool = Field(default=False, description="Enable weekly intelligence signal scheduler")
INTELLIGENCE_SIGNAL_DAY: int = Field(default=3, description="Day of week (0=Mon, 3=Thu)")
INTELLIGENCE_SIGNAL_HOUR: int = Field(default=18, description="IST hour for generation")
INTELLIGENCE_SIGNAL_RECIPIENTS: str = Field(default="", description="Comma-separated emails (empty = Eyal only)")
INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE: bool = Field(default=False, description="Skip approval gate (keep False until quality proven)")
INTELLIGENCE_SIGNAL_VIDEO_ENABLED: bool = Field(default=False, description="Enable video generation")
INTELLIGENCE_SIGNAL_FOLDER_ID: str = Field(default="", description="Drive folder ID for signals")
PERPLEXITY_API_KEY: str = Field(default="", description="Perplexity API key")
PERPLEXITY_MODEL: str = Field(default="sonar-pro", description="Perplexity model")
ELEVENLABS_API_KEY: str = Field(default="", description="ElevenLabs API key")
ELEVENLABS_VOICE_ID: str = Field(default="21m00Tcm4TlvDq8ikWAM", description="ElevenLabs voice (Rachel)")
```

Property:
```python
@property
def intelligence_signal_recipients_list(self) -> list[str]:
    """Parse signal recipients. Empty = Eyal only."""
    if not self.INTELLIGENCE_SIGNAL_RECIPIENTS:
        return [self.EYAL_EMAIL] if self.EYAL_EMAIL else []
    return [e.strip() for e in self.INTELLIGENCE_SIGNAL_RECIPIENTS.split(",") if e.strip()]
```

### 1D: Dockerfile — add ffmpeg

### 1E: requirements.txt — add Pillow

---
## Step 2: Perplexity Client (services/perplexity_client.py)

Async client using httpx. Singleton pattern.
- search(query, system_prompt) -> PerplexityResult
- search_batch(queries, max_concurrent=6) -> dict[str, PerplexityResult]
- is_available() -> bool

Tests (~10): Mock httpx responses.

---
## Step 3: Context Builder (processors/intelligence_signal_context.py)

Builds the context packet from Supabase. All reads are SYNC.

Key function: `build_context_packet() -> dict`

Returns:
```python
{
    "week_number": int,
    "year": int,
    "signal_id": "signal-w14-2026",
    "active_crops": ["wheat", "coffee", "cocoa", "grapes"],
    "active_regions": ["Moldova", "Black Sea", ...],
    "active_bd_pipeline": [...],
    "technical_focus": [...],
    "known_competitors": [...],
    "last_signal_flags": [...],
    "open_tasks_summary": {...},
}
```

`build_research_queries(context) -> list[dict]` — generates ~12 Perplexity queries from context

`build_exploration_queries(week_number) -> list[dict]` — generates 2-3 rotating out-of-scope queries:
- Adjacent markets pool (aquaculture, forestry, livestock, palm oil)
- Wild card crops pool (rice, avocado, saffron, vanilla, cotton)
- Unexplored geographies pool (Indonesia, Kenya, Peru, Vietnam, Thailand)
- Uses week_number % len(pool) for rotation

Critical patterns:
- supabase_client.get_tasks(assignee="Paolo", status="pending") — SYNC
- supabase_client.client.table("competitor_watchlist").select(...) — SYNC
- Falls back to DEFAULT_ACTIVE_CROPS / DEFAULT_ACTIVE_REGIONS if Supabase is sparse

Tests (~10): Empty DB returns defaults, populated DB returns enriched context, exploration queries rotate.

---
## Step 4: Prompts (processors/intelligence_signal_prompts.py)

All prompts in one file.

`system_prompt_synthesis() -> str` — The news anchor character:
- Engaged, energetic journalist covering AgTech/commodities
- No opinions, no recommendations, no agenda
- Penalize false confidence, reward honest uncertainty
- Banned phrases list
- Allow empty sections (no padding)

`user_prompt_synthesis(context, research_results) -> str` — Section structure:
- Flags (max 3, decision-relevant only)
- The Problem, This Week
- New Horizons
- Commodity Pulse, Regional Watch, Regulatory Radar
- Competitive Landscape, AgTech Funding
- Science & Tech Signals
- Exploration Corner — explicit named section
- This Week's Angle

`system_prompt_script() -> str` — News flash narrator
`user_prompt_script(signal_content) -> str`

`format_telegram_notification(signal_id, drive_link, week_number, flags, research_source=None, watchlist_changes=None) -> str`
`format_email_html(signal_content, drive_link, week_number, year, flags) -> str`
`format_email_plain(signal_content, drive_link) -> str`

Tests (~5): Prompts contain expected traits, drive links, fallback warning.

---
## Step 5: Drive Service Extension (services/google_drive.py)

3 new methods:

```python
async def create_subfolder(self, name: str, parent_folder_id: str) -> dict
async def save_intelligence_signal(self, content: str, filename: str) -> dict
async def save_intelligence_signal_video(self, data: bytes, filename: str) -> dict
```

Tests (~3): Mock self.service.files().create().execute().

---
## Step 6: Main Agent (processors/intelligence_signal_agent.py)

Pipeline order:
1. Create DB record (status='generating')
2. Build context packet
3. Build research queries + exploration queries
4. Execute Perplexity batch search
5. If batch fails → retry chain
6. Store research_source in DB
7. Truncate research_results to 3KB per result
8. Synthesize with Opus via timeout-guarded thread executor (120s)
9. Store signal_content + flags to DB immediately (before Drive upload)
10. Upload as Google Doc to Drive
11. Update DB with drive_doc_id + drive_doc_url
12. If video enabled: generate script → TTS → slides → ffmpeg → upload
13. Update competitor watchlist → returns watchlist_changes dict
14. Submit for approval OR auto-distribute
15. Update status to pending_approval

Key functions:
- `generate_intelligence_signal(signal_id=None) -> dict`
- `distribute_intelligence_signal(signal_id) -> dict`
- `_submit_for_approval(signal_id, drive_link, week_number, flags, research_source, watchlist_changes)`
- `_update_competitor_watchlist(research_results, week_number, year) -> dict`
- `_run_synthesis_with_timeout(prompt, system, timeout_seconds=120)`

Tests (~25): Full pipeline, retry chain, timeout, distribution, competitor auto-curation.

---
## Step 7: MCP Tools (services/mcp_server.py)

5 new tools (38 → 43):

| # | Tool | Type | Description |
|---|------|------|-------------|
| 39 | get_intelligence_signal_status | Read | Latest signal status, flags, Drive links, next scheduled |
| 40 | approve_intelligence_signal | Write | Approve + distribute (or cancel with cancel=True) |
| 41 | trigger_intelligence_signal | Write | Manual ad-hoc generation |
| 42 | get_competitor_watchlist | Read | Auto-curated watchlist with categories |
| 43 | add_competitor | Write | Manually add a competitor |

Tests (~15): All 5 tools with success/error/edge cases.

---
## Step 8: Scheduler (schedulers/intelligence_signal_scheduler.py)

Class pattern matching MorningBriefScheduler. Thursday 18:00 IST. ZoneInfo("Asia/Jerusalem").

Registration in main.py start_services() and stop_services().

Tests (~8): Sleep calculation, duplicate week skip, heartbeat.

---
## Step 9: Video Pipeline (built, disabled)

- services/elevenlabs_client.py — TTS
- services/video_assembler.py — PIL slides + ffmpeg
- Font assets in assets/fonts/

Tests (~12): All mocked.

---
## Supabase Client Additions

8 new SYNC methods:
- create_intelligence_signal(data) -> dict
- update_intelligence_signal(signal_id, updates) -> dict
- get_intelligence_signal(signal_id) -> dict | None
- get_latest_intelligence_signal() -> dict | None
- get_intelligence_signals(limit=4) -> list[dict]
- get_competitor_watchlist(include_deactivated=False) -> list[dict]
- upsert_competitor(data) -> dict
- deactivate_stale_competitors(weeks_threshold=4) -> int

---
## Verification Plan

After Step 6 — Manual Calibration Gate:
1. Trigger 3 manual signal generations
2. Read each Google Doc completely
3. Iterate character prompt until consistent quality
4. Only after 2+ consecutive good outputs: set INTELLIGENCE_SIGNAL_RECIPIENTS to team

After Step 8 — Full integration test via MCP tools.

---
## Architecture Review Fixes Summary

| # | Fix | Status |
|---|-----|--------|
| 1 | flags JSONB column in schema | Incorporated |
| 2 | signal_content to DB before Drive upload | Incorporated |
| 3 | Watchlist changes in Telegram notification | Incorporated |
| 4 | Opus synthesis timeout (ThreadPoolExecutor + 120s) | Incorporated |
| 5 | Auto-distribute setting | KEPT at default=False |
| 6 | Exploration Corner as explicit section | Incorporated |
| 7 | Research source transparency in Telegram | Incorporated |
| 8 | Prompt calibration gate | Incorporated |
| 9 | research_results JSONB truncation (3KB/result) | Incorporated |

Estimated: ~90 new tests, ~1540 total.
