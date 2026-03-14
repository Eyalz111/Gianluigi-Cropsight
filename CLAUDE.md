# CLAUDE.md — Gianluigi Project Context

**Last Updated:** March 14, 2026
**Current Version:** v0.5 (tagged as v0.5-stable) → Building v1.0
**Status:** v0.5 live on Cloud Run, v1.0 design approved, implementation starting

---

## What Is This Project

Gianluigi is CropSight's AI operations assistant — an "AI Office Manager" for a 4-person AgTech founding team. It processes meeting transcripts, tracks tasks/decisions/commitments, maintains institutional memory, and is evolving into a full operational intelligence system that manages the company's operational Gantt chart, email intelligence, and serves as the CEO's private operations dashboard.

**CropSight:** Israeli AgTech startup — ML-powered crop yield forecasting. Pre-revenue, PoC stage. Team: Eyal (CEO), Roye (CTO), Paolo (BD, Italy), Prof. Yoram Weiss (Advisor).

---

## Current State (v0.5)

- 579 tests, all passing
- Deployed to Cloud Run (europe-west1, 512Mi, min-instances=1)
- Live tested with real meetings and team interactions
- DB freshly rebuilt Mar 13, 2026

### What Works
- Full transcript pipeline: Tactiq → Drive → Claude extraction → Supabase → approval → distribution
- Hybrid RAG (semantic + full-text, RRF fusion, time-weighted, parent chunks)
- Telegram bot with commands, Q&A, approval flow
- Gmail send/receive, Google Drive watchers, Calendar reading
- Task deduplication, status inference, open question resolution
- Entity registry, commitment tracking, proactive alerts
- Meeting prep generation, weekly digest, pre-meeting reminders
- Word document summaries, Google Sheets integration
- Cost optimization (tiered models: Opus/Sonnet/Haiku, prompt caching)
- 5-layer inbound security

### Known Issues (v0.5)
- Meeting prep quality: too much noise, wrong context for meeting type, timing issues
- Multi-turn conversation memory: data handling errors, formatting drift in long dialogues
- See KNOWN_ISSUES.md for full list from live testing

---

## v1.0 — "The AI Office Manager" (In Progress)

**Design document:** `V1_DESIGN.md` (comprehensive spec, READ THIS FIRST for any v1.0 work)

### Key Architecture Changes

1. **Multi-Agent Pattern:** Router (Haiku) → Conversation Agent (Sonnet) → Analyst Agent (Opus) → Operator Agent (Sonnet). Plain Python + Anthropic SDK, no CrewAI.

2. **Bidirectional Gantt Integration:** Read/write the operational Gantt (Google Sheets) with schema awareness, versioning, rollback, and approval flow.

3. **Email Intelligence:** Enhanced inbox monitoring + daily filtered scan of CEO's personal Gmail.

4. **End-of-Day Debrief:** Interactive Telegram conversation with structured session state, follow-up questions, and verified extraction.

5. **Weekly Review Session:** Calendar-driven CEO review via Claude.ai (MCP) or Telegram — Gantt updates, task review, slide generation, HTML report.

6. **MCP Server:** Claude.ai interface for structured work sessions. Read tools first, write tools after stability.

7. **Unified Heartbeat:** Single scheduler with multiple rhythms (pulse/5min, morning, evening, weekly_prep, weekly_post, alert).

8. **RAG Upgrades:** New source types (email, debrief, gantt_change), source priority ranking, embedding lifecycle, conflict detection.

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
| Calendar | Google Calendar API (read-only) |
| Hosting | Google Cloud Run (europe-west1) |
| Transcription | Tactiq (Chrome extension) |
| CEO Interface | Claude.ai via MCP server |
| Language | Python 3.11+, async |

---

## Supabase Notes
- All methods are **SYNC** (never await them)
- Uses PostgREST API via supabase-py
- pgvector for semantic search, tsvector for full-text
- New v1.0 tables: gantt_schema, gantt_proposals, gantt_snapshots, debrief_sessions, email_scans, mcp_sessions, weekly_reports, meeting_prep_history

## LLM Notes
- **Opus:** Transcript extraction, document analysis (accuracy-critical) — Analyst Agent
- **Sonnet:** Conversations, tool use, Gantt operations — Conversation + Operator Agents
- **Haiku:** Classification, intent routing — Router Agent
- Prompt caching via `cache_control: {"type": "ephemeral"}` on system prompts
- All calls go through `core/llm.py` centralized helper

## Important IDs
- Eyal Telegram DM: `8190904141`
- Group chat: `-5187389631`
- Calendar color `3` (purple = CropSight)

---

## Build Sequence (v1.0)
See V1_DESIGN.md Section 12 for detailed phased plan.
Summary: Pre-work → Multi-agent foundation → Gantt integration → Debrief flow → Email intelligence → Meeting prep redesign → Weekly review + outputs → MCP server → Heartbeat unification → Integration testing

---

## Files to Read for Context
1. `V1_DESIGN.md` — Full v1.0 specification (START HERE for new features)
2. `config/settings.py` — All environment variables and configuration
3. `config/team.py` — Team emails, filter keywords, blocklists
4. `core/system_prompt.py` — Gianluigi's personality and guardrails
5. `models/schemas.py` — All Pydantic data models
6. `KNOWN_ISSUES.md` — Bugs from v0.5 live testing
