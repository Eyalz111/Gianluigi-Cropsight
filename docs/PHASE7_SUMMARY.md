# Phase 7: MCP Core + Read Tools — Implementation Summary

**Implemented:** March 21, 2026
**Status:** Complete, smoke-tested locally with real Supabase data

---

## What Was Built

MCP server for Claude.ai as Eyal's CEO dashboard. 15 read-only tools, SSE transport, bearer token auth, rate limiting, audit logging. Zero new business logic — every tool is a thin wrapper around existing brain functions.

### Architecture

```
Claude.ai ──SSE──> MCP Server (Starlette/uvicorn, port 8080)
                       │
                       ├── Auth Middleware (pure ASGI, bearer token)
                       ├── Rate Limiting (100 calls/hr sliding window)
                       ├── Health/Ready/Reports (custom routes)
                       │
                       └── 15 MCP Tools ──> Existing Brain Functions
                                              (same functions Telegram uses)
```

When `MCP_AUTH_TOKEN` is set, the MCP Starlette server replaces the aiohttp health server on port 8080. When not set, the original aiohttp health server runs (backward compatible).

### Files Created

| File | Purpose |
|------|---------|
| `services/mcp_server.py` | FastMCP server, 15 tools, health/ready/report routes, uvicorn lifecycle |
| `guardrails/mcp_auth.py` | Bearer token auth, rate limiting, audit logging, pure ASGI middleware |
| `scripts/migrate_phase7.sql` | Index on `mcp_sessions(session_date DESC)` |
| `tests/test_mcp_auth.py` | 17 tests |
| `tests/test_mcp_tools.py` | 24 tests |
| `tests/test_mcp_server.py` | 19 tests |

### Files Modified

| File | Change |
|------|--------|
| `config/settings.py` | Added `MCP_RATE_LIMIT_PER_HOUR` (default 100) |
| `requirements.txt` | Added `mcp>=1.0.0`, `uvicorn>=0.30.0` |
| `main.py` | Conditional MCP server start when `MCP_AUTH_TOKEN` is set |
| `CLAUDE.md` | Updated phase status, test count, tech stack |

### MCP Tools (15 read-only)

| # | Tool | Wraps | Purpose |
|---|------|-------|---------|
| 1 | `get_system_context()` | Composite (supabase + alerts + team) | Onboarding — called first in every session |
| 2 | `get_last_session_summary()` | `supabase_client.get_latest_mcp_session()` | Session continuity |
| 3 | `save_session_summary()` | `supabase_client.create_mcp_session()` | Persist session notes |
| 4 | `search_memory()` | `supabase_client.search_memory()` + embeddings | Hybrid RAG search |
| 5 | `get_tasks()` | `supabase_client.get_tasks()` | Task queries with filters |
| 6 | `get_decisions()` | `supabase_client.list_decisions()` | Decision history |
| 7 | `get_open_questions()` | `supabase_client.get_open_questions()` | Unresolved questions |
| 8 | `get_commitments()` | `supabase_client.get_commitments()` | Commitment tracker |
| 9 | `get_stakeholder_info()` | `sheets_service.get_stakeholder_info()` | Stakeholder records |
| 10 | `get_meeting_history()` | `supabase_client.list_meetings()` | Recent meetings |
| 11 | `get_pending_approvals()` | `supabase_client.get_pending_approval_summary()` | Approval queue |
| 12 | `get_gantt_status()` | `gantt_manager.get_gantt_status()` | Current Gantt state |
| 13 | `get_gantt_horizon()` | `gantt_manager.get_gantt_horizon()` | Upcoming milestones |
| 14 | `get_upcoming_meetings()` | `calendar_service.get_upcoming_events()` + prep status | Calendar + prep |
| 15 | `get_weekly_summary()` | `weekly_review.compile_weekly_review_data()` | Compiled weekly review data |

### Test Results

- 60 new MCP tests, all passing
- 1348 total tests (1288 existing + 60 new)
- 0 regressions

### Smoke Test Results (Live, March 21)

- SSE connection with bearer token: working
- Auth rejection (no token / bad token): 401
- MCP protocol init: `gianluigi v1.26.0`
- `tools/list`: 15 tools discovered
- `get_pending_approvals`: 1 approval (real Supabase)
- `get_system_context`: Week 12, team of 4, real operational state
- `save_session_summary`: Session persisted with UUID

---

## Revised Phase Roadmap

The original V1_DESIGN.md Phase 7 was a 4-day estimate for read-only MCP. During planning, we reviewed a much larger strategic vision document (see `docs/qa/phase7_review_notes.md`) that proposed bundling security hardening, weekly review migration, write tools, extensibility hooks, and multi-user support into one mega-phase.

After architecture review, we rejected the mega-phase and trimmed to the original spirit: **ship fast, iterate based on real usage.** The revised roadmap:

| Phase | Name | Focus | Status |
|-------|------|-------|--------|
| **7** | MCP Core + Read Tools | SSE server, 15 read tools, basic auth | **Done** |
| **7.5** | Weekly Review Migration | Weekly review on Claude.ai, Telegram notification-only | Next — after 1-2 weeks real MCP usage |
| **8** | Heartbeat + Security Hardening | Unified scheduler, RLS, token health, processing runs | After 7.5 |
| **9** | Write Tools + Expansion | Task CRUD, Gantt management, role permissions as needed | After 8 |

### Key Decisions Behind This Split

1. **Single-user auth is enough for now.** Eyal is the only MCP user. Per-user tokens and role-based permissions are deferred to Phase 9 (when a second user actually needs MCP). Adding a second token is an afternoon's work, not a sub-phase.

2. **No extensibility framework.** The existing pattern (`services/*.py`) IS the extensibility model. A formal tool registry with `connect()` / `health_check()` / `list_capabilities()` is premature — build it when the second integration exists, not before.

3. **No RLS yet.** Supabase RLS is meaningful in a multi-tenant threat model. A single-workspace system behind a bearer token doesn't need it yet. Deferred to Phase 8.

4. **No stdio transport.** SSE for Claude.ai is the only transport needed now. The thin-wrapper architecture already guarantees transport-agnosticism — adding stdio later is a config change, not a redesign.

5. **Weekly review is NOT a 7-step wizard.** The original plan proposed expanding the Telegram 3-part review to a 7-part MCP wizard. Rejected — MCP tools are callable on-demand, Claude adapts the flow naturally. Start minimal, let Eyal's usage patterns decide what needs formal steps.

6. **Processing runs table deferred.** Useful for QC tracing but not blocking. Phase 8.

7. **Sensitivity enforcement at DB level deferred.** Currently prompt-only. Phase 8 with RLS.

### What Phase 7.5 Will Ship

- Weekly review data served via MCP tools (already wired — `get_weekly_summary()`)
- Gantt proposal approval through MCP (the ONE write operation needed for review)
- Report/slide delivery through MCP response
- Telegram becomes notification-only: "Your review data is ready"
- Depends on: Phase 7 deployed + 1-2 weeks of real usage

### What Phase 8 Will Ship

- Unified heartbeat scheduler (original V1_DESIGN.md Phase 8)
- Supabase RLS activation
- OAuth token health monitoring
- Processing runs table (QC tracing)
- Integration testing

### What Phase 9 Will Ship

- Full write MCP tools (task CRUD, Gantt management, quick inject)
- Role-based permissions (if/when second user needs MCP)
- Whatever real usage of Phases 7-8 reveals is needed

---

## Configuration

To enable MCP, add to `.env`:

```
MCP_AUTH_TOKEN=<random-secret-token>
MCP_RATE_LIMIT_PER_HOUR=100  # optional, default 100
```

When `MCP_AUTH_TOKEN` is set, the MCP Starlette server starts on PORT (8080) instead of the aiohttp health server. All health/ready/report routes continue working on the same port.

---

*This document is the authoritative reference for Phase 7 decisions. Read `docs/qa/phase7_review_notes.md` for the architecture review that shaped these decisions.*
