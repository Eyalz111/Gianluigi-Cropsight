# CLAUDE.md — Gianluigi Project Context

**Last Updated:** March 21, 2026
**Current Version:** v1.0 (Phases 0-7 complete, MCP server live)
**Status:** v1.0 Phases 0-7 complete, Phase 7.5 (weekly review migration) next

---

## What Is This Project

Gianluigi is CropSight's AI operations assistant — an "AI Office Manager" for a 4-person AgTech founding team. It processes meeting transcripts, tracks tasks/decisions/commitments, maintains institutional memory, and is evolving into a full operational intelligence system that manages the company's operational Gantt chart, email intelligence, and serves as the CEO's private operations dashboard.

**CropSight:** Israeli AgTech startup — ML-powered crop yield forecasting. Pre-revenue, PoC stage. Team: Eyal (CEO), Roye (CTO), Paolo (BD, Italy), Prof. Yoram Weiss (Advisor).

---

## Current State (Post Phase 6)

- 1348 tests, all passing (1288 existing + 60 MCP)
- Deployed to Cloud Run (europe-west1, 512Mi, min-instances=1)
- Live tested with real meetings and team interactions
- DB freshly rebuilt Mar 13, 2026 (Phase 5 migration Mar 18, Phase 6 migration Mar 18)

### What Works
- Full transcript pipeline: Tactiq → Drive → Claude extraction → Supabase → approval → distribution
- Hybrid RAG (semantic + full-text, RRF fusion, time-weighted, parent chunks, source weights)
- Telegram bot with commands, Q&A, approval flow, /status command
- Gmail send/receive, Google Drive watchers, Calendar reading
- Task deduplication, status inference, open question resolution
- Entity registry, commitment tracking, proactive alerts
- Meeting prep generation, weekly digest (Friday), pre-meeting reminders
- Word document summaries, Google Sheets integration
- Cost optimization (tiered models: Opus/Sonnet/Haiku, prompt caching)
- 5-layer inbound security
- **v1.0 Phase 1:** Multi-agent pattern (Router/Conversation/Analyst/Operator)
- **v1.0 Phase 2:** Bidirectional Gantt integration (read/write/rollback/backup)
- **v1.0 Phase 3:** End-of-day debrief (quick injection + full interactive sessions)
- **v1.0 Phase 4:** Email intelligence (personal Gmail scan, morning brief, email classifier)
- **Architecture Review:** Approval reminders, expiry, health monitoring, RAG source weights, session locking
- **v1.0 Phase 5:** Meeting prep redesign — propose-discuss-generate pipeline, template-driven prep, meeting type classifier, Telegram inline outline flow, timeline modes, restart-safe state, .docx generation, sensitivity-aware distribution
- **v1.0 Phase 6:** Weekly review + outputs — interactive 3-part session (stats → decisions → outputs), HTML report with per-report tokens, Gantt proposal distribution, session corrections with Haiku/Sonnet fallback, digest/review scheduler coexistence, 48h session expiry, debrief interruption support
- **v1.0 Phase 7:** MCP Core + Read Tools — FastMCP SSE server on port 8080, 15 read-only tools (thin wrappers around existing brain functions), bearer token auth, rate limiting (100/hr), audit logging, `get_system_context()` onboarding tool, session save/load, health/ready/report routes on same port

### Known Issues
- Email dedup edge cases: forwarded threads may not deduplicate perfectly at low volume
- Some schedulers disabled by default (morning brief, email scan, debrief prompt)
- Transcript watcher disabled by default (TRANSCRIPT_WATCHER_ENABLED=false) — enable for live testing
- See KNOWN_ISSUES.md for full list

---

## v1.0 — "The AI Office Manager" (In Progress)

**Design document:** `V1_DESIGN.md` (comprehensive spec, READ THIS FIRST for any v1.0 work)
**Architecture review:** `docs/qa/ARCHITECTURE_REVIEW_ISSUES.md` (12 issues identified, most addressed)
**Phase 5 architecture:** `docs/system_architecture_v1_phase5.md` (post-Phase 5 system visualization)
**Phase 6 architecture:** `docs/system_architecture_v1_phase6.md` (post-Phase 6 system visualization)

### Completed Phases
- **Phase 0:** Database migration, new models
- **Phase 1:** Multi-agent foundation
- **Phase 2:** Gantt integration
- **Phase 3:** Debrief flow
- **Phase 4:** Email intelligence
- **Post-Phase 4:** Architecture review fixes (approval expiry, health monitoring, RAG weights, session locking)
- **Phase 5:** Meeting prep redesign (propose-discuss-generate, templates, type classifier, timeline modes)
- **Phase 6:** Weekly review + outputs (3-part interactive session, HTML reports, Gantt distribution, live QA fixes)
- **Phase 7:** MCP Core + Read Tools (SSE server, 15 read tools, auth, rate limiting, audit logging)

### Remaining Phases
- **Phase 7.5:** Weekly review migration (weekly review via Claude.ai, Telegram notification-only)
- **Phase 8:** Heartbeat unification + security hardening (includes OAuth for MCP, data boundary enforcement)
- **Phase 9:** Write tools + expansion

### Known MCP Limitation: Personal Data Leakage
Claude.ai mixes MCP tool results with its own conversation memory. MCP `instructions` are guidance, not a sandbox — Claude.ai can and will use prior conversation context when Gianluigi data is sparse. **Current mitigation:** Use a dedicated Claude Project ("CropSight Ops") to isolate business conversations. **Future:** OAuth integration (Phase 8) may enable stricter session isolation. This is a Claude.ai platform limitation, not a Gianluigi bug.

### What's NOT Changing
- Supabase (EU region) as primary database
- Telegram as primary daily interaction channel
- Google Workspace (Drive, Sheets, Calendar, Gmail) integrations
- Professional tone guardrails and sensitivity classification
- CEO-approval-first pattern for all team distributions
- Tactiq for meeting transcription
- Tiered model strategy (Opus/Sonnet/Haiku)
- Cloud Run hosting

---

## Important Design Principles

- **Gianluigi proposes, Eyal approves.** Never write to Gantt, distribute to team, or make structural changes without explicit CEO approval.
- **All team interactions go through Eyal.** No direct nudging of team members. Only approved distributions.
- **Brain is interface-agnostic.** Capabilities are Python functions. Telegram and MCP are interfaces.
- **Free-text resilience.** No rigid command formats. Understand natural language, typos, abbreviations.
- **Confirm before action.** Any write operation from ambiguous input must be confirmed first.
- **Source citations.** Every extracted item references its source.
- **Sensitivity follows data.** Tags applied at ingestion, follow through to outputs.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Claude API (Opus/Sonnet/Haiku) via Anthropic SDK |
| Database | Supabase (PostgreSQL + pgvector, EU region Frankfurt) |
| Embeddings | OpenAI text-embedding-3-small (1536d) |
| Chat | Telegram Bot (python-telegram-bot) |
| Email | Gmail API (gianluigi.cropsight@gmail.com) |
| Files | Google Drive API |
| Tasks/Gantt | Google Sheets API |
| Calendar | Google Calendar API (read-only, authenticated as Eyal via per-user OAuth token) |
| Hosting | Google Cloud Run (europe-west1) |
| Transcription | Tactiq (Chrome extension) |
| CEO Interface | Claude.ai via MCP server (SSE transport, FastMCP SDK) |
| MCP Server | `mcp` Python SDK + uvicorn, SSE on port 8080 |
| Language | Python 3.11+, async |

---

## Supabase Notes
- All methods are **SYNC** (never await them)
- Uses PostgREST API via supabase-py
- pgvector for semantic search, tsvector for full-text
- v1.0 tables: gantt_schema, gantt_proposals, gantt_snapshots, debrief_sessions, email_scans, mcp_sessions, weekly_reports (+ html_content, access_token, expires_at), weekly_review_sessions, meeting_prep_history (+ outline_content, focus_instructions, timeline_mode), pending_approvals (with expires_at), calendar_classifications (+ meeting_type), meetings (+ meeting_type)

## LLM Notes
- **Opus:** Transcript extraction, document analysis (accuracy-critical) — Analyst Agent
- **Sonnet:** Conversations, tool use, Gantt operations — Conversation + Operator Agents
- **Haiku:** Classification, intent routing, outline agenda generation, focus classification — Router Agent
- Prompt caching via `cache_control: {"type": "ephemeral"}` on system prompts
- All calls go through `core/llm.py` centralized helper

## Calendar Architecture
- Gianluigi reads Eyal's calendar using **Eyal's OAuth token** (`EYAL_CALENDAR_REFRESH_TOKEN`), not a shared calendar
- This lets us see Eyal's event colors (purple = CropSight), declined status, etc.
- Token obtained via `python scripts/get_calendar_token.py` (calendar.readonly scope)
- Falls back to Gianluigi's token if Eyal's not set (but colors won't be visible)
- **Future (Phase B):** When CropSight moves to Google Workspace, replace per-user tokens with service account + domain-wide delegation

## Important IDs
- Eyal Telegram DM: `8190904141`
- Group chat: `-5187389631`
- Calendar color `3` (purple = CropSight)

---

## Files to Read for Context
1. `V1_DESIGN.md` — Full v1.0 specification (START HERE for new features)
2. `config/settings.py` — All environment variables and configuration
3. `config/team.py` — Team emails, filter keywords, blocklists
4. `core/system_prompt.py` — Gianluigi's personality and guardrails
5. `models/schemas.py` — All Pydantic data models
6. `KNOWN_ISSUES.md` — Bugs from live testing
7. `docs/qa/ARCHITECTURE_REVIEW_ISSUES.md` — Architecture review findings
8. `docs/system_architecture_v1_phase5.md` — Post-Phase 5 system architecture
9. `docs/system_architecture_v1_phase6.md` — Post-Phase 6 system architecture
10. `config/meeting_prep_templates.py` — Meeting prep template definitions
11. `services/mcp_server.py` — MCP server with 15 read-only tools
12. `guardrails/mcp_auth.py` — MCP bearer token auth, rate limiting, audit logging
