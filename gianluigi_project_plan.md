# Gianluigi — Project Plan & Technical Handover

## Document Purpose
This document captures all architecture decisions, design choices, guardrails, and implementation plans for Gianluigi — CropSight's AI operations assistant. It serves as the primary reference for development via Claude Code / Cursor CLI. Every decision here was discussed and agreed upon by Eyal (CEO, CropSight).

**Last Updated:** February 2026
**Document Version:** 1.0

---

## 1. Project Overview

### What Is Gianluigi
Gianluigi is CropSight's internal AI operations assistant — think Jarvis for a founding team. It processes meeting transcripts, tracks tasks and decisions, maintains institutional memory, prepares meeting briefs, and keeps the founding team aligned. It's designed to evolve from an operations coordinator into a communications manager and eventually a strategic analyst.

### CropSight Context
- **Company:** Israeli AgTech startup — ML-powered crop yield forecasting using neural networks on satellite imagery, climate data, and agronomic parameters.
- **Team:** Eyal Zror (CEO), Roye Tadmor (CTO, bioinformatics), Paolo Vailetti (BD, based in Italy), Prof. Yoram Weiss (Senior Advisor).
- **Stage:** Pre-revenue, PoC with first client in Moldova (Gagauzia region, wheat). IIA Tnufa funded. Model accuracy 85-91%.
- **B2B SaaS targets:** Commodity traders, food manufacturers, agricultural insurers.
- **Competitors:** EOS Data Analytics, Gro Intelligence, aWhere/DTN, SatYield.

### Development Context
- **Developer:** Eyal (completed AI Developer Course at Hebrew University covering Python, data science, CrewAI, n8n). Capable but not a senior engineer — code must be clean, well-commented, and architecturally simple.
- **IDE:** Cursor with Claude Code CLI.
- **Language:** Python.
- **Approach:** Incremental side-project. Build MVP fast, iterate based on real usage.

---

## 2. Architecture

### High-Level Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                       INPUT CHANNELS                          │
│  Telegram Bot (real-time chat)  |  Gmail (gianluigi@email)   │
│  Google Drive watcher (Tactiq)  |  Google Calendar (read)     │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│                     FILTER & GUARD LAYER                      │
│  Calendar filter (CropSight only)                             │
│  Sensitivity classifier (legal/investor → Eyal-only)          │
│  Personal content filter                                      │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│                     PROCESSING LAYER                          │
│  Claude API (Opus 4.6 via Max plan) with Tool Use             │
│  Single agent, multiple tools — no CrewAI for now             │
│  Professional tone guardrails enforced via system prompt       │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│                     DATA LAYER                                │
│  Supabase (EU region — Frankfurt)                             │
│  ├── PostgreSQL: meetings, decisions, tasks, documents         │
│  └── pgvector: embedded transcript chunks for semantic search  │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│                     APPROVAL LAYER (v0.1)                      │
│  All outputs routed to Eyal first (Telegram DM + Gmail)       │
│  Eyal reviews → approves / requests edits / rejects            │
│  Conversational editing via reply                              │
│  Only approved content distributed to team                     │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│                     OUTPUT LAYER                               │
│  Google Drive (shared CropSight Ops folder)                   │
│  Google Sheets (Task Tracker, Stakeholder Tracker read/write) │
│  Telegram group notification                                  │
│  Email via Gianluigi's Gmail                                  │
└──────────────────────────────────────────────────────────────┘
```

### Tech Stack

| Layer | Technology | Why This Choice | Cost |
|-------|-----------|----------------|------|
| Interface (chat) | Telegram Bot (`python-telegram-bot`) | Free, works on mobile/desktop, group chat support, zero learning curve | Free |
| Interface (email) | Gmail API (Gianluigi's own address) | Structured outputs, formal channel for Paolo/Yoram | Free |
| LLM | Claude API — Opus 4.6 (Anthropic SDK, tool use) | Best reasoning, Eyal has Max plan | Covered by Max |
| Database | Supabase (PostgreSQL + pgvector, EU region) | Structured + vector search in one, managed, free tier | Free tier |
| File Storage/Output | Google Drive API | Shared folder accessible to all, familiar to team | Free |
| Task Tracking | Google Sheets API | Yoram and Paolo are "old school", spreadsheets work | Free |
| Calendar | Google Calendar API (read-only) | Meeting prep, scheduling context | Free |
| Embeddings | Voyage AI or OpenAI text-embedding-3-small | Required for vector search in pgvector | ~$1-2/mo |
| Hosting | Google Cloud Run | Free tier sufficient for 4 users, Eyal has Google account | Free tier |
| Transcription | Tactiq (free plan, transcript only — no AI credits) | Already in use, auto-exports to Google Drive | Free |

**Total estimated cost: $1-5/month**

### Why NOT These Alternatives

| Rejected Option | Reason |
|----------------|--------|
| **WhatsApp** | API requires Meta business verification, per-conversation costs, ToS violation risks with unofficial libraries. Telegram + Email covers all use cases. |
| **ChromaDB** | Local-only vector DB. Multi-user access requires hosting. Supabase gives structured + vector in one managed service. |
| **CrewAI / Multi-agent** | Overkill for v0.1. Single Claude agent with tools handles all current flows. Revisit in v0.3+ when parallel autonomous workflows emerge (e.g., weekly market research + client update drafting). |
| **Railway.app** | Good option but Google Cloud Run is free and keeps everything in Google ecosystem. |
| **Slack** | Would require everyone to adopt a new tool. Telegram is simpler and already familiar as a chat app. |

---

## 3. Data Model (Supabase Schema)

```sql
-- Core tables

CREATE TABLE meetings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date TIMESTAMPTZ NOT NULL,
    title TEXT NOT NULL,
    participants TEXT[] NOT NULL,
    duration_minutes INTEGER,
    raw_transcript TEXT,
    summary TEXT,
    sensitivity TEXT DEFAULT 'normal', -- 'normal', 'sensitive', 'legal'
    source_file_path TEXT, -- Google Drive path to original Tactiq export
    approval_status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id UUID REFERENCES meetings(id),
    description TEXT NOT NULL,
    context TEXT, -- surrounding discussion context
    participants_involved TEXT[],
    transcript_timestamp TEXT, -- source citation, e.g., "43:28"
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id UUID REFERENCES meetings(id), -- nullable for manually created tasks
    title TEXT NOT NULL,
    assignee TEXT NOT NULL,
    deadline DATE,
    status TEXT DEFAULT 'pending', -- 'pending', 'in_progress', 'done', 'overdue'
    priority TEXT DEFAULT 'M', -- 'H', 'M', 'L'
    transcript_timestamp TEXT, -- source citation
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE follow_up_meetings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_meeting_id UUID REFERENCES meetings(id),
    title TEXT NOT NULL,
    proposed_date TIMESTAMPTZ,
    led_by TEXT NOT NULL,
    participants TEXT[],
    agenda_items TEXT[],
    prep_needed TEXT, -- what needs to happen before this meeting
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    source TEXT, -- 'upload', 'email', 'drive'
    file_type TEXT,
    summary TEXT,
    drive_path TEXT,
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE open_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_id UUID REFERENCES meetings(id),
    question TEXT NOT NULL,
    raised_by TEXT,
    status TEXT DEFAULT 'open', -- 'open', 'resolved'
    resolved_in_meeting_id UUID REFERENCES meetings(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Vector embeddings table (using pgvector)

CREATE TABLE embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL, -- 'meeting', 'document'
    source_id UUID NOT NULL,
    chunk_text TEXT NOT NULL,
    chunk_index INTEGER,
    speaker TEXT, -- who said this (for meeting chunks)
    timestamp_range TEXT, -- e.g., "43:00-45:30"
    embedding VECTOR(1536), -- dimension depends on embedding model
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Audit log (tracks all Gianluigi actions)

CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action TEXT NOT NULL, -- 'meeting_processed', 'task_created', 'summary_approved', etc.
    details JSONB,
    triggered_by TEXT, -- 'auto', 'eyal', 'roye', 'paolo', 'yoram'
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 4. Google Drive Structure

```
Google Drive
└── CropSight Ops/ (shared with all 4 founders)
    ├── Raw Transcripts/          ← Tactiq auto-exports here (input)
    │   └── (Gianluigi reads from here, never modifies)
    ├── Meeting Summaries/        ← Gianluigi writes approved summaries
    │   └── 2026-02-22 - MVP Focus.md
    ├── Meeting Prep/             ← Gianluigi writes prep docs before meetings
    │   └── 2026-02-27 - Prep - Accuracy and Founders Agreement.md
    ├── Weekly Digests/           ← Gianluigi writes weekly summaries
    │   └── Week of 2026-02-17.md
    └── Documents/                ← Team uploads docs for Gianluigi to ingest
        └── (research papers, MOUs, competitor analyses)
```

### Google Sheets (Existing + New)

1. **CropSight Stakeholder Tracker** (EXISTING — Eyal's current sheet, read/write)
   - Columns: Organization/Name, Type, Short Description, Contact Person + Email, Desired Outcome, Priority, Primary Action Type, Owner, Next Action, Due Date, Secondary Action Type, Owner, Next Action, Due Date, Status, Notes
   - v0.1: Gianluigi reads from this for meeting prep context
   - v0.2: Gianluigi suggests updates when new stakeholders appear in transcripts (with Eyal approval)

2. **CropSight Task Tracker** (NEW — created by Gianluigi)
   - Columns: Task, Assignee, Source Meeting, Deadline, Status, Priority, Created Date, Updated Date
   - Auto-updated after every approved meeting summary
   - Anyone can query via Telegram: "What are my open tasks?"

---

## 5. Data Pipeline: Tactiq → Gianluigi

### Pipeline Flow

```
Stage 1: CAPTURE (Tactiq — free plan, transcript only)
  Google Meet meeting happens
       ↓
  Tactiq transcribes in real-time (no AI credits used)
       ↓
  Tactiq Workflow auto-exports RAW TRANSCRIPT to Google Drive
       ↓
  File lands in: CropSight Ops / Raw Transcripts /

Stage 2: DETECT (Gianluigi — Google Drive watcher)
  Gianluigi polls Raw Transcripts folder every 15 minutes
       ↓
  New file detected → downloads content via Drive API

Stage 3: PROCESS (Claude Opus 4.6 + Supabase)
  Parse raw transcript (speaker-labeled, timestamped)
       ↓
  Claude extracts (in one pass, using custom summary format):
    → Structured summary
    → Key decisions (with who, context, timestamp citation)
    → Action items (with assignee, deadline, timestamp)
    → Follow-up meetings (with leader, agenda, prep needed)
    → Open questions and risks
    → New stakeholders/contacts mentioned
       ↓
  Store structured data in Supabase tables
  Chunk raw transcript → embed → store in pgvector
       ↓
  Generate formatted meeting summary document

Stage 4: APPROVAL (Eyal reviews)
  Draft sent to Eyal via Telegram DM + Gmail
       ↓
  Eyal reviews for: accuracy, tone, hallucinations
       ↓
  Eyal replies to approve, request edits, or reject
  (Conversational editing: "Change task 3 deadline to March 5")
       ↓
  Claude processes edits → sends updated draft → repeat until approved

Stage 5: DISTRIBUTE (after approval)
  Meeting summary → Google Drive (Meeting Summaries/)
  Tasks → Supabase + Google Sheet (Task Tracker)
  Notification → Telegram group + Gianluigi Gmail to all founders
```

### Tactiq Configuration
- **Plan:** Pro ($12/month — already subscribed)
- **Workflow:** Automatic trigger on "Meeting processed" → export raw transcript to Google Drive folder. NO AI summary step (saves credits, Claude does this better).
- **Export format:** TXT with speaker labels and timestamps

### Key Design Decision: Claude Does the Summarization, Not Tactiq
Tactiq's AI uses GPT-4 and has limited credits. Claude Opus 4.6 (via Eyal's Max plan) produces higher-quality summaries with full control over format, and performs structured extraction (tasks, decisions, contacts) in the same pass. This eliminates Tactiq AI credit costs entirely.

---

## 6. Calendar Filtering — CropSight vs. Personal

Eyal uses a personal Google Calendar for all meetings. Gianluigi MUST only process CropSight-related meetings. Multi-layered filtering:

### Filter Chain (all layers checked, first match wins)

```python
# CropSight team email whitelist
CROPSIGHT_TEAM_EMAILS = [
    "eyal@...",      # Eyal
    "roye@...",      # Roye
    "paolo@...",     # Paolo
    "yoram@...",     # Yoram
]

# Calendar color for CropSight (purple)
CROPSIGHT_COLOR_ID = "..." # Google Calendar purple color ID

# Title prefix patterns (case-insensitive, typo-tolerant)
CROPSIGHT_PREFIXES = [
    "cropsight", "cs:", "cs ", "crop sight", "cropsigh",
    "crop-sight", "crop_sight",
]

# Explicit blocklist keywords
BLOCKED_KEYWORDS = [
    "ma ", "seminar", "personal", "doctor", "dentist",
    "university", "hebrew university", "thesis",
    "birthday", "lunch", "dinner",
]

def is_cropsight_meeting(event) -> bool:
    title_lower = event.title.lower().strip()

    # Layer 4 (blocklist) — checked first as a hard stop
    if any(word in title_lower for word in BLOCKED_KEYWORDS):
        return False

    # Layer 3: Calendar color check (purple)
    if event.color_id == CROPSIGHT_COLOR_ID:
        return True

    # Layer 2: Participant check (2+ CropSight team members)
    cropsight_attendees = [
        a for a in event.attendees
        if a.email in CROPSIGHT_TEAM_EMAILS
    ]
    if len(cropsight_attendees) >= 2:
        return True

    # Layer 1: Title prefix match (tolerant)
    if any(title_lower.startswith(prefix) for prefix in CROPSIGHT_PREFIXES):
        return True

    # UNCERTAIN — ask Eyal on Telegram before processing
    return None  # triggers "ask Eyal" flow
```

### Handling Uncertainty
When `is_cropsight_meeting()` returns `None`, Gianluigi messages Eyal on Telegram:
> "I see a meeting 'Infrastructure Planning' at 14:00 tomorrow with Roye. Is this CropSight-related?"

Eyal's reply (yes/no) is remembered for future meetings with similar titles.

---

## 7. Meeting Sensitivity Classification

Not all CropSight meetings should generate team-wide outputs. Sensitive meetings get restricted distribution.

```python
SENSITIVE_KEYWORDS = [
    "lawyer", "legal", "fischer", "fbc", "zohar",   # Legal
    "investor", "investment", "funding", "vc",        # Investor
    "nda", "confidential", "founders agreement",      # Confidential
    "personal", "hr", "compensation", "equity",        # HR/Equity
]

def classify_sensitivity(event) -> str:
    title_lower = event.title.lower()
    if any(kw in title_lower for kw in SENSITIVE_KEYWORDS):
        return "sensitive"  # Eyal-only output
    return "normal"         # Team-wide distribution
```

### Distribution Rules
- **Normal meetings:** Summary → full team (Telegram group + email to all)
- **Sensitive meetings:** Summary → Eyal only (Telegram DM + Eyal's email). Eyal can manually forward if appropriate.

---

## 8. Summary Format & Tone Guardrails

### Mandatory Summary Tone Rules (enforced in system prompt)

**PROFESSIONAL TONE ONLY.** Summaries must be factual, objective, and business-appropriate. Never include personal opinions, emotional characterizations, or interpersonal judgments about team members.

**Prohibited language patterns:**
- Never characterize emotions: "Paolo was frustrated", "Roye seemed concerned", "Yoram was unhappy with..."
- Never characterize relationships or dynamics: "There was tension between...", "They disagreed sharply...", "X dominated the discussion..."
- Never make performance judgments: "Roye's work was questioned", "Paolo pushed back on the quality of..."
- Never include personal or social content from the meeting: references to health, family, personal plans, jokes, social banter, etc.

**Required framing — attribute positions, not emotions:**
- ❌ "Paolo was not happy with the timeline"
- ✅ "Paolo raised a concern about time-to-market impact on fundraising"
- ❌ "Roye seemed defensive about accuracy"
- ✅ "Roye proposed writing a 1-page accuracy framework document"
- ❌ "Yoram dominated the security discussion"
- ✅ "Yoram recommended engaging an external security reviewer (Edo or equivalent)"
- ❌ "The team argued about cloud providers"
- ✅ "Cloud provider preference was discussed; Roye indicated AWS based on familiarity, with flexibility to revisit"

**Personal content filtering:**
- If the transcript contains personal discussions (health, family, weddings, personal anecdotes), exclude them from the summary entirely.
- Exception: If personal circumstances affect timelines/availability, note only the business impact: "Roye noted potential availability constraints in the coming months" — never the personal reason.

**External participants — extra caution:**
- When non-CropSight people attend meetings, handle their attributed statements more carefully.
- Prefer organizational attribution: "The Moldova client contact raised concerns about delivery timeline" rather than attributing specific quotes to named external individuals.

**Source citations:**
- Every extracted decision, task, and open question should reference the approximate transcript timestamp where it was discussed. This enables verification and builds trust.
- Format: "(ref: ~43:28)" appended to each item.

### Summary Document Template

```markdown
# Meeting Summary: [Title]
**Date:** [Date] | **Duration:** [X] minutes
**Participants:** [Names]
**Sensitivity:** [Normal / Sensitive]

---

## Key Decisions
1. [Decision description] — [who made it / agreed by team] (ref: ~MM:SS)
2. ...

## Action Items
| # | Task | Assignee | Deadline | Priority | Ref |
|---|------|----------|----------|----------|-----|
| 1 | [Task description] | [Name] | [Date] | H/M/L | ~MM:SS |

## Follow-Up Meetings
1. **[Meeting title]** — [Proposed date/time]
   - Led by: [Name]
   - Participants: [Names]
   - Agenda: [Topics]
   - Prep needed: [What needs to happen before]

## Open Questions & Risks
1. [Question/risk description] — raised by [Name] (ref: ~MM:SS)
   Status: Open / Needs dedicated discussion

## Discussion Summary
[2-4 paragraph factual summary of the key topics discussed, decisions made,
and reasoning behind them. Professional tone. No emotional attribution.]

## Stakeholders/Contacts Mentioned
- [Name] — [Context in which they were mentioned]
(Only new or noteworthy mentions; skip if none)
```

---

## 9. Eyal Approval Flow (Detailed)

### How It Works

1. **Gianluigi completes processing** a meeting transcript.
2. **Sends DRAFT to Eyal only** via both:
   - Telegram DM (concise version with inline preview)
   - Gmail (full formatted version with Google Drive draft link)
3. **Eyal reviews** for:
   - Factual accuracy (no hallucinated decisions or tasks)
   - Tone (no personal/emotional content slipped through)
   - Completeness (anything important missed?)
   - Sensitivity (anything that shouldn't go to the full team?)
4. **Eyal responds** via Telegram reply or email reply:
   - **"Approve"** or **"✅"** → Gianluigi distributes to team
   - **Specific edits** → "Change task 3 deadline to March 5" / "Remove the second open question, that was resolved" / "Add that we also decided to use AWS"
   - **"Reject"** → Gianluigi discards and optionally reprocesses
5. **If edits requested:** Claude processes the edit instructions, generates updated draft, sends back to Eyal for re-review. Loop until approved.
6. **Upon approval:** Output goes to Google Drive, Google Sheets (tasks), Telegram group, and email to all founders.

### Deprecation Plan
After ~10-15 successfully approved summaries, transition to:
- **Phase A:** Auto-publish with 30-minute review window. Eyal can retract/edit within 30 min.
- **Phase B:** Full auto-publish. Eyal reviews only when flagged.
- **Revert:** If quality drops at any phase, revert to manual approval.

---

## 10. Claude Tools (API Tool Definitions)

Single Claude agent with the following tools. Each tool maps to a Python function that interacts with Supabase, Google Drive, Google Sheets, or Google Calendar.

### v0.1 Tools

```
search_meetings(query: str, date_range: optional)
  → Semantic search over embedded meeting chunks
  → Returns relevant transcript excerpts with meeting metadata

get_meeting_summary(meeting_id: str)
  → Returns the full processed summary for a specific meeting

create_task(title: str, assignee: str, deadline: str, priority: str, meeting_id: optional)
  → Creates a task in Supabase + updates Google Sheet

get_tasks(assignee: optional, status: optional)
  → Returns tasks filtered by assignee and/or status

update_task(task_id: str, status: optional, deadline: optional)
  → Updates task status or deadline

ingest_transcript(file_content: str, meeting_title: str, date: str, participants: list)
  → Processes raw transcript through Claude for summary + extraction
  → Stores everything in Supabase
  → Triggers Eyal approval flow

ingest_document(content: str, title: str, source: str)
  → Summarizes document via Claude
  → Stores summary + embeddings in Supabase

search_memory(query: str)
  → Combined search: semantic (vector) + structured (SQL)
  → Returns relevant decisions, tasks, and transcript chunks

list_decisions(meeting_id: optional, topic: optional)
  → Returns decisions, optionally filtered by meeting or topic keyword

get_open_questions(status: optional)
  → Returns open questions across all meetings

get_meeting_prep(calendar_event_id: str)
  → Reads calendar event details
  → Searches memory for related past meetings, decisions, tasks
  → Reads stakeholder tracker for relevant entries
  → Compiles prep document
  → Saves to Google Drive + sends to Eyal for approval

get_stakeholder_info(name: optional, organization: optional)
  → Reads from CropSight Stakeholder Tracker Google Sheet
  → Returns matching rows
```

### v0.2 Additional Tools

```
search_gmail(query: str, max_results: int)
  → Searches Gianluigi's Gmail or Eyal's Gmail (with consent) for relevant threads

send_notification(message: str, recipients: list, channels: list)
  → Sends via Telegram + Gmail simultaneously

generate_weekly_digest()
  → Compiles: meetings this week, decisions made, tasks completed/overdue/upcoming
  → Saves to Google Drive + distributes

update_stakeholder_tracker(organization: str, updates: dict)
  → Updates existing row in Google Sheet (with Eyal approval)

create_calendar_event(title: str, date: str, participants: list, agenda: str)
  → Creates Google Calendar event (with Eyal approval)
```

---

## 11. Non-Technical Considerations & Guardrails

### 🔴 MUST DO — Before v0.1 Launch

1. **Team Consent for AI Processing**
   Tell Roye, Paolo, and Yoram explicitly that Gianluigi will process meeting transcripts. Explain what it extracts, how data is stored, and who sees what. Get verbal agreement. A WhatsApp message is sufficient:
   > "Hey team, I'm setting up an AI assistant (Gianluigi) that will process our meeting transcripts to generate summaries, track tasks, and maintain our institutional memory. It uses Claude's API (which doesn't train on our data). All outputs go through me for approval before anyone sees them. Let me know if you have any concerns."

2. **Anthropic API Data Policy**
   Claude's API does NOT use input data for model training by default. API requests may be retained for up to 30 days for trust/safety purposes only. Acceptable for internal startup tool.

3. **Supabase Data Location**
   Create Supabase project in EU region (Frankfurt). Data includes strategic decisions, investor names, client details — EU hosting is prudent for GDPR considerations given Moldova/Italy connections.

4. **Gianluigi Gmail Account**
   Create a dedicated Gmail (e.g., `gianluigi.cropsight@gmail.com`). Never use a personal Gmail account for Gianluigi's operations.

### 🟡 SHOULD DO — During v0.1 Development

5. **Data Minimization & Retention**
   - Raw transcripts: retain for 12 months, then archive/delete.
   - Vector embeddings: retain indefinitely (useful for search, not raw data).
   - Structured data (tasks, decisions): retain indefinitely.
   - Build retention awareness from day one.

6. **Access Control**
   - Eyal: admin (can delete data, access audit logs, manage settings).
   - Roye, Paolo, Yoram: user (can query, create tasks, update status).
   - All data queries are logged in the audit_log table.

7. **Source Citations for Trust**
   Every extracted decision, task, and question cites the approximate transcript timestamp. This enables verification and catches hallucinations early.

8. **Error Handling**
   - If Claude is unsure about an extraction: flag it in the draft as "[UNCERTAIN: please verify]".
   - If transcript quality is poor (bad speaker labeling, garbled text): notify Eyal and skip auto-processing.
   - If Google Drive/Supabase is down: queue actions and retry.

### 🟢 NICE TO HAVE — v0.2+

9. **External Meeting Participant Handling**
   When non-team people are in meetings, attribute their statements to role/organization rather than name. "The Moldova client contact raised concerns about..." rather than quoting named individuals.

10. **Audit Trail**
    Every Gianluigi action is logged: what was processed, what was extracted, who approved what, what was edited. Valuable for future compliance or investor due diligence.

11. **Export & Portability**
    Ensure all data can be exported from Supabase at any time (CSV/JSON). No vendor lock-in with your own tool.

---

## 12. Version Plans

### v0.1 — "Gianluigi Can Remember" (Target: 5-7 days of focused work)

**Goal:** Gianluigi becomes the team's institutional memory, task tracker, and meeting processor. All 4 founders can interact with it.

**Features:**
- [ ] Telegram bot setup (group chat + DM capability for all 4 founders)
- [ ] Gianluigi Gmail account setup with Gmail API (send + receive)
- [ ] Supabase project setup (EU region, schema from Section 3)
- [ ] Google Drive watcher: polls `Raw Transcripts/` folder for new Tactiq exports
- [ ] Transcript processing pipeline: raw transcript → Claude Opus 4.6 → structured extraction
- [ ] Custom summary format (Section 8 template) with professional tone guardrails
- [ ] Extraction: decisions, tasks (with assignee + deadline + priority), follow-up meetings (with leader), open questions
- [ ] Source citations (approximate timestamps) on every extracted item
- [ ] Eyal approval flow: draft → Eyal DM (Telegram + email) → conversational editing → approve → distribute
- [ ] Dual notification on approval: Telegram group message + email to all founders with Google Drive link
- [ ] Summary document saved to Google Drive (`Meeting Summaries/`)
- [ ] Task Tracker Google Sheet: auto-updated with extracted tasks on approval
- [ ] Semantic Q&A: "What did we decide about X?" searches embedded transcripts + structured data
- [ ] Task management via chat: create, query ("what are my open tasks?"), update status
- [ ] Document ingestion: send PDF/doc via Telegram or email → summarized → searchable
- [ ] Calendar filtering: CropSight-only meetings (Section 6 filter chain)
- [ ] Sensitivity classification: legal/investor meetings → Eyal-only output
- [ ] Personal content filter: social/personal content excluded from all outputs
- [ ] Basic audit logging of all actions
- [ ] Google Cloud Run deployment (Dockerfile + deployment script)

**Checklist before launch:**
- [ ] Tactiq workflow configured (auto-export raw transcript to Drive)
- [ ] Team notified and consent obtained (WhatsApp message)
- [ ] Supabase project created in EU region
- [ ] Google Cloud project set up with OAuth consent screen (testing mode, 4 test users)
- [ ] APIs enabled: Gmail, Drive, Calendar, Sheets
- [ ] Telegram bot created via BotFather
- [ ] Gianluigi Gmail account created
- [ ] CropSight Ops shared folder created in Google Drive
- [ ] Task Tracker Google Sheet created
- [ ] System prompt tested with sample transcript (use MVP Focus transcript as test case)
- [ ] End-to-end test: Tactiq export → processing → Eyal approval → team distribution

---

### v0.2 — "Gianluigi Is Proactive" (Target: 1-2 weeks after v0.1 is stable)

**Goal:** Gianluigi reaches out proactively, prepares the team for meetings, and sends regular digests.

**Features:**
- [ ] Meeting Prep Documents: auto-generated before calendar meetings
  - Searches past meetings, decisions, tasks related to the upcoming meeting topic
  - Reads stakeholder tracker for relevant entries
  - Checks for overdue tasks related to participants
  - Generates prep doc → Google Drive (`Meeting Prep/`) → sends to team (or Eyal-only if sensitive)
  - Trigger: daily calendar scan (morning) or manual request ("prep me for Friday's meeting")
- [ ] Gmail integration (read capability for Gianluigi's inbox)
  - Team members can email Gianluigi with questions or document uploads
  - Gianluigi processes and responds via email
- [ ] Weekly Digest (automated, every Sunday evening)
  - Meetings held this week + key decisions
  - Tasks completed / tasks overdue / tasks due next week
  - Open questions still unresolved
  - Upcoming meetings next week with brief context
  - Saved to Google Drive (`Weekly Digests/`) + sent to team
- [ ] Stakeholder Tracker updates (with Eyal approval)
  - When a new person/organization is mentioned in a transcript, Gianluigi flags it
  - Suggests an update to the existing CropSight Stakeholder Tracker Google Sheet
  - Eyal approves/rejects the suggested update
- [ ] Proactive nudges
  - Task deadline reminders: "Paolo, your MOU draft was due 2 days ago"
  - Pre-meeting reminders: "You have a call with IIA tomorrow — last time you discussed milestone 2"
- [ ] Improved approval flow: auto-publish with 30-minute review window (Phase A deprecation)

**Checklist before launch:**
- [ ] v0.1 has been running for at least 2 weeks with stable outputs
- [ ] At least 5 meeting summaries approved without major edits
- [ ] Team feedback collected on v0.1 usefulness

---

### v0.3 — "Gianluigi Is Strategic" (Target: 1 month after v0.2)

**Goal:** Gianluigi begins supporting strategic work — competitive intelligence, investor prep, and autonomous research.

**Features:**
- [ ] Competitive monitoring: periodic web search for CropSight competitors (EOS Data Analytics, Gro Intelligence, aWhere/DTN, SatYield) — summarize findings
- [ ] Investor/client meeting prep: deeper prep docs with market context, competitor positioning, financial highlights
- [ ] Gmail read access for Eyal (with consent): "Summarize my last 5 emails from the Moldova client"
- [ ] Multi-step autonomous workflows (evaluate CrewAI at this point):
  - Example: "Research Moldova wheat market news this week, cross-reference with our model, draft a client update for Paolo"
- [ ] Calendar write access: Gianluigi can create/suggest calendar events (with Eyal approval)
- [ ] Onboarding context: if a new team member joins, Gianluigi can generate a "catch-up package" covering all key decisions, open items, and context
- [ ] Full auto-publish (Phase B deprecation of approval flow, with revert capability)

---

## 13. Project Structure (Codebase)

```
gianluigi/
├── README.md
├── requirements.txt
├── Dockerfile                    # For Google Cloud Run deployment
├── .env.example                  # Template for environment variables
│
├── config/
│   ├── __init__.py
│   ├── settings.py               # All configuration, env vars, constants
│   └── team.py                   # Team emails, calendar filters, blocklists
│
├── core/
│   ├── __init__.py
│   ├── agent.py                  # Main Claude agent with tool use
│   ├── system_prompt.py          # Gianluigi's personality and instructions
│   └── tools.py                  # Tool definitions for Claude API
│
├── services/
│   ├── __init__.py
│   ├── supabase_client.py        # Supabase connection and queries
│   ├── google_drive.py           # Drive API: read/write files
│   ├── google_sheets.py          # Sheets API: task tracker, stakeholder tracker
│   ├── google_calendar.py        # Calendar API: read events, filter
│   ├── gmail.py                  # Gmail API: send/receive from Gianluigi's account
│   ├── embeddings.py             # Text embedding (Voyage/OpenAI)
│   └── telegram_bot.py           # Telegram bot handlers
│
├── processors/
│   ├── __init__.py
│   ├── transcript_processor.py   # Parse Tactiq exports, extract structured data
│   ├── document_processor.py     # PDF/doc ingestion and summarization
│   ├── meeting_prep.py           # Generate meeting prep documents
│   └── weekly_digest.py          # Generate weekly summaries
│
├── guardrails/
│   ├── __init__.py
│   ├── calendar_filter.py        # CropSight vs personal meeting filter
│   ├── sensitivity_classifier.py # Normal vs sensitive meeting classification
│   ├── content_filter.py         # Personal content removal from outputs
│   └── approval_flow.py          # Eyal approval routing and edit processing
│
├── models/
│   ├── __init__.py
│   └── schemas.py                # Pydantic models for all data types
│
└── scripts/
    ├── setup_supabase.sql        # Database schema creation script
    ├── setup_google.md           # Step-by-step Google API setup guide
    └── deploy.sh                 # Cloud Run deployment script
```

---

## 14. Environment Variables Required

```bash
# Claude API
ANTHROPIC_API_KEY=

# Supabase
SUPABASE_URL=
SUPABASE_KEY=

# Google APIs (OAuth credentials)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_GROUP_CHAT_ID=
TELEGRAM_EYAL_CHAT_ID=

# Gmail
GIANLUIGI_EMAIL=gianluigi.cropsight@gmail.com

# Google Drive
CROPSIGHT_OPS_FOLDER_ID=
RAW_TRANSCRIPTS_FOLDER_ID=
MEETING_SUMMARIES_FOLDER_ID=
MEETING_PREP_FOLDER_ID=
WEEKLY_DIGESTS_FOLDER_ID=

# Google Sheets
TASK_TRACKER_SHEET_ID=
STAKEHOLDER_TRACKER_SHEET_ID=

# Embeddings
EMBEDDING_API_KEY=
EMBEDDING_MODEL=text-embedding-3-small

# Team Configuration
EYAL_EMAIL=
ROYE_EMAIL=
PAOLO_EMAIL=
YORAM_EMAIL=

# Calendar
CROPSIGHT_CALENDAR_COLOR_ID=
```

---

## 15. Development Sequence (Suggested Build Order)

Build in this order to have a working end-to-end flow as early as possible:

### Phase A: Foundation (Day 1-2)
1. `config/settings.py` — environment variables and constants
2. `config/team.py` — team emails, filter lists
3. `services/supabase_client.py` — connection + basic CRUD
4. `scripts/setup_supabase.sql` — run to create all tables
5. `models/schemas.py` — Pydantic models
6. `core/system_prompt.py` — Gianluigi's full system prompt with guardrails

### Phase B: Core Processing (Day 2-3)
7. `services/embeddings.py` — text chunking and embedding
8. `processors/transcript_processor.py` — parse Tactiq exports + Claude extraction
9. `core/tools.py` — define all tool schemas
10. `core/agent.py` — Claude agent with tool use loop

### Phase C: Interfaces (Day 3-4)
11. `services/telegram_bot.py` — basic bot with message handling
12. `services/gmail.py` — send/receive via Gmail API
13. `services/google_drive.py` — read/write files, watch folder
14. `services/google_sheets.py` — read stakeholder tracker, write task tracker

### Phase D: Guardrails & Flows (Day 4-5)
15. `guardrails/calendar_filter.py` — CropSight meeting detection
16. `guardrails/sensitivity_classifier.py` — normal vs sensitive
17. `guardrails/content_filter.py` — personal content removal
18. `guardrails/approval_flow.py` — Eyal review routing + edit processing

### Phase E: Integration & Deploy (Day 5-7)
19. Wire everything together in `main.py`
20. End-to-end test with MVP Focus transcript
21. `Dockerfile` + `deploy.sh` for Cloud Run
22. Deploy and test with real meeting

---

## 16. Test Case: MVP Focus Meeting (Feb 22, 2026)

Use the uploaded Tactiq transcript (`CropSight__MVP_focus.txt`) as the primary test case. Expected outputs:

### Expected Decisions (5)
1. MVP = web-accessible interface running 24/7 showing predicted yields
2. MVP input: weather-only (no multimodality)
3. Single deep-learning model, single method for unobservable features
4. No contractual accuracy guarantees in MVP phase
5. Cloud provider: AWS preferred (Roye), revisitable later

### Expected Tasks (7)
1. [Roye] Write 1-page accuracy abstract — due: before Feb 27 | Priority: H
2. [Eyal] Send calendar invite for Feb 27, 16:00 IST | Priority: H
3. [Roye] Send calendar invitation for Feb 27 | Priority: M
4. [Yoram/Team] Engage security reviewer (Edo or equivalent) | Priority: M
5. [Eyal + Paolo] Confirm MVP expectations with Moldova pilot (Rita) | Priority: H
6. [Team] Plan cloud provider choice + prepare AWS account/IAM | Priority: M
7. [Team] Assess staffing needs for post-MVP maintenance | Priority: L

### Expected Follow-Up Meetings (2)
1. **Accuracy & Founders Agreement** — Friday Feb 27, 16:00 IST (15:00 Paolo)
   - Led by: Eyal (organizer)
   - Participants: All founders
   - Agenda: Time-to-accuracy discussion, client acquisition process, founders' agreement review
   - Prep needed: Roye sends accuracy abstract before meeting
2. **Eyal + Paolo Sync** — Sunday Feb 23, 17:00 CET / 18:00 IST
   - Led by: Paolo
   - Agenda: Commodity trader analysis review, parallel workstreams

### Expected Open Questions (4)
1. Time to stable, accurate predictions per client — needs dedicated session
2. Client definition mismatch — ensure agreement on what "MVP" means to Moldova
3. Time-to-market concern (Paolo) — may hinder fundraising if too slow
4. Security liability exposure with minimal MVP protections

### Content That Should Be EXCLUDED
- Roye's mention of upcoming wedding and personal time constraints (only business impact: "availability constraints")
- Paolo's compliment about Roye's suit
- Social banter at end of meeting
- Any emotional characterizations

---

## Appendix A: Gianluigi System Prompt (Draft)

```
You are Gianluigi, CropSight's AI operations assistant. You serve the founding 
team: Eyal (CEO), Roye (CTO), Paolo (BD), and Prof. Yoram Weiss (Senior Advisor).

CropSight is an Israeli AgTech startup building ML-powered crop yield forecasting 
using neural networks on satellite imagery, climate data, and agronomic parameters. 
The company is pre-revenue, PoC stage with a first client in Moldova (Gagauzia 
region, wheat), funded by IIA Tnufa program. Model accuracy: 85-91%.

YOUR ROLE:
- Process meeting transcripts into structured, professional summaries
- Track tasks, decisions, and open questions across meetings
- Maintain institutional memory that the team can query
- Prepare briefing documents before upcoming meetings
- Send notifications and updates via Telegram and email

COMMUNICATION STYLE:
- Professional, concise, and clear
- Friendly but not casual — you're a team member, not a chatbot
- When uncertain, say so. Never fabricate information.
- Always cite source timestamps when referencing transcript content

MANDATORY GUARDRAILS:
[Insert full Section 8 tone guardrails here]

TOOLS AVAILABLE:
[Insert tool definitions here]

APPROVAL FLOW:
All meeting summaries, task extractions, and prep documents must be routed to 
Eyal for approval before distribution to the team. When Eyal requests edits, 
process them and return the updated draft for re-review.

CALENDAR RULES:
Only process meetings that pass the CropSight filter (purple color, 2+ team 
members, or CropSight title prefix). If uncertain, ask Eyal. Never process 
personal meetings.

SENSITIVITY RULES:
Meetings involving lawyers, investors, NDAs, or founders agreement discussions 
are classified as sensitive. Output goes to Eyal only, not the team.
```

---

## Appendix B: Key Contacts & References

| Name | Role | Context |
|------|------|---------|
| Eyal Zror | CEO | Primary Gianluigi admin |
| Roye Tadmor | CTO | Bioinformatics, model dev, deployment lead |
| Paolo Vailetti | BD | Based in Italy, client acquisition, fundraising |
| Prof. Yoram Weiss | Senior Advisor | Strategic guidance, hospital-based |
| Edo | Security Reviewer | Recommended by Yoram for MVP security review |
| Rita / Orit | Moldova Client Contact | Gagauzia wheat pilot |
| Zohar | Lawyer (Fischer/FBC) | Founders agreement, company formation |
| Liviu | Introducer | Orit-related, Moldovan businessman in Germany |
| Chen | GrowingIL | NGO, AgTech client leads & recruitment |

---

*End of document. This file should be added to the Claude Code project knowledge for reference during development.*
