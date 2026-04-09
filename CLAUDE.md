# CLAUDE.md — Gianluigi Project Context

**Last Updated:** April 9, 2026
**Current Version:** v2.2 (Phases 0-13 + X1/X2 + Intelligence Signal + Deal Intelligence + CEO UX + Approval Flow Robustness Tiers 1-3, 43 MCP tools)
**Status:** Tier 3 approval-flow architectural robustness complete. Production revision `gianluigi-00061-vsk`. Smoke-tested live (reject path) 2026-04-09.

---

## What Is This Project

Gianluigi is CropSight's AI operations assistant — an "AI Office Manager" for a 4-person AgTech founding team. It processes meeting transcripts, tracks tasks/decisions with cross-meeting topic threading and continuity intelligence, maintains institutional memory via hybrid RAG + operational snapshots, generates weekly market intelligence (Intelligence Signal), and serves as the CEO's private operations dashboard via Claude.ai MCP (43 tools).

**CropSight:** Israeli AgTech startup — ML-powered crop yield forecasting. Pre-revenue, PoC stage. Team: Eyal (CEO), Roye (CTO), Paolo (BD, Italy), Prof. Yoram Weiss (Advisor).

---

## Current State (Post Tier 3)

- ~1950 tests, all new tests passing (22 pre-existing failures baselined in `tier3_handoff.md`)
- MCP server with 43 tools, connected to Claude.ai via CropSight Ops project
- Full cycle verified live: transcript → extraction (pending) → approve → distribution → MCP query, and reject → tombstone → cascade-clear
- Meeting continuity engine: cross-meeting context, task match annotations, decision chains
- Daily QA agent: extraction quality, distribution completeness, scheduler health, data integrity, RLS coverage, rejected orphans, **approved-with-pending-children safety net (T3.1)**
- Document versioning with content hash dedup, Dropbox sync ready (disabled, needs credentials)
- Phase 11-13 migrations applied (sensitivity, email body, decision freshness, task signals, doc versioning)
- **Approval flow robustness** (Tiers 1+2+T1.9+3) complete: cascading reject + tombstones + FK CASCADE + `approval_status` gating + Gmail/Telegram retry + sheet format on approval
- Production revision: `gianluigi-00061-vsk` (as of 2026-04-09)

### What Works
- Full transcript pipeline: Tactiq → Drive → Claude extraction → Supabase → approval → distribution
- Hybrid RAG (semantic + full-text, RRF fusion, time-weighted, parent chunks, source weights)
- Telegram bot with commands, Q&A, approval flow, /status command
- Gmail send/receive, Google Drive watchers, Calendar reading
- Task deduplication, status inference, open question resolution
- Entity registry, proactive alerts, system failure alerts (CRITICAL → Telegram DM, WARNING → batched)
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
- **v1.0 Phase 7:** MCP Core + Read Tools — FastMCP SSE server on port 8080, 16 tools (15 read + `get_full_status()` composite), bearer token auth, rate limiting (100/hr), audit logging, `get_system_context()` onboarding tool, session save/load, health/ready/report routes on same port
- **QA Hardening:** 16 issues fixed — commitments deprecated (unified into action items), extraction prompt improved (deadline-only-if-explicit, consolidation 3-7 items), decisions exported to Sheets, summary teaser distribution, all schedulers Israel timezone, system failure alerts (`services/alerting.py`), MCP `_success(warnings=...)` pattern, stakeholder tab fix, silent logging fixes
- **v2 Phase 11 (Operational Maturity):** Distribution pre-edit fix, time-window filters, morning brief direct send, evening debrief prompt, sensitivity LLM + propagation, Sheets on-demand sync, Telegram multi-part fix
- **v2 Phase 12 (Meeting Continuity):** Enhanced context gatherer, continuity-aware extraction (existing_task_match), decision freshness, task signal detection, decision chain traversal
- **v2 Phase 13 (Data Ingestion):** Email body storage, attachment Drive persistence, document versioning + content hash dedup, Dropbox sync (disabled), CropSight document types
- **X1:** Daily QA Agent (extraction quality, distribution, scheduler health, data integrity)
- **X2:** Skills manifest (17 capabilities documented in docs/SKILLS.md)
- **Intelligence Signal (v2.1):** Weekly market intelligence — Perplexity research → Opus synthesis → .docx report → video (MoviePy + ElevenLabs v3 TTS) → email distribution with attachments. 5 new MCP tools (38→43), competitor watchlist auto-curation, Thursday 18:00 IST scheduler.
- **v2.2 Session 1:** Broken pipe retry fix (3 attempts, exponential backoff), 15-min transcript watcher interval, YAML prompt library (`config/prompts/`) with hot-reload.
- **v2.2 Session 2:** Sensitivity tiers redesigned (FOUNDERS/CEO/TEAM/PUBLIC), retrieval-level filtering, interpersonal signal extraction, Telegram task replies via inline buttons.
- **v2.2 Session 2.5:** Sensitivity tier rename (TEAM→FOUNDERS, CEO_ONLY→CEO), retrieval-level filtering across all processors.
- **v2.2 Session 3 — Deal Intelligence (Phase 4):** 3 new DB tables (deals, deal_interactions, external_commitments), `deal_ops` MCP composite tool (9 actions, replaces deprecated `get_commitments`), deal signal detection from transcripts, Deal Pulse + Commitments Due in morning brief, stakeholder sheet +3 columns (Deal Stage, Deal Value, Last Interaction), zero-friction auto-interaction from meetings/emails.
- **v2.2 Session 3 — CEO UX (Phase 5):** `get_full_status(view="ceo_today")` CEO dashboard (overdue tasks, this week, milestones, deal pulse, drift alerts), Gantt drift detection (>50% overdue = drift alert), morning brief enhancements (Task Urgency, Gantt Milestones, Drift Alerts sections).
- **Approval Flow Robustness Tier 1 (2026-04-07):** Cascading reject via `delete_meeting_cascade` + unified Telegram→DB writer + cleanup script + watcher rejection awareness. Closes the "rejected meetings leave orphan children" leak.
- **Approval Flow Robustness Tier 2 (2026-04-07):** `/status` dashboard + morning brief "System State" section + QA scheduler `_check_rejected_meetings` defense-in-depth check.
- **Approval Flow Robustness T1.9 pivot (2026-04-09):** Reject uses DB tombstones (`keep_tombstone=True`) instead of hard delete — the `meetings` row is preserved with `approval_status='rejected'` so the watcher can skip re-processing the same source file. HTTP 403 on user-owned Drive files made the original Drive-move approach unworkable.
- **Approval Flow Robustness Tier 3 (2026-04-09):** Architectural robustness + known-issue fixes.
  - **T3.1 narrow** — `approval_status` column on tasks/decisions/open_questions/follow_up_meetings with CHECK constraint and partial indexes. The 4 central read helpers (`get_tasks`, `list_decisions`, `get_open_questions`, `list_follow_up_meetings`) now filter to approved-only by default (`include_pending=False`). Extraction writes rows as 'pending' via DB default; approve flow promotes them via `_promote_children_to_approved()` with 3-attempt retry.
  - **T3.2 FK CASCADE** — every meeting child table now has `ON DELETE CASCADE` (discovered live-DB schema drift: tasks/decisions/open_questions had been `NO ACTION` in production despite `setup_supabase.sql` claiming `CASCADE`). `delete_meeting_cascade(keep_tombstone=False)` simplified to single DB delete + embeddings (polymorphic) cleanup.
  - **T3.3 cleanup + safety net** — fixed latent bug in `scripts/cleanup_rejected_meetings.py` that was destroying tombstones by calling `delete_meeting_cascade()` without `keep_tombstone=True`. Added QA scheduler check `_check_approved_meetings_with_pending_children` that catches any silent `_promote_children_to_approved` failures (parent='approved' + child='pending' = invisible data hole). Runs daily, surfaces in morning brief.
  - **T3.4 retry on Gmail/Telegram sends** — extracted network calls into `_execute_send` (gmail) and `_bot_send_message` (telegram_bot) wrapped with `@retry` from `core/retry.py`. 3 attempts, exponential backoff. Fixes BrokenPipe silent-drops observed during test 4.
  - **T3.5 `format_task_tracker()` on approval** — `distribute_approved_content()` now calls `format_task_tracker()` after the tasks append loop, so new rows don't inherit header-bleed styling.
  - **T3.6 known limitation doc** — `KNOWN_ISSUES.md` now documents the `source_file_path` ILIKE substring match collision risk for tombstone matching (rare because Tactiq filenames are timestamp-prefixed).

### Known Issues
- Email dedup edge cases: forwarded threads may not deduplicate perfectly at low volume
- Transcript watcher disabled by default (TRANSCRIPT_WATCHER_ENABLED=false)
- Dropbox sync needs SDK + credentials before enabling
- See KNOWN_ISSUES.md for full list

---

## v1.0 — "The AI Office Manager" (Complete)

**Design document:** `V1_DESIGN.md` (comprehensive spec for v1.0 phases)
**v2 implementation plan:** `.claude/plans/keen-strolling-pnueli.md` (Phases 11-13 + X1/X2)
**Architecture review:** `docs/qa/gianluigi_v2_architecture_review.md` (v2 concerns, addressed)
**Skills manifest:** `docs/SKILLS.md` (17 capabilities with triggers, inputs, outputs, costs)
**Phase 6 architecture:** `docs/system_architecture_v1_phase6.md` (visual, pre-v2 — needs update)

### Completed Phases
- **Phase 0:** Database migration, new models
- **Phase 1:** Multi-agent foundation
- **Phase 2:** Gantt integration
- **Phase 3:** Debrief flow
- **Phase 4:** Email intelligence
- **Post-Phase 4:** Architecture review fixes (approval expiry, health monitoring, RAG weights, session locking)
- **Phase 5:** Meeting prep redesign (propose-discuss-generate, templates, type classifier, timeline modes)
- **Phase 6:** Weekly review + outputs (3-part interactive session, HTML reports, Gantt distribution, live QA fixes)
- **Phase 7:** MCP Core + Read Tools (SSE server, 16 tools, auth, rate limiting, audit logging)
- **QA Hardening:** 16 issues fixed (commitments deprecated, extraction improved, alerting, timezone, decisions export, MCP composite tool)

- **Phase 7.5:** Weekly review migration (weekly review via Claude.ai MCP, Telegram redirect)
- **Phase 8a:** Extraction intelligence (task continuity, team roles, escalation, Hebrew) + MCP write tools (task CRUD, quick inject, Gantt propose)
- **Phase 8b:** Health monitoring (scheduler heartbeats), cost monitoring, tsvector Hebrew fix, index audit
- **Phase 9A:** Decision intelligence (rationale, confidence, review triggers, supersession detection), canonical project labels, task archival
- **Phase 9B:** Cross-meeting memory — meeting-to-meeting continuity, compressed operational snapshots, topic threading (4 MCP tools)
- **Phase 9C:** Gantt intelligence (velocity, slippage, milestone risk, Now-Next-Later), follow-up tracking
- **Phase 9D:** Tool grouping (category prefixes on 33 tools), weekly review integration, Word doc labels

- **Phase 10:** Polish & Ship — Sheets redesign (TASK_COLUMNS/DECISION_COLUMNS constants, column reorder, rebuild functions), dynamic canonical projects (DB table + 2 MCP tools), Claude.ai project prompt (35 tools documented), deprecated commitment code removed, data validation removed, smoke test transcript

- **Phase 11 (v2 Workstream C — Operational Maturity):** Distribution pre-edit fix, time-window filters (enabled alert+reminder schedulers), morning brief direct send, evening debrief prompt, watcher intervals, sensitivity LLM classification + propagation, Sheets on-demand sync, Telegram multi-part fix
- **Phase 12 (v2 Workstream A — Meeting Continuity):** Enhanced context gatherer (daily + pre-meeting), continuity-aware extraction with existing_task_match annotations, decision freshness tracking (touch + stale surfacing), task signal detection (email/Gantt/calendar), decision chain traversal + MCP tool
- **Phase 13 (v2 Workstream B — Data Ingestion):** Full email body storage, email attachment Drive persistence, document versioning (title+source + content hash dedup), CropSight document types, Dropbox → Drive sync (disabled, needs credentials)
- **X1:** Daily QA Agent — extraction quality, distribution completeness, scheduler health, data integrity checks. Runs 06:00 IST, feeds morning brief, on-demand MCP tool
- **X2:** Skills manifest (`docs/SKILLS.md`) — 17 capabilities documented

- **v2.2 Session 1:** Broken pipe retry, 15-min watcher, YAML prompt library
- **v2.2 Session 2:** Sensitivity tiers (4-level), interpersonal signals, Telegram task replies
- **v2.2 Session 2.5:** Sensitivity tier rename (FOUNDERS/CEO), retrieval-level filtering
- **v2.2 Session 3 Phase 4:** Deal Intelligence — 3 tables, `deal_ops` MCP tool (replaces `get_commitments`), deal signal detection, Deal Pulse + Commitments Due in morning brief, stakeholder sheet expansion
- **v2.2 Session 3 Phase 5:** CEO UX — `ceo_today` view on `get_full_status`, Gantt drift detection, Task Urgency/Milestones/Drift in morning brief

### Deferred (Beyond v2.2)
- Risk register, meeting effectiveness scoring, OKR layer, full Sheets bidirectional sync

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
| CEO Interface | Claude.ai via MCP server (Streamable HTTP, FastMCP SDK) |
| MCP Server | `mcp` Python SDK + uvicorn, 43 tools on port 8080 |
| Video | MoviePy + PIL + matplotlib + ffmpeg (2-pass encoding) |
| TTS | ElevenLabs v3 API (per-segment narration) |
| Research | Perplexity Sonar Pro API (web search with citations) |
| Language | Python 3.11+, async |

---

## Supabase Notes
- All methods are **SYNC** (never await them)
- Uses PostgREST API via supabase-py
- pgvector for semantic search, tsvector for full-text
- v1.0 tables: gantt_schema, gantt_proposals, gantt_snapshots, debrief_sessions, email_scans, mcp_sessions, weekly_reports (+ html_content, access_token, expires_at), weekly_review_sessions, meeting_prep_history (+ outline_content, focus_instructions, timeline_mode), pending_approvals (with expires_at), calendar_classifications (+ meeting_type), meetings (+ meeting_type)

## MANDATORY: Row Level Security on every new table
Every `CREATE TABLE` statement in a migration SQL file MUST be followed by
`ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;`. Without this, Supabase flags
the table as publicly accessible via the anon key (security vulnerability).

Gianluigi uses the service_role key, which bypasses RLS automatically — so
enabling RLS has zero functional impact, it only locks out the anon/public
path.

Enforcement layers:
1. **Test:** `tests/test_rls_coverage.py` queries Supabase and fails pytest
   if any public table is missing RLS.
2. **Runtime:** `schedulers/qa_scheduler._check_rls_coverage()` runs daily
   and fires a CRITICAL alert in the morning brief + `/status` if anything
   slipped through.
3. **Template:** copy the pattern from `scripts/migrate_rls_security_v2.sql`
   (bottom of file has a commented template).

Both layers 1 and 2 depend on `public.get_table_rls_status()` being installed
— it's created by `scripts/migrate_rls_security_v2.sql`.

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
11. `services/mcp_server.py` — MCP server with 38 tools (read + write)
12. `guardrails/mcp_auth.py` — MCP bearer token auth, rate limiting, audit logging
13. `docs/SKILLS.md` — All 17 system capabilities documented
14. `processors/meeting_continuity.py` — Cross-meeting context (Phase 12)
15. `schedulers/qa_scheduler.py` — Daily QA agent (X1)
