# Gianluigi System Architecture — Current State (Post Phase 4)

**Date:** March 16, 2026
**Version:** v1.0 (Phase 0-4 complete)
**Tests:** 933 passing
**Deployed:** Cloud Run (europe-west1, 512Mi, min-instances=1)

Please create a comprehensive visual diagram of this system. Use flowcharts, sequence diagrams, and architecture diagrams to show how all components connect. Make it beautiful and clear.

---

## 1. HIGH-LEVEL ARCHITECTURE

```
                    ┌─────────────────────────────────────┐
                    │         EXTERNAL INPUTS              │
                    ├──────┬──────┬──────┬──────┬──────────┤
                    │Tactiq│Gmail │Google│Google│  Google   │
                    │(Meet)│ API  │Drive │Cal   │  Sheets   │
                    └──┬───┴──┬───┴──┬───┴──┬───┴────┬─────┘
                       │      │      │      │        │
                    ┌──▼──────▼──────▼──────▼────────▼─────┐
                    │          SCHEDULER LAYER              │
                    │  (8 background asyncio tasks)         │
                    │                                       │
                    │  transcript_watcher (30s poll Drive)  │
                    │  document_watcher (5min poll Drive)   │
                    │  email_watcher (5min poll Gmail)      │
                    │  personal_email_scanner (daily 7am)   │
                    │  morning_brief_scheduler (daily 7am)  │
                    │  meeting_prep_scheduler (hourly)      │
                    │  weekly_digest_scheduler (Sun 18:00)  │
                    │  orphan_cleanup_scheduler (6hr)       │
                    └──────────────┬────────────────────────┘
                                   │
                    ┌──────────────▼────────────────────────┐
                    │         PROCESSOR LAYER               │
                    │                                       │
                    │  transcript_processor (Opus extract)  │
                    │  email_classifier (Haiku/Sonnet)      │
                    │  morning_brief (compile + format)     │
                    │  cross_reference (dedup/status/Q)     │
                    │  entity_extraction (Haiku 2-pass)     │
                    │  proactive_alerts (4 SQL detectors)   │
                    │  debrief (interactive session)        │
                    │  document_ingestion (Word/PDF)        │
                    │  meeting_prep_generator (Sonnet)      │
                    │  weekly_digest_generator (Sonnet)     │
                    └──────────────┬────────────────────────┘
                                   │
                    ┌──────────────▼────────────────────────┐
                    │         GUARDRAILS LAYER              │
                    │                                       │
                    │  approval_flow (Eyal approves all)    │
                    │  inbound_filter (5-layer security)    │
                    │  calendar_filter (CropSight only)     │
                    │  content_filter (sensitivity tags)    │
                    │  gantt_guard (schema validation)      │
                    └──────────────┬────────────────────────┘
                                   │
                    ┌──────────────▼────────────────────────┐
                    │         CORE BRAIN                    │
                    │                                       │
                    │  Multi-Agent System:                  │
                    │    Router (Haiku) → classify intent   │
                    │    Conversation Agent (Sonnet)        │
                    │    Analyst Agent (Opus) → deep work   │
                    │    Operator Agent (Sonnet) → actions  │
                    │                                       │
                    │  RAG: semantic + fulltext + RRF       │
                    │  Tools: 12 callable functions         │
                    │  LLM: centralized via core/llm.py    │
                    └──────────────┬────────────────────────┘
                                   │
                    ┌──────────────▼────────────────────────┐
                    │         INTERFACES                    │
                    │                                       │
                    │  Telegram Bot (primary daily UI)      │
                    │  Gmail (send approved content)        │
                    │  Google Sheets (task/Gantt write)     │
                    │  Google Drive (summary docs upload)   │
                    └──────────────────────────────────────┘
                                   │
                    ┌──────────────▼────────────────────────┐
                    │         STORAGE                       │
                    │                                       │
                    │  Supabase (PostgreSQL + pgvector)     │
                    │  Tables: meetings, tasks, decisions,  │
                    │    embeddings, entities, commitments,  │
                    │    email_scans, pending_approvals,    │
                    │    gantt_schema, gantt_proposals,     │
                    │    debrief_sessions, documents...     │
                    └──────────────────────────────────────┘
```

---

## 2. DAILY RHYTHM (The Heartbeat)

This is the designed daily operating cycle:

```
TIME (IST)  EVENT                           COMPONENT
──────────  ──────────────────────────────  ──────────────────────────
07:00       Morning Brief                   morning_brief_scheduler
            ├─ Run personal email scan      personal_email_scanner
            ├─ Collect overnight constant    email_watcher (queued)
            │  layer extractions
            ├─ Fetch today's calendar        google_calendar
            ├─ Compile into ONE message      morning_brief processor
            └─ Send to Eyal for approval     approval_flow → Telegram
                ├─ "Approve all" → inject items into DB + RAG
                ├─ "Review items" → per-item approve/skip
                └─ "Dismiss" → discard

All day     Continuous monitoring:
            ├─ Transcript watcher (30s)      new meeting recordings
            ├─ Email watcher (5min)          team emails to Gianluigi
            │   ├─ Direct questions → answer immediately
            │   ├─ Approval replies → process
            │   └─ Everything else → queue for next morning brief
            ├─ Document watcher (5min)       new uploads to Drive
            └─ Meeting prep (1hr before)     prep docs for CropSight meetings

~18:00      End-of-Day Debrief (Eyal-initiated via Telegram)
            ├─ Interactive conversation      debrief processor
            ├─ Quick injection mode          "just log this"
            ├─ Full debrief mode             structured extraction
            └─ Reviews queued email items    "I also captured 2 items..."

Sunday      Weekly Digest                   weekly_digest_scheduler
18:00       ├─ All meetings this week
            ├─ Task scorecard
            ├─ Commitment scorecard
            ├─ Cross-reference intelligence
            └─ Sent to Eyal for approval
```

---

## 3. DATA FLOW: TRANSCRIPT PIPELINE

```
Tactiq (Chrome) → Google Drive (auto-upload)
      │
      ▼
transcript_watcher (polls every 30s)
      │ detects new .txt file
      ▼
transcript_processor
      │
      ├─ Step 1: Download transcript text
      ├─ Step 2: Parse (Tactiq unbracketed format: "MM:SS Speaker: text")
      ├─ Step 3: Claude Opus extraction (structured JSON)
      │          → summary, key_points, tasks, decisions, follow_ups,
      │            open_questions, stakeholders, commitments
      ├─ Step 4: Store meeting record in Supabase
      ├─ Step 5: Store tasks, decisions, follow-ups, open questions
      ├─ Step 6: Create embeddings (contextual chunks + metadata)
      ├─ Step 7a: Cross-reference (dedup tasks, infer status, resolve Qs)
      ├─ Step 7b: Entity extraction (Opus piggyback + Haiku validation)
      ├─ Step 7c: Commitment extraction (Opus piggyback)
      ├─ Step 7d: Proactive alerts check
      └─ Step 8: Submit for approval
              │
              ▼
         approval_flow → Telegram message to Eyal
              │
              ├─ "Approve" → distribute to team
              │   ├─ Telegram group message (formatted summary)
              │   ├─ Gmail to all participants
              │   ├─ Google Sheets (tasks + commitments)
              │   ├─ Google Drive (.md + .docx summaries)
              │   └─ Apply cross-reference changes
              │
              ├─ "Edit" → review mode (multi-turn conversation)
              │
              └─ Auto-publish after 60 minutes if no response
```

---

## 4. DATA FLOW: EMAIL INTELLIGENCE (Phase 4 — NEW)

### 4a. Constant Layer (Team emails to gianluigi@cropsight.io)

```
Team member sends email to gianluigi@cropsight.io
      │
      ▼
email_watcher (polls every 5 minutes)
      │
      ├─ Route 1: Direct question → answer immediately via Claude agent
      ├─ Route 2: Approval reply → process approval
      ├─ Route 3: Attachment → download + ingest (with type/size filter)
      └─ Route 4: ALL emails → _extract_and_log()
              │
              ├─ Haiku classifies: relevant / borderline / false_positive
              ├─ Sonnet extracts: tasks, decisions, commitments, info
              └─ Queue to email_scans (approved=False)
                      │
                      └─ Included in next morning brief
```

### 4b. Daily Scan Layer (Eyal's personal Gmail — read-only)

```
07:00 IST — morning_brief_scheduler triggers
      │
      ▼
personal_email_scanner.run_daily_scan()
      │
      ├─ Step 1: Build live keyword list
      │          (entity registry + active tasks + recent decisions + baseline)
      ├─ Step 2: Load tracked thread IDs from DB (last 30 days)
      ├─ Step 3: Fetch yesterday's emails (metadata only — headers + snippet)
      ├─ Step 4: Apply whitelist filter chain:
      │          ┌─ Blocklist check (reject personal contacts)
      │          ├─ Team member? (sender or recipient)
      │          ├─ Known stakeholder domain? (entity registry)
      │          ├─ Subject contains CropSight keywords? (live list)
      │          └─ Thread already tracked? (from email_scans DB)
      │
      ├─ Step 5: Check thread overlap with constant layer (avoid dupes)
      ├─ Step 6: Haiku classifies passing emails
      ├─ Step 7: Sonnet extracts from relevant emails (full body fetch)
      ├─ Step 8: Log outbound emails as metadata only (no LLM cost)
      ├─ Step 9: Note attachments without downloading
      └─ Step 10: Queue all to email_scans (approved=False)
```

### 4c. Morning Brief Compilation

```
personal_email_scanner completes
      │
      ▼
compile_morning_brief()
      │
      ├─ Source 1: Daily email scan results (personal Gmail)
      ├─ Source 2: Overnight constant layer (queued email_scans)
      ├─ Source 3: Today's calendar events (CropSight-filtered)
      └─ Source 4: (Future: overnight alerts)
      │
      ▼
format_morning_brief()
      │
      ├─ Group by source category: team / investor / legal / partner / other
      ├─ Show extracted intelligence, NOT raw email metadata
      │   Example: "From investor correspondence (Mar 16): [SENSITIVE]
      │             • [commitment] Term sheet discussion expected next week"
      ├─ Calendar preview section
      └─ Truncate at 4000 chars for Telegram
      │
      ▼
submit_for_approval(content_type="morning_brief")
      │
      ├─ "Approve all" → _apply_morning_brief_approval()
      │   ├─ Mark email_scans as approved=True
      │   ├─ Inject items: tasks → create_task, decisions → create_decision,
      │   │   commitments → create_commitment, info → store embedding
      │   └─ Create RAG embeddings with source_type='email'
      │
      ├─ "Review items" → per-item approve/skip
      └─ "Dismiss" → mark as dismissed, no injection
```

---

## 5. DATA FLOW: GANTT INTEGRATION (Phase 2)

```
Eyal says: "Move Moldova pilot to week 22"
      │
      ▼
Telegram bot → Multi-agent system
      │
      ├─ Router (Haiku): classifies as gantt_operation
      ├─ Conversation Agent (Sonnet): understands intent
      └─ Operator Agent (Sonnet): executes via tools
              │
              ▼
         gantt_manager
              │
              ├─ read_gantt() → fetch current state from Google Sheets
              ├─ Validate against gantt_schema (column types, allowed values)
              ├─ Create snapshot backup (gantt_snapshots table)
              ├─ Build proposed changes
              └─ submit gantt_proposal for approval
                      │
                      ▼
                 approval_flow → Telegram
                      │
                      ├─ "Approve" → write changes to Google Sheets
                      ├─ "Reject" → discard proposal
                      └─ "Rollback" → restore from snapshot
```

---

## 6. DATA FLOW: DEBRIEF SESSION (Phase 3)

```
Eyal sends: "/debrief" or "let's do a debrief"
      │
      ▼
debrief processor (interactive Telegram conversation)
      │
      ├─ Mode 1: Quick Injection
      │   Eyal: "just log that Paolo confirmed the Lavazza timeline"
      │   → Extract + confirm + inject immediately
      │
      └─ Mode 2: Full Debrief
          ├─ Gianluigi asks structured questions
          ├─ Eyal provides updates conversationally
          ├─ Items extracted with follow-up verification
          ├─ Queued email items surfaced:
          │   "I also captured 2 items from emails today — review those too?"
          └─ Final approval → inject all items
                  │
                  ├─ Tasks → Supabase + Google Sheets
                  ├─ Decisions → Supabase
                  ├─ Commitments → Supabase + Sheets
                  └─ All → RAG embeddings
```

---

## 7. MULTI-AGENT SYSTEM (Core Brain)

```
User message arrives (Telegram or email)
      │
      ▼
┌─────────────────────────────┐
│  ROUTER (Haiku — cheap)     │
│  Classifies intent:         │
│  • question                 │
│  • task_status              │
│  • entity_lookup            │
│  • decision_history         │
│  • gantt_operation          │
│  • debrief                  │
│  • general                  │
└────────────┬────────────────┘
             │
      ┌──────▼──────┐
      │ CONVERSATION │
      │ AGENT        │
      │ (Sonnet)     │
      │              │
      │ Has 12 tools:│
      │ • get_tasks  │
      │ • get_decisions
      │ • search_knowledge
      │ • get_entity_info
      │ • get_entity_timeline
      │ • get_commitments
      │ • get_meeting_context
      │ • get_calendar
      │ • get_email_intelligence  ← NEW (Phase 4)
      │ • read_gantt
      │ • write_gantt
      │ • web_search
      └──────┬──────┘
             │
             │ (escalates complex analysis)
             ▼
      ┌──────────────┐
      │ ANALYST      │
      │ AGENT (Opus) │
      │              │
      │ Deep analysis│
      │ Transcript   │
      │ extraction   │
      └──────────────┘
```

---

## 8. RAG SYSTEM (Institutional Memory)

```
WRITE PATH:
  Meeting transcript → contextual chunks (metadata prepended)
  Email intelligence → approved items embedded
  Documents (Word/PDF) → chunked + embedded
  Debrief items → embedded with source attribution
      │
      ▼
  OpenAI text-embedding-3-small (1536 dimensions)
      │
      ▼
  Supabase pgvector (embeddings table)
  + tsvector full-text index

READ PATH:
  User query → query router classifies type
      │
      ├─ Semantic search (pgvector cosine similarity)
      ├─ Full-text search (tsvector ts_rank)
      └─ Merge via Reciprocal Rank Fusion (RRF)
              │
              ├─ Time-weighted: 0.7 * RRF + 0.3 * recency
              ├─ Parent chunk enrichment (±1 chunks for context)
              └─ Source priority (meetings > emails > documents)
```

---

## 9. APPROVAL FLOW (Central Guardrail)

```
CONTENT TYPES:
  ┌──────────────────────┬───────────────────────────────────┐
  │ Content Type         │ What happens on approval          │
  ├──────────────────────┼───────────────────────────────────┤
  │ meeting_summary      │ Distribute: Telegram group, Gmail,│
  │                      │ Sheets, Drive (.md + .docx)       │
  ├──────────────────────┼───────────────────────────────────┤
  │ meeting_prep         │ Send prep doc to Eyal only        │
  ├──────────────────────┼───────────────────────────────────┤
  │ weekly_digest        │ Send to Eyal only (Telegram+Email)│
  ├──────────────────────┼───────────────────────────────────┤
  │ gantt_update         │ Write changes to Google Sheets    │
  ├──────────────────────┼───────────────────────────────────┤
  │ morning_brief (NEW)  │ Mark scans approved, inject items │
  │                      │ to DB + RAG, no team distribution │
  ├──────────────────────┼───────────────────────────────────┤
  │ debrief              │ Inject items to DB + RAG          │
  └──────────────────────┴───────────────────────────────────┘

  All approvals persisted in Supabase (pending_approvals table).
  Auto-publish: meeting summaries auto-approve after 60 minutes.
  Timer reconstruction on restart from DB state.
```

---

## 10. SECURITY LAYERS

```
INBOUND (5 layers):
  1. Sender verification (team member check)
  2. Topic relevance (CropSight-related?)
  3. Leak prevention (no internal data in responses)
  4. Output sanitization (strip sensitive markers)
  5. Audit logging (all interactions logged)

EMAIL FILTER CHAIN (Phase 4):
  1. Blocklist (personal contacts)
  2. Team member whitelist
  3. Known stakeholder domain (entity registry)
  4. CropSight keyword match (live-built list)
  5. Tracked thread passthrough

CONTENT GUARDRAILS:
  - Sensitivity classification at ingestion
  - Tags follow data through pipeline
  - [SENSITIVE] flag on investor/legal content
  - Source categorization (never raw email metadata)
  - CEO-approval-first for ALL team distributions
```

---

## 11. EXTERNAL SYSTEMS MAP

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Telegram   │     │    Gmail     │     │ Google Drive  │
│              │     │              │     │              │
│ Bot API      │     │ gianluigi@   │     │ Transcripts  │
│ Eyal DM      │     │ cropsight.io │     │ Documents    │
│ Group chat   │     │              │     │ Summaries    │
│ Approval UI  │     │ Eyal personal│     │              │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       └────────────┬───────┴────────────┬───────┘
                    │                    │
              ┌─────▼────────────────────▼─────┐
              │         GIANLUIGI              │
              │     (Cloud Run instance)       │
              │                                │
              │  Python 3.11+ / asyncio        │
              │  Claude API (Opus/Sonnet/Haiku)│
              │  OpenAI Embeddings API         │
              └─────┬────────────────────┬─────┘
                    │                    │
       ┌────────────┴───────┬────────────┴───────┐
       │                    │                    │
┌──────▼───────┐     ┌──────▼───────┐     ┌──────▼───────┐
│   Supabase   │     │Google Sheets │     │Google Calendar│
│              │     │              │     │              │
│ PostgreSQL   │     │ Task Tracker │     │ Read-only    │
│ pgvector     │     │ Commitments  │     │ CropSight    │
│ 15+ tables   │     │ Gantt Chart  │     │ events only  │
│ Frankfurt EU │     │ Stakeholders │     │ (purple=CS)  │
└──────────────┘     └──────────────┘     └──────────────┘
```

---

## 12. FILE STRUCTURE

```
gianluigi/
├── main.py                          # Entry point — starts all services
├── config/
│   ├── settings.py                  # All env vars + configuration (~100 settings)
│   └── team.py                      # Team emails, calendar keywords, email filter chain
├── core/
│   ├── agent.py                     # Conversation agent with 12 tools
│   ├── llm.py                       # Centralized LLM calls (caching, retries)
│   ├── router.py                    # Multi-agent router (Haiku intent classification)
│   ├── orchestrator.py              # v1.0 multi-agent orchestrator
│   ├── system_prompt.py             # Gianluigi personality + guardrails
│   └── tools.py                     # Tool definitions (JSON schema)
├── services/
│   ├── supabase_client.py           # Database (2600+ lines, SYNC methods)
│   ├── telegram_bot.py              # Telegram bot (commands + conversations)
│   ├── gmail.py                     # Gmail send/receive
│   ├── google_drive.py              # Drive read/write
│   ├── google_calendar.py           # Calendar read-only
│   ├── google_sheets.py             # Sheets read/write
│   ├── embeddings.py                # OpenAI embedding service
│   ├── conversation_memory.py       # In-memory per-chat history
│   ├── health_server.py             # Cloud Run health check
│   └── word_generator.py            # .docx summary generation
├── processors/
│   ├── transcript_processor.py      # Meeting extraction pipeline (Opus)
│   ├── email_classifier.py          # Email classification + extraction (Phase 4)
│   ├── morning_brief.py             # Morning brief compile + format (Phase 4)
│   ├── cross_reference.py           # Task dedup, status inference, Q resolution
│   ├── entity_extraction.py         # Entity linking (Haiku 2-pass)
│   ├── proactive_alerts.py          # 4 SQL-driven alert detectors
│   ├── debrief.py                   # Interactive debrief sessions (Phase 3)
│   ├── meeting_prep_generator.py    # Pre-meeting prep documents
│   ├── weekly_digest_generator.py   # Weekly summary generation
│   ├── document_ingestion.py        # Word/PDF processing
│   └── sensitivity_classifier.py    # Content sensitivity tagging
├── schedulers/
│   ├── transcript_watcher.py        # Poll Drive for new transcripts (30s)
│   ├── document_watcher.py          # Poll Drive for new documents (5min)
│   ├── email_watcher.py             # Poll Gmail inbox (5min)
│   ├── personal_email_scanner.py    # Daily scan of Eyal's Gmail (Phase 4)
│   ├── morning_brief_scheduler.py   # Daily 7am IST trigger (Phase 4)
│   ├── meeting_prep_scheduler.py    # Hourly calendar check
│   ├── weekly_digest_scheduler.py   # Sunday evening digest
│   ├── orphan_cleanup_scheduler.py  # DB hygiene (6hr)
│   ├── alert_scheduler.py           # Proactive alerts (disabled)
│   └── task_reminder_scheduler.py   # Task reminders (disabled)
├── guardrails/
│   ├── approval_flow.py             # Central approval system (6 content types)
│   ├── inbound_filter.py            # 5-layer inbound security
│   ├── calendar_filter.py           # CropSight meeting detection
│   ├── content_filter.py            # Sensitivity classification
│   └── gantt_guard.py               # Gantt schema validation
├── models/
│   └── schemas.py                   # Pydantic data models
├── scripts/
│   ├── setup_supabase.sql           # Full DB schema
│   ├── migrate_phase4.sql           # Phase 4 migration (direction column)
│   └── seed_entities.py             # Initial entity registry
└── tests/
    └── 30+ test files               # 933 tests
```

---

## 13. LLM COST STRATEGY

```
TIER       MODEL              USE CASE                         EST. COST
──────────────────────────────────────────────────────────────────────────
Opus       claude-opus-4      Transcript extraction            ~$0.50/meeting
                              Document analysis
                              Deep analytical questions

Sonnet     claude-sonnet-4    Conversation agent               ~$0.05/query
                              Email intelligence extraction
                              Meeting prep generation
                              Gantt operations
                              Debrief extraction

Haiku      claude-haiku-4.5   Intent routing                   ~$0.001/call
                              Email classification
                              Entity extraction
                              Sensitivity classification
                              Question vs edit detection

Embeddings text-embedding-    All vector embeddings            ~$0.01/meeting
           3-small (OpenAI)

OPTIMIZATION:
  - Prompt caching on system prompts (cache_control: ephemeral)
  - Opus piggyback: entity + commitment extraction from Opus transcript call
  - Metadata-first email fetch: Haiku classifies on snippet, Sonnet only on relevant
  - Haiku for all classification tasks (~$0.001/email vs ~$0.01 with Sonnet)

ESTIMATED MONTHLY: ~$15-25 (4-person team, ~10 meetings/month, ~50 emails/day scanned)
```

---

## 14. DATABASE TABLES (Supabase)

```
CORE DATA:
  meetings              — meeting records with metadata
  tasks                 — extracted tasks (6 categories)
  decisions             — extracted decisions
  follow_up_meetings    — scheduled follow-ups
  open_questions        — unresolved questions
  documents             — ingested documents

INTELLIGENCE:
  embeddings            — pgvector embeddings (1536d) + tsvector
  entities              — entity registry (people, orgs, projects)
  entity_mentions       — entity appearances across meetings
  task_mentions         — task references across meetings
  commitments           — tracked commitments with fulfillment

EMAIL (Phase 4):
  email_scans           — all scanned emails (constant + daily layers)
                          columns: scan_type, direction, classification,
                          extracted_items, approved, thread_id

GANTT (Phase 2):
  gantt_schema          — column definitions + validation rules
  gantt_proposals       — proposed changes awaiting approval
  gantt_snapshots       — backup snapshots for rollback

SESSIONS:
  debrief_sessions      — interactive debrief state
  pending_approvals     — persistent approval queue

OPERATIONS:
  calendar_classifications — learned meeting classifications
  action_log            — audit trail of all actions
```

---

## 15. TELEGRAM COMMANDS

```
COMMAND          DESCRIPTION
───────────────  ──────────────────────────────────────────
/start           Welcome message + status
/help            Show all commands
/mytasks         Show open tasks for Eyal
/tasks           Show all open tasks
/decisions       Show recent decisions
/status          System health status
/digest          Generate weekly digest now
/reprocess <id>  Reprocess a meeting transcript
/gantt           Show current Gantt chart state
/debrief         Start end-of-day debrief session
/emailscan       Trigger manual email scan (1/day limit)

FREE TEXT:       Ask any question — routed through multi-agent system
REPLIES:         Reply to approval messages to edit/approve/reject
```
