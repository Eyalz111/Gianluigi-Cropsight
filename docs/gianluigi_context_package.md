# Gianluigi — Full Context Package

Use this document to give Claude full context about the Gianluigi project for brainstorming and planning sessions.

---

## 1. What Is Gianluigi

Gianluigi is an AI operations assistant for CropSight, an Israeli agtech startup (4-person founding team, pre-revenue, ML-powered crop yield forecasting). It watches the team's meetings, documents, calendar, and emails — then automatically processes everything into structured, searchable institutional memory.

The team interacts with it through Telegram (chat) and email. It runs autonomously on Google Cloud Run and only bothers the CEO (Eyal) when it needs approval to share something with the team.

**In short:** The team has meetings, Gianluigi watches, remembers everything, and keeps everyone aligned — without anyone having to take notes or update spreadsheets.

### CropSight Context
- **Company:** AgTech startup — ML-powered crop yield forecasting using neural networks on satellite imagery, climate data, and agronomic parameters
- **Team:** Eyal Zror (CEO), Roye Tadmor (CTO, bioinformatics), Paolo Vailetti (BD, based in Italy), Prof. Yoram Weiss (Senior Advisor)
- **Stage:** Pre-revenue, PoC with first client in Moldova (Gagauzia region, wheat). IIA Tnufa funded. Model accuracy 85-91%
- **B2B SaaS targets:** Commodity traders, food manufacturers, agricultural insurers
- **Competitors:** EOS Data Analytics, Gro Intelligence, aWhere/DTN, SatYield

### Developer Context
- **Developer:** Eyal (completed AI Developer Course at Hebrew University). Capable but not a senior engineer — code must be clean, well-commented, and architecturally simple.
- **IDE:** Cursor with Claude Code CLI
- **Language:** Python 3.11+, async architecture

---

## 2. Architecture

```
INPUT                    PROCESSING                 OUTPUT
─────                    ──────────                 ──────

Google Drive  ──┐                                ┌── Google Drive (.md, .docx)
(transcripts,   │    ┌──────────────────┐        │
 documents)     │    │  Filter Layer    │        ├── Google Sheets (tasks,
                ├───→│  (sensitivity,   │        │   stakeholders, commitments)
Telegram Bot  ──┤    │   calendar,      │        │
(user queries)  │    │   content)       │        ├── Telegram (notifications,
                │    └────────┬─────────┘        │   approvals, answers)
Google Calendar─┤             │                  │
(read-only)     │    ┌────────▼─────────┐        ├── Email (summaries,
                │    │  Claude LLM      │        │   prep docs, replies)
Gmail ──────────┘    │  (tool use,      │───────→│
(inbound emails)     │   tiered models) │        └── Supabase (structured
                     └────────┬─────────┘             data + vector store)
                              │
                     ┌────────▼─────────┐
                     │  Approval Layer  │
                     │  (CEO reviews    │
                     │   before team    │
                     │   distribution)  │
                     └──────────────────┘
```

### Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Claude API (Opus/Sonnet/Haiku) via Anthropic SDK |
| Database | Supabase (PostgreSQL + pgvector, EU region) |
| Embeddings | OpenAI text-embedding-3-small (1536 dimensions) |
| Chat interface | Telegram Bot (python-telegram-bot) |
| Email | Gmail API (gianluigi.cropsight@gmail.com) |
| File storage | Google Drive API |
| Task tracking | Google Sheets API |
| Calendar | Google Calendar API (read-only) |
| Hosting | Google Cloud Run (europe-west1) |
| Transcription | Tactiq (Chrome extension, free plan) |
| Language | Python 3.11+, async |

**Monthly cost: ~$3-5** (LLM tokens + embeddings. Everything else is free tier.)

### Key Design Decisions
- **Single agent, not multi-agent.** One Claude agent with tool use handles everything. No CrewAI, no agent chains. Simpler to debug, cheaper to run.
- **Tiered models.** Opus for transcript extraction (accuracy-critical), Sonnet for agent queries and background tasks, Haiku for classification. Keeps costs at ~$3/month.
- **Human-in-the-loop.** Nothing reaches the team without CEO approval. Conversational editing lets Eyal refine outputs before distribution.
- **Hybrid search, not just embeddings.** Semantic + keyword search merged with Reciprocal Rank Fusion. Catches both meaning-based and exact-match queries.
- **All Google ecosystem.** Drive, Sheets, Calendar, Gmail — the team already uses these tools daily. Zero adoption friction.
- **Prompt caching.** System prompts cached across calls (90% cost reduction on repeated calls within 5-minute windows).

---

## 3. What It Does (All Features Built)

### Meeting Processing Pipeline
Meetings are transcribed by Tactiq (Chrome extension) and exported to Google Drive. Gianluigi detects new transcripts, then:
- Extracts **decisions**, **tasks** (with 6 categories), **open questions**, **follow-up meetings**, and **stakeholder mentions** — all with source timestamps
- Classifies meeting sensitivity (legal/investor meetings go only to the CEO)
- Runs **cross-meeting intelligence**: deduplicates tasks, infers status changes from discussion context, resolves open questions when they're answered in later meetings
- Builds an **entity registry** (people, companies, places) and tracks them across meetings
- Extracts **commitments** ("I'll have that ready by Friday") and tracks fulfillment
- Sends everything to the CEO for approval via Telegram — he can approve, edit through conversation, or reject
- On approval: uploads summary to Google Drive (.md + .docx), updates Task Tracker and Stakeholder Tracker in Google Sheets, notifies the team via email and Telegram

### Institutional Memory (RAG)
Every meeting transcript and document is chunked, embedded, and stored in a vector database. When anyone asks a question via Telegram:
- **Semantic search** (pgvector embeddings — understands meaning, not just keywords)
- **Full-text search** (PostgreSQL tsvector — catches exact names and terms)
- **Reciprocal Rank Fusion** to merge both result sets
- **Time-weighted scoring** so recent meetings rank higher
- **Parent chunk retrieval** for expanded context around matches
- **Query router** classifies questions (task status? entity lookup? decision history?) and pre-fetches relevant context

### Document Ingestion
Team members drop documents (PDFs, PPTX, DOCX) into a Google Drive folder. Gianluigi detects them, extracts text, classifies the document type (strategy, legal, technical, pitch, client), chunks and embeds for search, and links them to related meetings.

### Scheduled Intelligence
Runs autonomously in the background:
- **Meeting prep** — Before upcoming meetings, generates a briefing doc with past context, open tasks, relevant decisions, and suggested talking points. Sent to CEO for approval before distribution.
- **Weekly digest** — Every Sunday, generates a summary of the week: meetings held, decisions made, task progress, commitment scorecard, cross-meeting patterns, and operational alerts.
- **Pre-meeting reminders** — Sends context to relevant team members before their meetings.
- **Proactive alerts** — Detects overdue task clusters, stale commitments, recurring unresolved discussions, and open question pileup. Notifies the CEO.
- **Orphan cleanup** — Daily sweep for stale approvals, orphan data, failed auto-publishes.

### Telegram Bot
The team's primary interface:
- Free-text questions ("What did we decide about the Moldova timeline?")
- `/search <query>` — Search across all meetings and documents (with `-m`/`-d` flags)
- `/meetings` — Browse past meetings
- `/mytasks` — See your open tasks
- `/status` — System dashboard (Eyal only)
- `/cost` — LLM spend breakdown (Eyal only)
- `/reprocess` — Reprocess a meeting transcript
- Approval flow: Eyal reviews, edits conversationally, approves/rejects — all within Telegram

### Email Integration
- Gianluigi has its own Gmail address (gianluigi.cropsight@gmail.com)
- Team members can email questions — Gianluigi replies in-thread with sourced answers
- Approved content is also distributed via email (personalized per recipient with their action items)
- Approval replies by email are routed back to the approval flow

---

## 4. Database Schema

| Table | What it stores |
|-------|---------------|
| `meetings` | Meeting metadata, transcript, summary, approval status |
| `decisions` | Extracted decisions with timestamps and participants |
| `tasks` | Action items with assignee, deadline, priority, category, status |
| `open_questions` | Unresolved questions tracked across meetings |
| `follow_up_meetings` | Proposed follow-up meetings |
| `documents` | Ingested documents (PDFs, slides, etc.) |
| `embeddings` | Vector embeddings for semantic search (pgvector, 1536d) |
| `entities` | People, companies, places mentioned across meetings |
| `entity_mentions` | Where/when each entity was discussed |
| `commitments` | "I'll do X by Y" promises, tracked for fulfillment |
| `task_mentions` | Cross-meeting task references for status inference |
| `pending_approvals` | Approval queue (persistent across restarts) |
| `calendar_classifications` | Remembered meeting classifications (fuzzy matching) |
| `token_usage` | LLM cost tracking per call site |
| `audit_log` | Full audit trail of all system actions |

---

## 5. Project Structure

```
gianluigi/
├── main.py                              # Entry point, starts all services
├── config/
│   ├── settings.py                      # Pydantic settings with env validation
│   └── team.py                          # Team configuration, keywords, filters
├── core/
│   ├── agent.py                         # Claude agent with tool use
│   ├── llm.py                           # Centralized LLM helper with caching
│   ├── system_prompt.py                 # System prompts for Claude
│   ├── tools.py                         # Tool definitions for agent
│   ├── retry.py                         # Retry decorator with exponential backoff
│   ├── error_alerting.py                # Critical error notification to Eyal
│   └── logging_config.py               # Structured JSON logging
├── models/
│   └── schemas.py                       # Pydantic models for all data types
├── services/
│   ├── supabase_client.py               # Full Supabase integration (SYNC methods)
│   ├── telegram_bot.py                  # Telegram bot with commands, approval flow
│   ├── gmail.py                         # Gmail API (OAuth2)
│   ├── google_drive.py                  # Drive read/write + document polling
│   ├── google_sheets.py                 # Task & Stakeholder tracker
│   ├── google_calendar.py               # Calendar reading for prep
│   ├── embeddings.py                    # OpenAI embeddings + vector search
│   ├── conversation_memory.py           # In-memory per-chat history (TTL 30min)
│   ├── health_server.py                 # aiohttp /health + /ready for Cloud Run
│   └── word_generator.py               # Generate .docx summaries
├── processors/
│   ├── transcript_processor.py          # Full transcript pipeline (Tactiq → structured)
│   ├── cross_reference.py               # Task dedup, status inference, Q resolution
│   ├── entity_extraction.py             # Entity extraction + linking
│   ├── proactive_alerts.py              # 4 SQL-driven alert detectors
│   ├── meeting_prep.py                  # Meeting prep generation
│   ├── weekly_digest.py                 # Weekly digest generation
│   └── document_processor.py            # Document ingestion (PDF, PPTX, DOCX)
├── guardrails/
│   ├── approval_flow.py                 # Approval + distribution workflow
│   ├── calendar_filter.py               # CropSight meeting detection
│   ├── sensitivity_classifier.py        # Sensitive content detection
│   ├── content_filter.py                # Tone filtering
│   └── inbound_filter.py               # 5-layer inbound security
├── schedulers/
│   ├── transcript_watcher.py            # Drive polling for new transcripts
│   ├── document_watcher.py              # Documents folder polling
│   ├── meeting_prep_scheduler.py        # Prep doc generation + reminders
│   ├── weekly_digest_scheduler.py       # Sunday evening digest
│   ├── email_watcher.py                 # Gmail inbox polling + routing
│   ├── task_reminder_scheduler.py       # Deadline reminders
│   ├── alert_scheduler.py               # Proactive alert scheduler (12hr cycle)
│   └── orphan_cleanup_scheduler.py      # Daily stale data cleanup
├── scripts/
│   ├── setup_supabase.sql               # Full database schema
│   ├── seed_entities.py                 # Pre-populate entity registry
│   └── upload_secrets.py                # GCP Secret Manager upload
├── tests/                               # 579 tests (all passing)
├── Dockerfile                           # Multi-stage Docker build
├── cloudbuild.yaml                      # Cloud Build → Cloud Run deployment
└── requirements.txt                     # Python dependencies
```

---

## 6. Guardrails (Non-Negotiable)

- **Professional tone only** — No emotional characterizations ("Paolo was frustrated"), only factual attribution ("Paolo raised a concern about timeline")
- **Source citations** — Every extracted item references a transcript timestamp
- **Sensitivity filtering** — Legal, investor, HR content goes only to CEO
- **Personal content exclusion** — Health, family, social banter stripped from summaries
- **External participant caution** — Non-team members attributed by role/org, not name
- **Information security** — No financial details, equity splits, or credentials in outputs
- **Inbound guardrails** — Sender verification, topic relevance, leak prevention, output sanitization, audit logging
- **Human-in-the-loop** — Nothing reaches the team without CEO approval

---

## 7. Version History

### v0.1 — "Gianluigi Can Remember" (Feb 24, 2026)
- Full transcript pipeline: Tactiq → Claude extraction → Supabase storage → embeddings
- Approval flow via Telegram DM + email (approve/edit/reject)
- Distribution: tasks → Sheets, summaries → Drive, notifications → Telegram + email
- Telegram bot with commands, question answering via Claude agent
- Gmail integration, Google Drive watchers, Calendar reading
- All guardrails (calendar filter, sensitivity, content/tone filter)
- 68 tests

### v0.2 — "Gianluigi Is Proactive" (Feb 25, 2026)
- **RAG upgrade**: Hybrid search (semantic + full-text), RRF fusion, contextual chunk embeddings
- **Meeting prep documents**: Auto-generated before calendar meetings
- **Weekly digest**: Automated Sunday 18:00-20:00
- **Gmail read**: Email watcher routes team questions/attachments/approval replies
- **Stakeholder tracker updates**: Approval flow with Telegram buttons
- **Pre-meeting reminders**: 2-3 hours before meetings
- **Auto-publish with review window**: 60-minute timed auto-approve + /retract
- 204 tests

### v0.2.1 — Post-v0.2 Refinements (Feb 26, 2026)
- **Task categories**: 6 categories across full pipeline (Product & Tech, BD & Sales, Legal & Compliance, Finance & Fundraising, Operations & HR, Strategy & Research)
- **Approval routing**: Meeting prep + weekly digest routed through CEO approval
- **Google Sheets formatting**: Professional styling (colors, frozen headers, dropdowns, conditional formatting)
- **Inbound guardrails**: 5-layer security system
- 286 tests

### v0.3 Phase 1 — Operational Intelligence (Feb 28, 2026)
- **Task deduplication**: Compare new tasks against existing open tasks (NEW/DUPLICATE/UPDATE)
- **Task status inference**: Claude analyzes transcripts to detect task completions/progress
- **Open question resolution**: Detects when questions get answered in later meetings
- **Cross-reference orchestrator**: Coordinates all three analyses
- **Time-weighted RAG**: Recent meetings rank higher (30-day half-life)
- **Parent chunk retrieval**: Fetches neighboring chunks for context
- **Query router**: Classifies questions and pre-fetches relevant context
- 349 tests

### v0.3 Tier 2 — Entity Registry, Commitments, Alerts (Mar 1, 2026)
- **Entity registry**: People, orgs, projects, locations with alias resolution. Extracted by piggybacking on existing Opus transcript call (zero extra cost)
- **Commitment tracking**: Extracts verbal promises from transcripts, tracks fulfillment across meetings
- **Proactive alerts**: 4 SQL-driven detectors — overdue task clusters, stale commitments, recurring discussions, question pileup
- **Alert scheduler**: 12-hour cycle, once-per-day alerts to CEO
- **Commitment scorecard**: Weekly digest section
- **Settings centralization**: ~25 hardcoded values moved to config/settings.py
- 420 tests

### v0.4 — Persistent Approvals + Cloud Run (Mar 1, 2026)
- **Persistent approvals**: In-memory dict → Supabase table. Survives restarts, rebuilds timers on startup
- **Commitment Opus piggyback**: Commitments extracted in main Opus call (full transcript coverage, no extra LLM cost)
- **Cloud Run deployment**: Health server, Dockerfile, cloudbuild.yaml, secrets management
- 446 tests

### v0.4.1 — Calendar Memory + Word Docs + Commitments Sheet (Mar 1, 2026)
- **Calendar classification memory**: Persists to Supabase, fuzzy matching for similar titles
- **Word document summaries**: .docx generated alongside .md on approval
- **Commitment dashboard**: New "Commitments" tab in Task Tracker sheet
- **Reprocess command**: Re-run pipeline on existing transcripts
- 499 tests

### v0.5 — "Gianluigi Goes to Work" (Mar 2, 2026)
- **Cloud Run deployment**: Actually deployed and running in production
- **Cost optimization**: Centralized LLM helper with prompt caching on all call sites, token usage tracking
- **Writing quality**: Better prompts for discussion summaries, task descriptions, meeting prep
- **Document ingestion**: PPTX support, type classification, cross-referencing
- **Telegram UX**: /search with flags, /meetings browser, /status dashboard, inline Drive links
- **Production hardening**: Retry logic, orphan cleanup, structured logging, error alerting
- 579 tests

### Latest (Mar 13, 2026)
- **3 live testing bug fixes**: Sheets tab targeting (tasks going to wrong tab), email approval routing (replies now processed), edit count message accuracy
- Deployed to Cloud Run, DB wiped and rebuilt fresh

---

## 8. Data Flow: Meeting → Team Update

```
1. Team has a meeting
2. Tactiq transcribes → exports .txt to Google Drive
3. Gianluigi detects new file (polling every 5 min)
4. Calendar filter: is this a CropSight meeting? (checks color, participants, title)
5. Sensitivity classifier: legal/investor → CEO-only distribution
6. Claude Opus extracts structured data (decisions, tasks, questions, stakeholders, commitments)
7. Cross-meeting intelligence runs:
   - Task deduplication (is this task already tracked?)
   - Status inference (did they say the task is done?)
   - Open question resolution (was this question answered?)
   - Entity linking (who/what was mentioned?)
   - Commitment extraction (who promised what?)
8. Embeddings generated, stored in pgvector
9. Summary formatted, sent to CEO via Telegram for approval
10. CEO reviews:
    - Approves → distributed to team (Drive + Sheets + Telegram + Email)
    - Edits → conversational back-and-forth until approved
    - Rejects → archived, not distributed
```

---

## 9. What Was Planned But Not Yet Built

### v0.6 Backlog
- Personal/informal discussion filter (exclude family talk, football, casual chat from summaries)

### Ideas From Original Project Plan (Deferred)
| Feature | Why deferred |
|---------|-------------|
| Competitive monitoring (web search for EOS, Gro, aWhere, SatYield) | Nice-to-have, team isn't asking for it yet |
| Calendar write access (suggest/create events) | Risky — accidental calendar changes. Revisit after trust |
| Multi-step autonomous workflows (CrewAI) | Overengineered for 4-person team |
| Full auto-publish (remove approval gate) | Too early — Eyal should review until trust established |
| Sentiment analysis | Academic — not actionable for 4-person team |
| Onboarding context package | No new hires imminent |

---

## 10. Current State (Mar 13, 2026)

- **579 tests passing** — all green
- **Deployed to Cloud Run** (europe-west1, 512Mi, min-instances=1)
- **Live tested** with real meetings, real team interactions, real approval flows
- **DB freshly rebuilt** (Mar 13) — clean slate, entities seeded
- **Cost**: ~$3-5/month (LLM + embeddings, everything else free tier)
- The system works end-to-end: drop a transcript in Drive → get approval request in Telegram → approve → team gets summary email + Drive doc + Sheets update

### Known Limitations
- Telegram uses polling (not webhooks) — works with min-instances=1 but not ideal
- Email watcher checks every 5 minutes (not real-time)
- No web search / competitive monitoring
- No calendar write access
- Document ingestion is basic (no OCR, no image processing)
- Single-process architecture (no task queue)

---

## 11. Eyal's Pain Points (From Planning Sessions)

These are the real-world problems that drove development:
1. **Repeating discussions** — Same topics come up meeting after meeting without resolution
2. **Forgotten commitments** — "I'll send that by Friday" gets lost
3. **Stakeholder context loss** — "Who is this person and what did we last discuss with them?"
4. **Task sprawl** — Too many tasks, no clear priorities or status visibility
5. **Manual overhead** — Taking notes, updating spreadsheets, writing summaries after every meeting
6. **Information silos** — Context trapped in individual memories, not shared

---

## 12. Key Technical Details

### Supabase Client
- All methods are **SYNC** (never await them)
- Uses PostgREST API via supabase-py
- pgvector for semantic search, tsvector for full-text search

### LLM Strategy
- **Opus**: Transcript extraction (accuracy-critical, full transcript context)
- **Sonnet**: Agent queries, background tasks (meeting prep, weekly digest)
- **Haiku**: Classification tasks (task dedup, entity validation, intent classification)
- Prompt caching via `cache_control: {"type": "ephemeral"}` on system prompts

### Embeddings
- OpenAI `text-embedding-3-small` (1536 dimensions)
- Contextual chunk embeddings: metadata prepended for embedding, raw text stored for display
- `OPENAI_API_KEY` used as fallback for `EMBEDDING_API_KEY`

### Important IDs
- Eyal Telegram DM: `8190904141`
- Group chat: `-5187389631`
- Calendar color: `3` (purple = CropSight)
