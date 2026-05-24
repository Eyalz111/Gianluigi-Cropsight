# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Gianluigi — CropSight's AI operations assistant ("AI Office Manager") for a 4-person AgTech founding team. Ingests meeting transcripts (Tactiq → Drive), extracts tasks/decisions/open questions, threads them across meetings, and serves the CEO (Eyal) via Telegram and Claude.ai MCP (45 tools).

CropSight: Israeli AgTech startup, ML crop-yield forecasting, pre-revenue. Team: Eyal (CEO), Roye (CTO), Paolo (BD), Yoram (Advisor).

For recent change history, use `git log` and the `KNOWN_ISSUES.md` file — they're authoritative. This file is forward-looking guidance, not a changelog.

## Commands

```bash
# Run locally (Python 3.11+ required, enforced in main.py)
python main.py
python main.py --debug

# Tests — pytest with asyncio_mode=auto. Anything async runs without explicit @pytest.mark.asyncio.
python -m pytest                                  # full suite (~2000 tests, ~21 pre-existing failures baselined)
python -m pytest tests/test_cross_reference.py    # single file
python -m pytest tests/test_x.py::test_specific   # single test
python -m pytest -k "approval and not slow"       # by keyword
python -m pytest --lf                             # only last failed

# Deploy (used in production — Cloud Run builds from source, secrets persist across deploys)
gcloud run deploy gianluigi --source . --region europe-west1 --memory 1Gi \
  --min-instances 1 --max-instances 1 --cpu 1 --no-cpu-throttling \
  --timeout 3600 --allow-unauthenticated

# Logs / status
gcloud run logs read --service=gianluigi --region=europe-west1
curl https://<service-url>/health
```

There is no separate lint step configured. `scripts/qa_pre_deploy.py` is the pre-deploy sanity check.

## Architecture

**Entry point: `main.py`.** It boots a single asyncio event loop that:
1. Starts an HTTP server on `$PORT` (Cloud Run liveness probe). If `MCP_AUTH_TOKEN` is set, `services/mcp_server.py` provides both MCP **and** `/health`/`/ready`/`/report` routes; otherwise `services/health_server.py` serves the health routes alone. Both expose `set_ready()` — readiness only flips true after all services init.
2. Initializes services (`services/{supabase_client,google_drive,google_calendar,google_sheets,gmail,embeddings}.py`). Only Supabase is critical; the rest degrade gracefully (matching schedulers skip).
3. Spawns ~14 background scheduler tasks. Most are gated by an `*_ENABLED` flag in `config/settings.py` and/or by whether their dependency service initialized. `TRANSCRIPT_WATCHER_ENABLED=false` by default — flip in env to turn on.
4. Reconstructs persistent state from DB: auto-publish timers, meeting-prep timers, interactive Telegram session stack. **Restart-safety is a property of the system** — anything mid-flow when Cloud Run cycles must be replayable from DB rows alone.

**Layer separation (the "brain is interface-agnostic" rule):**
- `core/` — LLM helper (`llm.py`, the **only** place that calls Anthropic), system prompt, agents (Router/Conversation/Analyst/Operator), retry, cost calc, health monitor.
- `processors/` — Pure business logic. Transcript extraction, cross-reference dedup, meeting continuity, topic threading, debrief, weekly review, morning brief, deal intelligence, etc. **No I/O surface** — these get called by schedulers/services and return data.
- `services/` — External integrations only. Supabase, Google APIs, Telegram, MCP server, embeddings, video assembler, ElevenLabs, Perplexity.
- `schedulers/` — Loops + cron-like triggers. Each is `start()/stop()`-able and emits a heartbeat consumed by `core/health_monitor.py`.
- `guardrails/` — Approval flow, sensitivity classifier, content filter, MCP auth (bearer token + rate limit + audit log), Gantt validation.
- `config/` — Settings (`settings.py`), team config, prompt YAML library, escalation rules.
- `models/schemas.py` — All Pydantic models.

**Prompts live in `config/prompts/*.yaml`** and are hot-reloaded through `config/prompt_registry.py`. To edit a prompt for production, edit the YAML — code references like `core/system_prompt.py` are wrappers. YAML single-quote escape gotcha: `'don''t'` (doubled single-quote).

**Approval flow is the central control point.** Extraction writes tasks/decisions/open_questions/follow_up_meetings with `approval_status='pending'`. The 4 central read helpers in `services/supabase_client.py` (`get_tasks`, `list_decisions`, `get_open_questions`, `list_follow_up_meetings`) filter to approved-only by default (`include_pending=False`). Approve → `_promote_children_to_approved()` flips them all. Reject → `delete_meeting_cascade(keep_tombstone=True)` preserves the `meetings` row as a `rejected` tombstone so the watcher won't reprocess the source file. FK CASCADE on every child table handles cleanup.

## Supabase / DB

- **All `supabase_client` methods are SYNC.** Never `await` them — `await supabase_client.get_tasks()` is a bug. PostgREST via supabase-py, service-role key bypasses RLS.
- pgvector for semantic search, tsvector for full-text. Hybrid RAG (RRF fusion + time decay + source weights) lives in `processors/` (search helpers) and the `match_embeddings` RPC.
- `meeting_id` FK CASCADE is real and enforced — production once drifted to `NO ACTION` despite `setup_supabase.sql` saying CASCADE, so trust the live schema, not the file.

### MANDATORY: RLS on every new table

Every `CREATE TABLE` in a migration SQL file must be immediately followed by `ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;`. Service-role key bypasses RLS so there's zero functional impact — it just closes the anon-key public-access vulnerability that Supabase flags.

Enforcement:
1. `tests/test_rls_coverage.py` fails pytest if any public table is missing RLS.
2. `schedulers/qa_scheduler._check_rls_coverage()` runs daily, fires CRITICAL alert in morning brief + `/status` if anything slipped.
3. Template at the bottom of `scripts/migrate_rls_security_v2.sql`.

Both checks depend on the `public.get_table_rls_status()` function created by `migrate_rls_security_v2.sql`.

## LLM routing

All Anthropic calls go through `core/llm.py`. Models are tiered to balance accuracy vs cost:

- **Opus** — transcript extraction, document analysis (accuracy-critical), Analyst Agent.
- **Sonnet** — conversations, tool use, Gantt operations, Conversation + Operator Agents.
- **Haiku** — classification, intent routing, focus/outline generation, Router Agent.

Prompt caching via `cache_control: {"type": "ephemeral"}` on long system prompts.

## Scheduler conventions

- All schedulers run in **Asia/Jerusalem** timezone. UTC math will silently drift the morning brief / weekly digest by hours.
- Cloud Run idle-then-wake produces stale httplib2 sockets → `[Errno 32] Broken pipe`. The Google service wrappers in `services/` wrap their API calls in `_execute_with_retry` that nulls `_service` between attempts so the transport is rebuilt. Pattern: when adding a new Google API call site, route it through the existing retry wrapper.
- Heartbeats: every scheduler ticks `core/health_monitor.heartbeat()` so `/status` and the QA scheduler can detect a dead loop.

## Calendar auth quirk

Calendar is read using **Eyal's OAuth refresh token** (`EYAL_CALENDAR_REFRESH_TOKEN`) — not a shared calendar, not a service account. This is the only way to see his event colors (color `3` = purple = CropSight) and declined-status. Token obtained via `python scripts/get_calendar_token.py` (calendar.readonly). Falls back to Gianluigi's own token, but colors disappear. When CropSight gets Google Workspace, swap to a service account with domain-wide delegation.

## Important IDs

- Eyal Telegram DM: `8190904141`
- Group chat: `-5187389631`
- Calendar color `3` = purple = CropSight

## Known limitations

- **MCP personal-data leakage.** Claude.ai mixes MCP tool results with conversation memory; MCP `instructions` are guidance, not a sandbox. Mitigate with a dedicated Claude Project ("CropSight Ops") for business work.
- **Transcript watcher off by default.** `TRANSCRIPT_WATCHER_ENABLED=false`. Document watcher takes over when the transcript watcher is off and `DOCUMENTS_FOLDER_ID` is set.
- **Dropbox sync disabled** — needs SDK + credentials.
- See `KNOWN_ISSUES.md` for the full live-issue list.

## Design principles (non-negotiable)

- **Gianluigi proposes, Eyal approves.** Never write to Gantt, distribute to the team, or make structural changes without explicit CEO approval. Proposal → approval gate → action.
- **All team interactions go through Eyal.** No direct nudging of Roye/Paolo/Yoram.
- **Brain is interface-agnostic.** Capabilities are functions in `processors/`. Telegram and MCP are thin call sites — never put business logic in either.
- **Sensitivity follows data.** LLM-classified at ingestion (FOUNDERS/CEO/TEAM/PUBLIC), propagated to children, enforced at retrieval. Adding a new processor? Carry sensitivity through.
- **Confirm before action.** Any write from ambiguous input must confirm first.
- **Source citations.** Every extracted item carries its source meeting_id.

## Files to read for context

1. `V1_DESIGN.md` — full v1.0 spec, START HERE for new features
2. `config/settings.py` — all env vars and feature flags (`*_ENABLED`)
3. `config/team.py` — team emails, filter keywords, blocklists
4. `core/system_prompt.py` + `config/prompts/system.yaml` — Gianluigi's voice + guardrails
5. `models/schemas.py` — all Pydantic data models
6. `services/mcp_server.py` — MCP server with 45 tools (read + write)
7. `guardrails/mcp_auth.py` + `guardrails/approval_flow.py` — auth and the approval/reject/cascade flow
8. `processors/transcript_processor.py` + `processors/cross_reference.py` — extraction + dedup
9. `processors/meeting_continuity.py` + `processors/topic_threading.py` — cross-meeting memory
10. `schedulers/qa_scheduler.py` — daily QA agent (extraction, distribution, scheduler health, RLS, orphans)
11. `docs/SKILLS.md` — 17 system capabilities with triggers, inputs, outputs, costs
12. `KNOWN_ISSUES.md` — current bugs and limitations
