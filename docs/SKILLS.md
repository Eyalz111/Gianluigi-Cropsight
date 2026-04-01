# Gianluigi Skills Manifest

**Version:** 1.0
**Last Updated:** April 2, 2026

This document defines Gianluigi's capabilities as discrete, modular skills. Each skill is a self-contained capability that can be independently tested, monitored, and evolved.

---

## Skill Format

Each skill is documented with:
- **Name**: Short identifier
- **Description**: What it does in one sentence
- **Trigger**: What activates it (schedule, event, MCP tool, Telegram command)
- **Inputs**: What data it needs
- **Outputs**: What it produces
- **Dependencies**: Other skills or services it requires
- **Cost**: Approximate LLM cost per invocation

---

## Core Skills

### 1. Transcript Processing
- **Trigger**: New Tactiq transcript detected in Google Drive
- **Inputs**: Raw transcript text, meeting metadata
- **Outputs**: Meeting record, decisions, tasks, open questions, embeddings
- **Dependencies**: Claude Opus (extraction), OpenAI (embeddings), Supabase
- **Cost**: ~$0.10-0.30 per transcript (Opus extraction + embeddings)

### 2. Cross-Reference Analysis
- **Trigger**: After transcript extraction
- **Inputs**: New tasks/decisions, existing open tasks
- **Outputs**: Dedup classifications, status changes, question resolutions, supersessions
- **Dependencies**: Claude Haiku (dedup), Claude Sonnet (supersession), Supabase
- **Cost**: ~$0.01-0.03 per meeting

### 3. Approval Flow
- **Trigger**: After extraction, or on Telegram approve/reject
- **Inputs**: Extracted data, CEO decision
- **Outputs**: Approved/edited summaries, team distributions
- **Dependencies**: Telegram, Supabase, Google Sheets

### 4. Morning Brief
- **Trigger**: Daily at 07:00 IST
- **Inputs**: Email scans, calendar, alerts, pending items, QA report
- **Outputs**: Consolidated brief sent to CEO Telegram DM
- **Dependencies**: Gmail, Calendar, Supabase, proactive alerts
- **Cost**: ~$0.005 (Haiku classification only)

### 5. Meeting Prep
- **Trigger**: On-demand or before scheduled meetings
- **Inputs**: Calendar event, participant list, meeting title
- **Outputs**: Prep document (Markdown/DOCX) with context, decisions, tasks, stakeholders
- **Dependencies**: Calendar, Supabase (RAG search), Claude Sonnet (synthesis)
- **Cost**: ~$0.02-0.05 per prep document

### 6. Evening Debrief
- **Trigger**: Daily at 18:00 IST (prompt), on-demand (session)
- **Inputs**: Quick injection text or interactive conversation
- **Outputs**: Stored debrief data, optional task creation
- **Dependencies**: Telegram, Claude Sonnet (conversation)
- **Cost**: ~$0.01 per prompt, ~$0.05-0.10 per interactive session

### 7. Weekly Review
- **Trigger**: MCP tool or Telegram redirect
- **Inputs**: Week's meetings, tasks, decisions, Gantt status
- **Outputs**: 3-part interactive review session, HTML report, Gantt proposals
- **Dependencies**: Supabase, Gantt, Claude Sonnet (session)
- **Cost**: ~$0.10-0.20 per review session

### 8. Email Intelligence
- **Trigger**: Constant layer (2h interval) + daily personal scan (07:00 IST)
- **Inputs**: Gmail inbox, classification rules
- **Outputs**: Classified emails, extracted items, body storage
- **Dependencies**: Gmail API, Claude Haiku (classification/extraction)
- **Cost**: ~$0.005-0.02 per scan cycle

### 9. Document Ingestion
- **Trigger**: Email attachment, Drive upload, Dropbox sync
- **Inputs**: Document content (text/PDF/DOCX)
- **Outputs**: Document record, summary, embeddings, version tracking
- **Dependencies**: OpenAI (embeddings), Claude Haiku (classification/summary), Drive
- **Cost**: ~$0.01-0.05 per document

### 10. RAG Search
- **Trigger**: MCP query or Telegram question
- **Inputs**: Natural language query
- **Outputs**: Relevant chunks with sources, time-weighted scoring
- **Dependencies**: OpenAI (query embedding), Supabase (pgvector + fulltext)
- **Cost**: ~$0.001 per query (embedding only)

---

## Intelligence Skills (Phase 12)

### 11. Meeting Continuity
- **Trigger**: Before extraction (context) or morning brief (daily context)
- **Inputs**: Participant list, meeting title
- **Outputs**: Cross-meeting context with task stats, decision reviews, question aging
- **Dependencies**: Supabase, Claude Sonnet (pre-meeting synthesis)
- **Cost**: ~$0.01-0.02 per context build

### 12. Decision Freshness
- **Trigger**: Passive (on decision query/reference)
- **Inputs**: Decision ID
- **Outputs**: Updated last_referenced_at timestamp, stale decision surfacing in weekly review
- **Dependencies**: Supabase
- **Cost**: $0 (DB operations only)

### 13. Task Signal Detection
- **Trigger**: Email processing, Gantt changes, calendar events
- **Inputs**: External data (emails, Gantt diffs, calendar events)
- **Outputs**: Task signal records for future analysis
- **Dependencies**: Supabase
- **Cost**: $0 (DB operations only)

### 14. Decision Chain Traversal
- **Trigger**: MCP tool (get_decision_chain)
- **Inputs**: Decision ID
- **Outputs**: Full chain of predecessor/successor decisions
- **Dependencies**: Supabase
- **Cost**: $0 (DB operations only)

---

## System Skills

### 15. QA Agent
- **Trigger**: Daily at 06:00 IST + on-demand MCP tool
- **Inputs**: System state (meetings, approvals, heartbeats, data)
- **Outputs**: Health report with score (healthy/warning/critical), issues list
- **Dependencies**: Supabase
- **Cost**: $0 (DB queries only)

### 16. Proactive Alerts
- **Trigger**: Post-meeting, morning brief, or standalone check
- **Inputs**: Open tasks, entity mentions, question pileups
- **Outputs**: Alert notifications (CRITICAL → DM, WARNING → batched)
- **Dependencies**: Supabase, Telegram
- **Cost**: $0 (DB queries only)

### 17. Dropbox Sync
- **Trigger**: Every 2 hours (when enabled)
- **Inputs**: Dropbox folder contents
- **Outputs**: Mirrored files in Google Drive, sync tracking records
- **Dependencies**: Dropbox SDK, Google Drive API, Supabase
- **Cost**: $0 (API calls only, no LLM)

---

## MCP Interface (38 tools)

All skills are accessible via Claude.ai MCP server. Tools are grouped by category prefix:
- `[TASKS]` — get_tasks, create_task, update_task
- `[DECISIONS]` — get_decisions, update_decision, get_decisions_for_review, get_decision_chain
- `[QUESTIONS]` — get_open_questions
- `[MEETINGS]` — get_meeting_history, get_upcoming_meetings
- `[GANTT]` — get_gantt_status, get_gantt_horizon, get_gantt_metrics, propose_gantt_update, approve_gantt_proposal
- `[TOPICS]` — get_topic_thread, list_topic_threads, merge_topic_threads, rename_topic_thread
- `[SYSTEM]` — get_system_context, get_system_health, get_cost_summary, get_full_status, run_qa_check
- `[SESSION]` — get_last_session_summary, save_session_summary, search_memory
- `[REVIEW]` — get_weekly_summary, start_weekly_review, confirm_weekly_review
- `[OPERATIONS]` — quick_inject, confirm_quick_inject, get_pending_approvals, get_stakeholder_info, get_commitments
- `[PROJECTS]` — list_canonical_projects, add_canonical_project
