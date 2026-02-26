# Gianluigi — Changelog

All notable changes to this project, organized by version.

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
