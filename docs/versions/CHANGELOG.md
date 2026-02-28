# Gianluigi — Changelog

All notable changes to this project, organized by version.

---

## v0.3 Tier 2 — Entity Registry, Commitment Tracking, Proactive Alerts (2026-03-01)

**Tests:** 420 passing (71 new since Phase 1)

### Added
- **Entity Registry** — canonical entity records (people, orgs, projects, locations) with alias resolution. Entities are extracted by piggybacking on the existing Opus transcript call (zero extra LLM cost). Two-pass validation (extraction + Haiku review) replaces hardcoded blocklist.
- **Entity Mentions** — tracks when/where entities appear across meetings with speaker, context, and sentiment
- **Commitment Tracking** — extracts verbal promises ("I'll send that by Friday") from transcripts using Haiku, stores in `commitments` table with speaker, context, implied deadline
- **Commitment Fulfillment Detection** — compares open commitments against new transcripts to detect when promises are kept
- **Proactive Alerts** — 4 SQL-driven pattern detectors (no LLM): overdue task clusters (3+ per assignee), stale commitments (2+ weeks), recurring discussions (entity in 3+ meetings), open question pileup (5+ unresolved)
- **Alert Scheduler** — 12-hour cycle, once-per-day Telegram alerts to Eyal
- **Weekly Entity Health Check** — `review_entity_health()` auto-cleans team member entities, flags orphans, lists new entities
- **Commitment Scorecard** — weekly digest section showing open commitments by speaker
- **Operational Alerts Section** — weekly digest section with severity-grouped alerts
- **Agent Tools** — `get_entity_info`, `get_entity_timeline`, `get_commitments` for natural language queries
- **Settings Centralization** — ~25 hardcoded values (thresholds, intervals, chunk sizes) moved to `config/settings.py` with Pydantic Fields
- **Opus Piggyback** — entity extraction uses pre-extracted stakeholders from the main Opus transcript call instead of a separate Haiku call (100% transcript coverage, zero extra cost)
- **Seed Script** — `scripts/seed_entities.py` pre-populates registry with 7 known CropSight entities

### Changed
- All 7 schedulers + conversation_memory: module-level constant capture replaced with runtime resolution in `__init__` (fixes test mockability)
- Sheets category names: updated to match Claude extraction (BD & Sales, Legal & Compliance, Strategy & Research)
- Entity extraction prompt: CropSight-specific stakeholder definition with relationship types
- Transcript processor: Steps 7c (entity extraction) and 7d (post-meeting alerts) added after cross-reference

### New Files
| File | Purpose |
|------|---------|
| `processors/entity_extraction.py` | Entity extraction + linking + health check |
| `processors/proactive_alerts.py` | 4 SQL-driven alert detectors |
| `schedulers/alert_scheduler.py` | Proactive alert scheduler |
| `scripts/seed_entities.py` | Entity registry seed data |
| `tests/test_entity_extraction.py` | 38 tests |
| `tests/test_commitment_tracking.py` | 16 tests |
| `tests/test_proactive_alerts.py` | 15 tests |

### Database Tables Added
| Table | Purpose |
|-------|---------|
| `entities` | Canonical entity records with aliases and metadata |
| `entity_mentions` | Cross-meeting entity mention tracking |
| `commitments` | Verbal commitment tracking with status lifecycle |

---

## v0.3 Phase 1 — Operational Intelligence (2026-02-28)

**Tests:** 349 passing (35 new)

### Added
- **Task Deduplication** — newly extracted tasks are compared against existing open tasks using Claude Haiku; classified as NEW, DUPLICATE, or UPDATE. Duplicates create `task_mention` records instead of duplicate task rows.
- **Task Status Inference** — Claude Sonnet analyzes full transcripts against open tasks to detect completions or progress changes with confidence levels (high/medium/low)
- **Open Question Resolution** — detects when previously raised questions get answered in later meetings
- **Cross-Reference Orchestrator** — `run_cross_reference()` coordinates all three analyses and creates `task_mention` audit trail records
- **Cross-Meeting Intelligence in Approval Messages** — Telegram approval requests now show task status changes, deduplicated tasks, and resolved questions
- **Cross-Reference Application on Approve** — when Eyal approves, inferred task statuses are updated in Supabase and open questions are resolved
- **Weekly Digest Cross-Reference Section** — summarizes dedup/status/resolution activity for the week
- **Time-Weighted RAG** — search results are boosted by recency (30-day half-life, 70/30 RRF/recency blend)
- **Parent Chunk Retrieval** — `enrich_chunks_with_context()` now fetches neighboring chunks (index ± 1) for expanded context
- **Query Router** — lightweight keyword-based pre-classification (`task_status`, `entity_lookup`, `decision_history`, `general`) with context pre-fetching
- **`task_mentions` table** — tracks cross-meeting task references with implied_status, confidence, and evidence

### Changed
- Transcript pipeline: new Step 7b runs cross-reference before storing tasks
- `create_tasks_batch()` only receives genuinely new tasks (duplicates filtered out)
- `search_memory()` applies time-weighted scoring after RRF fusion
- `process_message()` pre-fetches relevant context based on query type

### New Files
| File | Purpose |
|------|---------|
| `processors/cross_reference.py` | Core cross-reference processor |
| `tests/test_cross_reference.py` | 35 tests |
| `docs/versions/v0.3_implementation.md` | Implementation notes |

---

## v0.2.1 — Post-v0.2 Refinements (2026-02-26)

**Tests:** 286 passing (82 new)

### Added
- **Task Categories** — 6 categories (Product & Tech, BD & Sales, Legal & Compliance, Finance & Fundraising, Operations & HR, Strategy & Research) flow through extraction, storage, tools, Sheets, and digest
- **Meeting Prep Approval** — prep docs route through Eyal's approval before distribution to team
- **Weekly Digest Approval** — digests route through Eyal's approval before distribution
- **Google Sheets Formatting** — `format_task_tracker()` and `format_stakeholder_tracker()` methods with dark blue headers, frozen rows, conditional status formatting, auto-resize
- **Inbound Guardrails** — 5-layer security system: sender verification, topic relevance, leak prevention, output sanitization, audit logging (`guardrails/inbound_filter.py`)
- **Information Security Rules** — system prompt forbids financial details, legal docs, credentials in responses
- **Content-Type Approval Dispatch** — `_pending_approvals` tracks meeting_summary vs meeting_prep vs weekly_digest

### Changed
- Auto-review window default: 30 -> 60 minutes
- Task Tracker sheet: 8 -> 9 columns (Category inserted at position B)
- Approval flow: content-type-aware dispatching
- Telegram `_handle_message()`: inbound filter check before agent processing
- Email watcher `_handle_question()`: outbound sanitization on replies
- Meeting prep scheduler: routes through `submit_for_approval()` instead of direct Telegram
- Weekly digest scheduler: routes through `submit_for_approval()` instead of direct email/Telegram

### New Files
| File | Purpose |
|------|---------|
| `guardrails/inbound_filter.py` | 5-layer inbound security |
| `tests/test_task_categories.py` | 24 tests |
| `tests/test_approval_routing.py` | 18 tests |
| `tests/test_sheets_formatting.py` | 9 tests |
| `tests/test_inbound_filter.py` | 31 tests |

---

## v0.2 — "Gianluigi Is Proactive" (2026-02-25)

**Tests:** 204 passing (136 new)

### Added
- **RAG Foundation Upgrade** — hybrid search (semantic pgvector + full-text tsvector), Reciprocal Rank Fusion, contextual chunk embeddings, cross-reference enrichment
- **Meeting Prep Documents** — auto-generated before calendar meetings with related decisions, tasks, stakeholder context, sensitivity-aware distribution
- **Weekly Digest** — automated Sunday 18:00-20:00, summarizes meetings/decisions/tasks/upcoming
- **Gmail Read** — email watcher polls inbox every 5 minutes, routes team questions/attachments/approval replies
- **Stakeholder Tracker Updates** — approval flow with Telegram inline buttons, auto-add/update Google Sheets
- **Pre-Meeting Reminders** — 2-3 hours before meetings, includes context from last related meeting + open task count
- **Auto-Publish with Review Window** — 30-minute timed auto-approve, `/retract` command, countdown indicator
- **New Claude Tools** — `generate_weekly_digest`, `update_stakeholder_tracker`, `search_gmail`

### New Files
| File | Purpose |
|------|---------|
| `schedulers/email_watcher.py` | Gmail inbox polling + routing |
| `schedulers/weekly_digest_scheduler.py` | Sunday evening digest scheduler |
| `tests/test_rag_search.py` | 27 tests |
| `tests/test_meeting_prep.py` | 24 tests |
| `tests/test_weekly_digest.py` | 30 tests |
| `tests/test_email_watcher.py` | 29 tests |
| `tests/test_stakeholder_updates.py` | 12 tests |
| `tests/test_auto_publish.py` | 14 tests |

---

## v0.1 — "Gianluigi Can Remember" (2026-02-24)

**Tests:** 68 passing

### Added
- **Full transcript pipeline** — Tactiq export -> parse -> Claude extraction -> Supabase storage -> embeddings
- **Approval flow** — Eyal reviews summaries via Telegram DM with Approve/Reject/Edit buttons
- **Distribution** — tasks -> Task Tracker (Sheets), follow-ups -> Task Tracker, stakeholders -> Stakeholder Tracker
- **Telegram bot** — commands (`/start`, `/tasks`, `/mytasks`, `/decisions`, `/questions`, `/myid`), question answering via Claude
- **Gmail integration** — send approval emails, summary distribution
- **Google Drive** — transcript watcher, document watcher, document ingestion pipeline
- **Google Calendar** — read upcoming meetings, CropSight color filter
- **Guardrails** — calendar filter, sensitivity classifier, content/tone filter
- **Schedulers** — transcript watcher (15 min), document watcher, meeting prep, task reminders

### Core Files
| File | Purpose |
|------|---------|
| `main.py` | Entry point, starts all services |
| `core/agent.py` | Claude agent with tool use |
| `core/system_prompt.py` | System prompts |
| `core/tools.py` | Tool definitions |
| `config/settings.py` | Pydantic settings |
| `config/team.py` | Team configuration |
| `models/schemas.py` | Pydantic models |
| `services/supabase_client.py` | Database integration |
| `services/telegram_bot.py` | Telegram bot |
| `services/gmail.py` | Gmail API |
| `services/google_drive.py` | Drive API |
| `services/google_sheets.py` | Sheets API |
| `services/google_calendar.py` | Calendar API |
| `services/embeddings.py` | OpenAI embeddings |
| `processors/transcript_processor.py` | Transcript pipeline |
| `processors/meeting_prep.py` | Meeting prep generation |
| `guardrails/approval_flow.py` | Approval + distribution |
| `guardrails/calendar_filter.py` | CropSight detection |
| `guardrails/sensitivity_classifier.py` | Sensitive content detection |
| `guardrails/content_filter.py` | Tone filtering |

---

## Pre-v0.1 — Infrastructure & Live Testing

### Bugs Fixed (24 total)
See `PROJECT_STATUS.md` "Bugs Fixed During Live Testing" section for the full list.

Key fixes:
- Tactiq format parsing (unbracketed `MM:SS Speaker:`)
- SupabaseClient sync methods (never await them)
- Telegram HTML formatting (was sending broken Markdown)
- Gmail scope issues (`gmail.modify` not in initial token)
- Calendar filter string vs dict mismatch
- Embedding API key fallback (`OPENAI_API_KEY` as fallback for `EMBEDDING_API_KEY`)
