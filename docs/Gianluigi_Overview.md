# Gianluigi — AI Operations Assistant for CropSight

## What Is This

Gianluigi is an AI assistant that acts as an operations coordinator for a 4-person founding team. It watches the team's meetings, documents, calendar, and emails — then automatically processes everything into structured, searchable institutional memory.

The team interacts with it through Telegram (chat) and email. It runs autonomously in the background and only bothers the CEO (Eyal) when it needs approval to share something with the team.

**In short:** The team has meetings, Gianluigi watches, remembers everything, and keeps everyone aligned — without anyone having to take notes or update spreadsheets.

---

## The Problem It Solves

CropSight is an early-stage agtech startup (4 people, pre-revenue, first client in Moldova). The founding team has 5-10 meetings per week across business development, product, legal, and strategy. Information gets lost between meetings: decisions are forgotten, tasks slip, context about stakeholders fades, and the CEO spends time manually tracking what everyone agreed to do.

Gianluigi eliminates that overhead.

---

## What It Does

### 1. Meeting Processing Pipeline

Meetings are transcribed by Tactiq (a Chrome extension) and exported to Google Drive. Gianluigi detects new transcripts, then:

- Extracts **decisions**, **tasks**, **open questions**, **follow-up meetings**, and **stakeholder mentions** — all with source timestamps
- Classifies meeting sensitivity (legal/investor meetings go only to the CEO)
- Runs **cross-meeting intelligence**: deduplicates tasks, infers status changes from discussion context, resolves open questions when they're answered in later meetings
- Builds an **entity registry** (people, companies, places) and tracks them across meetings
- Extracts **commitments** ("I'll have that ready by Friday") and tracks fulfillment
- Sends everything to the CEO for approval via Telegram — he can approve, edit through conversation, or reject
- On approval: uploads summary to Google Drive (.md + .docx), updates Task Tracker and Stakeholder Tracker in Google Sheets, notifies the team

### 2. Institutional Memory (RAG)

Every meeting transcript and document is chunked, embedded, and stored in a vector database. When anyone asks a question via Telegram, Gianluigi searches using:

- **Semantic search** (pgvector embeddings — understands meaning, not just keywords)
- **Full-text search** (PostgreSQL tsvector — catches exact names and terms)
- **Reciprocal Rank Fusion** to merge both result sets
- **Time-weighted scoring** so recent meetings rank higher
- **Parent chunk retrieval** for expanded context around matches

A query router classifies questions (task status? entity lookup? decision history?) and pre-fetches relevant context before the LLM responds.

### 3. Document Ingestion

Team members drop documents (PDFs, PPTX, DOCX) into a Google Drive folder. Gianluigi detects them, extracts text, classifies the document type (strategy, legal, technical, pitch, client), chunks and embeds for search, and links them to related meetings.

### 4. Scheduled Intelligence

Runs autonomously in the background:

- **Meeting prep** — Before upcoming meetings, generates a briefing doc with past context, open tasks, relevant decisions, and suggested talking points. Sent to CEO for approval before distribution.
- **Weekly digest** — Every Sunday, generates a summary of the week: meetings held, decisions made, task progress, commitment scorecard, cross-meeting patterns, and operational alerts.
- **Pre-meeting reminders** — Sends context to relevant team members before their meetings.
- **Proactive alerts** — Detects overdue task clusters, stale commitments, recurring unresolved discussions, and open question pileup. Notifies the CEO.
- **Orphan cleanup** — Daily sweep for stale approvals, orphan data, failed auto-publishes.

### 5. Telegram Bot

The team's primary interface:

- Free-text questions ("What did we decide about the Moldova timeline?")
- `/search <query>` — Search across all meetings and documents
- `/meetings` — Browse past meetings
- `/mytasks` — See your open tasks
- `/status` — System dashboard (Eyal only)
- `/cost` — LLM spend breakdown (Eyal only)
- Approval flow: Eyal reviews, edits conversationally, approves/rejects — all within Telegram

### 6. Email Integration

- Gianluigi has its own Gmail address
- Team members can email questions — Gianluigi replies in-thread with sourced answers
- Approved content is also distributed via email

---

## Architecture Overview

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

### Key Design Decisions

- **Single agent, not multi-agent.** One Claude agent with tool use handles everything. No CrewAI, no agent chains. Simpler to debug, cheaper to run.
- **Tiered models.** Opus for transcript extraction (accuracy-critical), Sonnet for agent queries, Haiku for classification tasks. Keeps costs at ~$3/month.
- **Human-in-the-loop.** Nothing reaches the team without CEO approval. Conversational editing lets Eyal refine outputs before distribution.
- **Hybrid search, not just embeddings.** Semantic + keyword search merged with RRF. Catches both meaning-based and exact-match queries.
- **All Google ecosystem.** Drive, Sheets, Calendar, Gmail — the team already uses these tools daily. Zero adoption friction.
- **Prompt caching.** System prompts cached across calls (90% cost reduction on repeated calls within 5-minute windows).

### Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Claude API (Opus/Sonnet/Haiku) via Anthropic SDK |
| Database | Supabase (PostgreSQL + pgvector, EU region) |
| Embeddings | OpenAI text-embedding-3-small |
| Chat interface | Telegram Bot (python-telegram-bot) |
| Email | Gmail API |
| File storage | Google Drive API |
| Task tracking | Google Sheets API |
| Calendar | Google Calendar API (read-only) |
| Hosting | Google Cloud Run |
| Transcription | Tactiq (Chrome extension, free plan) |
| Language | Python 3.11+, async |

**Monthly cost: ~$3-5** (LLM tokens + embeddings. Everything else is free tier.)

---

## Data Flow: Meeting → Team Update

```
1. Team has a meeting
2. Tactiq transcribes → exports .txt to Google Drive
3. Gianluigi detects new file (polling every 5 min)
4. Calendar filter: is this a CropSight meeting? (checks color, participants, title)
5. Sensitivity classifier: legal/investor → CEO-only distribution
6. Claude Opus extracts structured data (decisions, tasks, questions, stakeholders)
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

## Database Schema (Simplified)

| Table | What it stores |
|-------|---------------|
| `meetings` | Meeting metadata, transcript, summary, approval status |
| `decisions` | Extracted decisions with timestamps and participants |
| `tasks` | Action items with assignee, deadline, priority, category, status |
| `open_questions` | Unresolved questions tracked across meetings |
| `follow_up_meetings` | Proposed follow-up meetings |
| `documents` | Ingested documents (PDFs, slides, etc.) |
| `embeddings` | Vector embeddings for semantic search (pgvector) |
| `entities` | People, companies, places mentioned across meetings |
| `entity_mentions` | Where/when each entity was discussed |
| `commitments` | "I'll do X by Y" promises, tracked for fulfillment |
| `task_mentions` | Cross-meeting task references for status inference |
| `pending_approvals` | Approval queue (persistent across restarts) |
| `calendar_classifications` | Remembered meeting classifications (fuzzy matching) |
| `token_usage` | LLM cost tracking per call site |
| `audit_log` | Full audit trail of all system actions |

---

## Guardrails

- **Professional tone only** — No emotional characterizations ("Paolo was frustrated"), only factual attribution ("Paolo raised a concern about timeline")
- **Source citations** — Every extracted item references a transcript timestamp
- **Sensitivity filtering** — Legal, investor, HR content goes only to CEO
- **Personal content exclusion** — Health, family, social banter stripped from summaries
- **External participant caution** — Non-team members attributed by role/org, not name
- **Information security** — No financial details, equity splits, or credentials in outputs
- **Inbound guardrails** — Sender verification, topic relevance, leak prevention, output sanitization, audit logging

---

## Current State

- **579 tests passing**
- All features implemented through v0.5
- Ready for Cloud Run deployment (Dockerfile, health server, cloudbuild.yaml all built)
- Live-tested with real meetings and real team interactions
- Not yet running in production — final deployment step pending

---

## Open Questions / Feedback Welcome

1. **Architecture:** Single async Python process with schedulers — is this the right pattern for this scale, or would something like a task queue (Celery, etc.) be better?
2. **LLM strategy:** Tiered models (Opus/Sonnet/Haiku) with prompt caching — any other cost optimization ideas?
3. **Search:** Hybrid semantic + keyword with RRF fusion — anything you'd change?
4. **Approval flow:** Human-in-the-loop for everything — right call for a 4-person team, or overkill?
5. **Data model:** 15 tables in Supabase — overengineered or about right?
6. **Missing capabilities:** What would you add before calling this production-ready?
