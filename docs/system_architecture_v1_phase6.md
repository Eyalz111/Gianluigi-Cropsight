# Gianluigi System Architecture — Post Phase 6 (Weekly Review + Outputs)

**Date:** March 18, 2026
**Version:** v1.0 (Phase 0-6 complete)
**Tests:** 1246 passing (+165 from Phase 6)
**Deployed:** Cloud Run (europe-west1, 512Mi, min-instances=1)

---

## 1. HIGH-LEVEL ARCHITECTURE

```
                    +---------------------------------------------+
                    |           EXTERNAL INPUTS                    |
                    +------+------+------+------+------+----------+
                    |Tactiq|Gmail |Google|Google|Google |  Google  |
                    |(Meet)|  API |Drive | Cal  |Sheets |  Cal     |
                    +--+---+--+---+--+---+--+---+--+---+----+-----+
                       |      |      |      |      |        |
                    +--v------v------v------v------v--------v-----+
                    |            SCHEDULER LAYER                  |
                    |    (9 background asyncio tasks)              |
                    |                                             |
                    |  transcript_watcher (30s poll Drive)        |
                    |  document_watcher (5min poll Drive)         |
                    |  email_watcher (5min poll Gmail)            |
                    |  personal_email_scanner (daily 7am)         |
                    |  morning_brief_scheduler (daily 7am)        |
                    |  meeting_prep_scheduler (hourly)            |
                    |  weekly_review_scheduler (15min)     [NEW]  |
                    |    OR weekly_digest_scheduler (Sun)         |
                    |  orphan_cleanup_scheduler (6hr)             |
                    +-----------------------+---------------------+
                                            |
                    +-----------------------v---------------------+
                    |           PROCESSOR LAYER                   |
                    |                                             |
                    |  transcript_processor (Opus extract)        |
                    |  email_classifier (Haiku/Sonnet)            |
                    |  morning_brief (compile + review mention)   |
                    |  cross_reference (dedup/status/Q)           |
                    |  entity_extraction (Haiku 2-pass)           |
                    |  proactive_alerts (4 SQL detectors)         |
                    |  debrief (interactive session + prep check) |
                    |  document_ingestion (Word/PDF)              |
                    |  meeting_prep (outline + v2 doc)            |
                    |  meeting_type_matcher (scoring)             |
                    |  weekly_digest_generator (Sonnet)           |
                    |  weekly_review (data compilation)    [NEW]  |
                    |  weekly_review_session (3-part flow) [NEW]  |
                    |  weekly_report (HTML generation)     [NEW]  |
                    |  gantt_slide (PPTX generation)       [NEW]  |
                    +-----------------------+---------------------+
                                            |
                    +-----------------------v---------------------+
                    |           GUARDRAILS LAYER                  |
                    |                                             |
                    |  approval_flow (8 content types now)        |
                    |  inbound_filter (5-layer security)          |
                    |  calendar_filter (CropSight only)           |
                    |  content_filter (sensitivity tags)          |
                    |  sensitivity_classifier (distribution)      |
                    |  gantt_guard (schema validation)            |
                    +-----------------------+---------------------+
                                            |
                    +-----------------------v---------------------+
                    |            CORE BRAIN                       |
                    |                                             |
                    |  Multi-Agent System:                        |
                    |    Router (Haiku) -> classify intent        |
                    |    Conversation Agent (Sonnet)              |
                    |    Analyst Agent (Opus) -> deep work        |
                    |    Operator Agent (Sonnet) -> actions       |
                    |                                             |
                    |  RAG: semantic + fulltext + RRF             |
                    |  Tools: 12 callable functions               |
                    |  LLM: centralized via core/llm.py          |
                    |  Weekly Review Prompts (Sonnet)      [NEW]  |
                    +-----------------------+---------------------+
                                            |
                    +-----------------------v---------------------+
                    |           INTERFACES                        |
                    |                                             |
                    |  Telegram Bot (primary daily UI)            |
                    |    + Session stack (review + debrief)  [NEW]|
                    |    + Inline keyboard (prep/review buttons)  |
                    |    + Focus input (prep discussion)          |
                    |  Health Server (Cloud Run HTTP)             |
                    |    + /reports/weekly/{token}           [NEW]|
                    |  Gmail (send approved content)              |
                    |  Google Sheets (task/Gantt write)           |
                    |  Google Drive (summaries + preps + reports) |
                    +---------------------------------------------+
                                            |
                    +-----------------------v---------------------+
                    |           STORAGE                           |
                    |                                             |
                    |  Supabase (PostgreSQL + pgvector)           |
                    |  Tables: meetings, tasks, decisions,        |
                    |    embeddings, entities, commitments,       |
                    |    email_scans, pending_approvals,          |
                    |    gantt_schema, gantt_proposals,           |
                    |    debrief_sessions, documents,             |
                    |    calendar_classifications, meetings,      |
                    |    meeting_prep_history,                    |
                    |    weekly_review_sessions          [NEW]    |
                    |    weekly_reports (+ html, token)  [MOD]    |
                    +---------------------------------------------+
```

---

## 2. DAILY RHYTHM (Post Phase 6)

```
TIME (IST)  EVENT                              COMPONENT
----------  ---------------------------------  --------------------------
07:00       Morning Brief                      morning_brief_scheduler
            +-- Run personal email scan        personal_email_scanner
            +-- Collect overnight constant      email_watcher (queued)
            |   layer extractions
            +-- Fetch today's calendar          google_calendar
            +-- Check pending prep outlines     supabase
            +-- Check weekly review session     supabase [NEW]
            +-- Compile into ONE message        morning_brief processor
            +-- Send to Eyal for approval       approval_flow -> Telegram

T-24h       Prep Outline Proposal              meeting_prep_scheduler
before      +-- Classify meeting type           meeting_type_matcher
meeting     +-- Generate structured outline     meeting_prep
            +-- Send outline to Eyal            telegram_bot
            +-- Schedule reminders + auto-gen   asyncio tasks

All day     Continuous monitoring:
            +-- Transcript watcher (30s)        new meeting recordings
            +-- Email watcher (5min)            team emails to Gianluigi
            +-- Document watcher (5min)         new uploads to Drive
            +-- Weekly review scheduler (15m)   detect review events [NEW]

~18:00      End-of-Day Debrief
            +-- Surface pending items           debrief processor
            +-- Interactive conversation
            +-- Quick injection / full debrief
            +-- If weekly review active:        session stack resume [NEW]
            |   offer "Resume weekly review?"

Friday      Weekly Review                      weekly_review_scheduler [NEW]
T-3h        +-- Detect calendar event           _find_review_event
            |   (fuzzy + Haiku title match)
            +-- Compile 5-section data          weekly_review.compile_*
            +-- Pre-generate HTML report        weekly_report
            +-- Pre-generate PPTX slide         gantt_slide
            +-- Create session (status=ready)   supabase

Friday      Notification                       weekly_review_scheduler [NEW]
T-30min     +-- Send Telegram DM to Eyal
            |   with report link + "/review"

Friday      Interactive Review Session         weekly_review_session [NEW]
            +-- Eyal sends /review
            +-- Part 1: "Here's your week"
            |   (stats, alerts, horizon)
            +-- Part 2: "Decisions needed"
            |   (Gantt proposals, next week)
            +-- Part 3: "Outputs"
            |   (generate, correct, approve)
            +-- Approve & Distribute
            |   Gantt first -> Drive -> email -> Telegram
```

---

## 3. PHASE 6: WEEKLY REVIEW FLOW (NEW)

This is the core Phase 6 addition — the calendar-triggered weekly review with interactive 3-part session:

```
Calendar event: "CropSight: Weekly Review with Gianluigi"
      |
      v
weekly_review_scheduler._check_cycle() (every 15 min)
      |
      +-- _find_review_event():
      |     exact title match -> yes
      |     fuzzy word match (60%+) -> yes
      |     non-Latin title -> Haiku fallback
      |     cancelled event -> skip
      |
      +-- T-3h: _trigger_prep(event)
      |     compile_weekly_review_data()
      |       +-- _compile_week_in_review()
      |       +-- _compile_gantt_proposals()
      |       +-- _compile_attention_needed()
      |       +-- _compile_next_week_preview()
      |       +-- _compile_horizon_check()
      |     generate_html_report() -> per-report token URL
      |     generate_gantt_slide() -> PPTX bytes
      |     create session (status=ready)
      |
      +-- T-30min: _send_notification()
      |     Telegram DM: "Review starts in 30 min"
      |     + report preview link
      |     + "/review when ready"
      |
      v
Eyal sends /review (or natural message)
      |
      v
start_weekly_review()
      |
      +-- Resume same-week session if exists
      +-- Cancel stale different-week session
      +-- If no pre-compiled data (manual /review),
      |   compile on the fly
      |
      v
PART 1: "Here's your week"
      |
      +-- Week stats (meetings, decisions, tasks)
      +-- Attention needed (overdue, stale tasks)
      +-- Horizon check (milestones, red flags)
      +-- Eyal reads, asks questions
      |
      +-- [Continue >>] button
      |
      v
PART 2: "Decisions needed"
      |
      +-- Gantt update proposals
      |   [Approve] [Reject] per proposal
      +-- Next week preview (calendar, deadlines)
      +-- Eyal adds items, reprioritizes
      |
      +-- [Continue >>] [<< Go back] buttons
      |
      v
PART 3: "Outputs"
      |
      +-- Generate/display PPTX + HTML + digest
      +-- Correction loop (max 10):
      |   Eyal: "Change X to Y"
      |   Sonnet parses -> regenerate affected output
      |
      +-- [Approve & Distribute] [Regenerate] [Cancel]
      |
      v
confirm_review(approved=True) — ATOMIC DISTRIBUTION
      |
      +-- 1. Execute approved Gantt proposals
      |     If ANY fail -> HOLD everything
      |     Eyal chooses: [Distribute anyway] [Hold]
      |
      +-- 2. Backup Gantt (post-write)
      |
      +-- 3. Upload PPTX to Drive (GANTT_SLIDES_FOLDER_ID)
      +-- 4. Upload digest to Drive
      +-- 5. Update weekly_reports (status=distributed)
      +-- 6. Email to team (sensitivity-aware)
      +-- 7. Telegram group notification
      +-- 8. Audit trail
```

---

## 4. SESSION STACK (Debrief Interrupts Review)

```
SCENARIO: Eyal is in Part 2 of weekly review. Sends /debrief.

Stack before:     ["weekly_review"]
Stack after push: ["weekly_review", "debrief"]
Active session:   "debrief"  (top of stack)

    Eyal completes debrief
    -> debrief approved
    -> pop "debrief" from stack
    -> Stack: ["weekly_review"]
    -> Gianluigi: "Resume weekly review?"
       [Yes] -> resume_after_debrief(session_id)
               -> refresh agenda_data (debrief may have added items)
               -> continue from last part
       [No]  -> pop "weekly_review" from stack

Backward compat:
    _active_interactive_session property
    -> returns stack[-1] or None (getter)
    -> pushes/pops on set (setter)
    Existing debrief code works unchanged.
```

---

## 5. HTML REPORT SERVING

```
ARCHITECTURE:

  Cloud Run Health Server (aiohttp, port 8080)
    |
    +-- GET /health           -> 200 always (liveness)
    +-- GET /ready            -> 200/503 (readiness)
    +-- GET /reports/weekly/{access_token}  [NEW]
        |
        +-- Look up weekly_reports by access_token
        +-- Return html_content as text/html
        +-- 404 if not found / empty

  Security:
    - Each report gets its own secrets.token_urlsafe(32) token
    - URL: https://gianluigi.run.app/reports/weekly/{token}
    - If one token leaks, only that single report is exposed
    - Replaces previous global REPORTS_SECRET_TOKEN approach

  Storage:
    - HTML stored in Supabase (weekly_reports.html_content)
    - NOT filesystem (Cloud Run containers are ephemeral)
    - Self-contained HTML (inline CSS, no external resources)
```

---

## 6. PPTX GANTT SLIDE

```
LAYOUT: Table-based (one table per Gantt section)

  +------------------------------------------------------------+
  | CropSight Operational Gantt — Week 12, 2026                 |
  | Gianluigi-generated                                        |
  +------------------------------------------------------------+
  | Strategic Milestones | Owner | W10 | W11 |*W12*| W13 | ... |
  |   * MVP Release      | [E]   |     |     | ███ | ░░░ |     |
  +------------------------------------------------------------+
  | Product & Tech       | Owner | W10 | W11 |*W12*| W13 | ... |
  |   Yield Model v2     | [R]   |     | ███ | ███ | ░░░ |     |
  |   API Gateway        | [R]   |     |     |     | ░░░ |     |
  +------------------------------------------------------------+
  | (etc. for Sales & BD, Fundraising, Legal & Finance)         |
  +------------------------------------------------------------+
  | Owners: [E]Eyal [R]Roye [P]Paolo [Y]Yoram                  |
  | Milestones: * Tech  . Commercial  + Funding                |
  | Colors: Green=Active Blue=Planned Red=Blocked Gray=Done     |
  +------------------------------------------------------------+
  | CropSight — Confidential | Generated by Gianluigi           |
  +------------------------------------------------------------+

  Color mapping:
    active    -> green  (#4CAF50)
    planned   -> blue   (#2196F3)
    blocked   -> red    (#F44336)
    completed -> gray   (#9E9E9E)
    delayed   -> orange (#FF9800)
    current week column -> light green highlight
```

---

## 7. DATA MODEL (5 Sections, MCP-Ready)

```
compile_weekly_review_data() returns:

{
  "week_in_review": {
    meetings_count, decisions_count,
    meetings, decisions, task_summary,
    commitment_scorecard,
    debrief_count, email_scan_count
  },
  "gantt_proposals": {
    proposals, count
  },
  "attention_needed": {
    stale_tasks, alerts,
    approaching_milestones
  },
  "next_week_preview": {
    upcoming_meetings, deadlines,
    gantt_items_due, priorities
  },
  "horizon_check": {
    milestones, red_flags
  },
  "meta": {
    week_number, year, compiled_at,
    data_sources
  }
}

Telegram: 3 parts (condensed for mobile UX)
  Part 1: week_in_review + attention_needed + horizon_check
  Part 2: gantt_proposals + next_week_preview
  Part 3: outputs (generate/correct/approve)

Phase 7 MCP/Claude.ai: Full 7-part presentation
  Same data, richer interface
```

---

## 8. APPROVAL FLOW (Updated — 8 Content Types)

```
CONTENT TYPES:
  +------------------------+-------------------------------------------+
  | Content Type           | What happens on approval                  |
  +------------------------+-------------------------------------------+
  | meeting_summary        | Distribute: Telegram group, Gmail,        |
  |                        | Sheets, Drive (.md + .docx)               |
  +------------------------+-------------------------------------------+
  | prep_outline           | Telegram-only. Eyal interacts via inline  |
  |                        | buttons. On generate: creates meeting_prep|
  +------------------------+-------------------------------------------+
  | meeting_prep           | Sensitivity-aware distribution:           |
  |                        | sensitive -> Eyal + Drive + note           |
  |                        | normal -> team (Telegram + email + Drive) |
  +------------------------+-------------------------------------------+
  | weekly_digest          | Send to Eyal only (Telegram + Email)      |
  +------------------------+-------------------------------------------+
  | weekly_review    [NEW] | Gantt-first atomic distribution:          |
  |                        | execute Gantt proposals -> backup ->      |
  |                        | PPTX to Drive -> digest to Drive ->       |
  |                        | email team -> Telegram group              |
  +------------------------+-------------------------------------------+
  | gantt_update           | Write changes to Google Sheets            |
  +------------------------+-------------------------------------------+
  | morning_brief          | Mark scans approved, inject items         |
  |                        | to DB + RAG. Shows pending preps + review |
  +------------------------+-------------------------------------------+
  | debrief                | Inject items to DB + RAG                  |
  +------------------------+-------------------------------------------+

  Expiry:
    morning_brief   -> 24 hours
    debrief         -> 48 hours
    prep_outline    -> 24 hours (auto-gen if meeting still future)
    weekly_digest   -> 7 days
    weekly_review   -> 7 days [NEW]
```

---

## 9. NEW/MODIFIED FILES (Phase 6)

```
gianluigi/
+-- config/
|   +-- settings.py                  [MOD] 6 new weekly review settings
+-- core/
|   +-- weekly_review_prompt.py      [NEW] System prompts for review Sonnet
+-- processors/
|   +-- weekly_review.py             [NEW] Data compilation (5 sections)
|   +-- weekly_review_session.py     [NEW] Interactive 3-part session
|   +-- weekly_report.py             [NEW] HTML report generator
|   +-- gantt_slide.py               [NEW] PPTX slide generator
|   +-- morning_brief.py             [MOD] Weekly review mention
+-- schedulers/
|   +-- weekly_review_scheduler.py   [NEW] Calendar-driven scheduler
+-- services/
|   +-- telegram_bot.py              [MOD] Session stack, /review command,
|   |                                      review callbacks, /status review
|   +-- supabase_client.py           [MOD] 9 new methods (sessions, reports,
|   |                                      stale tasks, proposals)
|   +-- health_server.py             [MOD] /reports/weekly/{token} route
+-- guardrails/
|   +-- approval_flow.py             [MOD] weekly_review content type,
|   |                                      distribute_approved_review()
+-- models/
|   +-- schemas.py                   [MOD] WeeklyReviewStatus enum,
|   |                                      WeeklyReviewSession model,
|   |                                      WeeklyReport extended
+-- templates/
|   +-- weekly_report.html           [NEW] Jinja2 self-contained HTML
+-- main.py                          [MOD] Scheduler coexistence logic
+-- scripts/
|   +-- migrate_phase6.sql           [NEW] Schema migration
+-- tests/
    +-- test_weekly_review_models.py     [NEW] 23 tests
    +-- test_weekly_review_data.py       [NEW] 23 tests
    +-- test_weekly_review_session.py    [NEW] 35 tests
    +-- test_weekly_report_html.py       [NEW] 16 tests
    +-- test_gantt_slide.py              [NEW] 17 tests
    +-- test_weekly_review_scheduler.py  [NEW] 38 tests
    +-- test_weekly_review_integration.py[NEW] 13 tests
    +-- test_morning_brief.py            [MOD] 1 test updated
```

---

## 10. LLM COST: WEEKLY REVIEW

```
STEP                          MODEL        COST
----------------------------  -----------  -----------
Data compilation (summaries)  Sonnet x2    ~$0.04
Gantt proposal summaries      Sonnet x1-3  ~$0.03-0.09
Session conversation (3 pts)  Sonnet x5-10 ~$0.10-0.20
Navigation classification    Haiku x5-10  ~$0.002
Correction parsing            Sonnet x0-3  ~$0.00-0.09
Calendar title matching       Haiku x0-1   ~$0.001
                                           -----------
TOTAL PER WEEKLY REVIEW                    ~$0.20-0.45
```

---

## 11. TELEGRAM COMMANDS (Updated)

```
COMMAND          DESCRIPTION
---------------  ------------------------------------------
/start           Welcome message + status
/help            Show all commands
/mytasks         Show open tasks for Eyal
/tasks           Show all open tasks
/decisions       Show recent decisions
/status          System health + pending preps + review state [ENHANCED]
/digest          Generate weekly digest now
/review          Start weekly review session [NEW]
/reprocess <id>  Reprocess a meeting transcript
/gantt           Show current Gantt chart state
/debrief         Start end-of-day debrief session
/emailscan       Trigger manual email scan (1/day limit)

INLINE BUTTONS:
  Review Part 1-2:
    [Continue >>]  [<< Go back]  [End review]
  Review Part 2 (per Gantt proposal):
    [Approve]  [Reject]  [Edit]
  Review Part 3:
    [Approve & Distribute]  [Edit]  [Regenerate]  [Cancel]
  Prep outline:
    [Generate as-is]  [Add focus]  [Wrong type?]  [Skip]

FREE TEXT:       Ask any question — routed through multi-agent system
REVIEW INPUT:    Free-text questions during review session
CORRECTIONS:     "Change X to Y" in Part 3 correction mode
```

---

## 12. REMAINING PHASES

```
PHASE    DESCRIPTION                    STATUS
-------  ---------------------------    --------
0-5      Foundation through prep        COMPLETE
6        Weekly review + outputs         COMPLETE
7        MCP server (Claude.ai)         NEXT
8        Heartbeat unification          PLANNED
9        Integration testing            PLANNED
```
