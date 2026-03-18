# Gianluigi System Architecture — Post Phase 5 (Meeting Prep Redesign)

**Date:** March 16, 2026
**Version:** v1.0 (Phase 0-5 complete)
**Tests:** 1067 passing (+134 from Phase 5)
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
                    |    (8 background asyncio tasks)              |
                    |                                             |
                    |  transcript_watcher (30s poll Drive)        |
                    |  document_watcher (5min poll Drive)         |
                    |  email_watcher (5min poll Gmail)            |
                    |  personal_email_scanner (daily 7am)         |
                    |  morning_brief_scheduler (daily 7am)        |
                    |  meeting_prep_scheduler (hourly) [REWRITTEN]|
                    |  weekly_digest_scheduler (Sun 18:00)        |
                    |  orphan_cleanup_scheduler (6hr)             |
                    +-----------------------+---------------------+
                                            |
                    +-----------------------v---------------------+
                    |           PROCESSOR LAYER                   |
                    |                                             |
                    |  transcript_processor (Opus extract)        |
                    |  email_classifier (Haiku/Sonnet)            |
                    |  morning_brief (compile + format + preps)   |
                    |  cross_reference (dedup/status/Q)           |
                    |  entity_extraction (Haiku 2-pass)           |
                    |  proactive_alerts (4 SQL detectors)         |
                    |  debrief (interactive session + prep check) |
                    |  document_ingestion (Word/PDF)              |
                    |  meeting_prep (outline + v2 doc) [NEW]      |
                    |  meeting_type_matcher (scoring)  [NEW]      |
                    |  weekly_digest_generator (Sonnet)           |
                    +-----------------------+---------------------+
                                            |
                    +-----------------------v---------------------+
                    |           GUARDRAILS LAYER                  |
                    |                                             |
                    |  approval_flow (7 content types now)        |
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
                    +-----------------------+---------------------+
                                            |
                    +-----------------------v---------------------+
                    |           INTERFACES                        |
                    |                                             |
                    |  Telegram Bot (primary daily UI)            |
                    |    + Inline keyboard (prep outline buttons) |
                    |    + Focus input (prep discussion)          |
                    |  Gmail (send approved content)              |
                    |  Google Sheets (task/Gantt write)           |
                    |  Google Drive (summary + prep docs upload)  |
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
                    |    calendar_classifications (+ meeting_type)|
                    |    meeting_prep_history (+ outline/focus)   |
                    +---------------------------------------------+
```

---

## 2. DAILY RHYTHM (Post Phase 5)

```
TIME (IST)  EVENT                              COMPONENT
----------  ---------------------------------  --------------------------
07:00       Morning Brief                      morning_brief_scheduler
            +-- Run personal email scan        personal_email_scanner
            +-- Collect overnight constant      email_watcher (queued)
            |   layer extractions
            +-- Fetch today's calendar          google_calendar
            +-- Check pending prep outlines     supabase [NEW]
            +-- Compile into ONE message        morning_brief processor
            +-- Send to Eyal for approval       approval_flow -> Telegram

T-24h       Prep Outline Proposal              meeting_prep_scheduler [NEW]
before      +-- Classify meeting type           meeting_type_matcher
meeting     |   (scoring: title, participants,
            |    day, history -> auto/ask/none)
            +-- Generate structured outline     meeting_prep.generate_prep_outline
            |   (template-driven data queries,
            |    graceful degradation per query)
            +-- Send outline to Eyal            telegram_bot.send_prep_outline
            |   with inline buttons:
            |   [Generate] [Add focus] [Skip]
            |   (+ [Wrong type?] if confidence=ask)
            +-- Schedule reminders + auto-gen   asyncio tasks (persistent)

Eyal        Outline Discussion                 telegram_bot callbacks [NEW]
responds    +-- "Generate as-is"               generate_meeting_prep_from_outline
            |   -> full doc -> standard approval
            +-- "Add focus"                    _handle_prep_focus_input
            |   -> Eyal types instruction
            |   -> update outline -> show again
            +-- "Wrong meeting type"           _handle_prep_reclassify_callback
            |   -> pick new template -> regenerate
            +-- "Skip"                         mark skipped, cancel timers

T-12h       Auto-generate (if no response)     scheduler timer [NEW]
            +-- Generate from outline as-is
            +-- Submit for standard approval

All day     Continuous monitoring:
            +-- Transcript watcher (30s)        new meeting recordings
            +-- Email watcher (5min)            team emails to Gianluigi
            +-- Document watcher (5min)         new uploads to Drive
            +-- Prep outline re-verification    calendar re-check [NEW]
            |   (deleted event -> cancel prep,
            |    rescheduled -> recalculate mode)

~18:00      End-of-Day Debrief
            +-- Surface pending prep outlines   debrief processor [NEW]
            +-- Interactive conversation
            +-- Quick injection / full debrief

Sunday      Weekly Digest                      weekly_digest_scheduler
18:00
```

---

## 3. PHASE 5: MEETING PREP FLOW (NEW)

This is the core Phase 5 addition — the propose-discuss-generate pipeline:

```
Calendar event detected (CropSight meeting)
      |
      v
meeting_type_matcher.classify_meeting_type(event)
      |
      +-- Scoring signals:
      |     title fuzzy match       (+3)
      |     exact participants      (+2)
      |     partial participants    (+1)
      |     day-of-week match       (+1)
      |     previously matched      (+2)
      |
      +-- Score >= 3: "auto" (confident)
      +-- Score == 2: "ask"  (include reclassify button)
      +-- Score <  2: "none" (use generic template)
      |
      v
calculate_timeline_mode(hours_until_meeting)
      |
      +-- > 24h:  "normal"     Full outline -> discuss -> generate
      +-- 12-24h: "compressed" Outline + shortened reminders
      +-- 6-12h:  "urgent"     Outline + single reminder + auto-gen at 4h
      +-- 2-6h:   "emergency"  Outline + parallel background generation
      +-- < 2h:   "skip"       Too late, log and skip
      |
      v
generate_prep_outline(event, meeting_type)
      |
      +-- Load template from MEETING_PREP_TEMPLATES
      +-- For each data_query in template:
      |     try:
      |       result = _execute_data_query(query)  # existing functions
      |       sections.append({status: "ok", data: result})
      |     except:
      |       sections.append({status: "unavailable: <reason>"})
      |
      +-- Generate suggested agenda (Haiku, ~$0.001)
      |
      v
format_outline_for_telegram(outline, confidence)
      |
      +-- Meeting name, time, participants
      +-- Per-section: name + summary + item count (or "unavailable")
      +-- If confidence=="ask": "I think this is a [type]..."
      +-- Suggested agenda bullet list
      |
      v
submit_for_approval(content_type="prep_outline") -> Telegram
      |
      +-- InlineKeyboard buttons:
      |     auto:  [Generate as-is] [Add focus] [Skip]
      |     ask:   [Generate as-is] [Add focus] [Wrong type?] [Skip]
      |
      v
Eyal interacts (or doesn't):
      |
      +-- "Generate as-is" ---------> generate_meeting_prep_from_outline()
      |                                    |
      |                                    +-- format_prep_document_v2()
      |                                    +-- Save .md to Drive
      |                                    +-- Submit as meeting_prep approval
      |                                    +-- (standard approve/edit/reject flow)
      |
      +-- "Add focus" --------------> Set focus_active in Supabase
      |   Eyal types: "focus on        Store in context.user_data (cache)
      |   MVP timeline"                Update outline, show again with
      |                                [Generate] [Edit more] [Skip]
      |
      +-- "Wrong meeting type" -----> Show template picker
      |   Eyal picks new type          remember_meeting_type() (persistent)
      |                                Regenerate outline with new template
      |
      +-- "Skip" ------------------> Mark skipped, cancel timers, log
      |
      +-- No response (timeout) ----> Auto-generate with defaults
      |
      v
On approval: distribute_approved_prep()
      |
      +-- Generate .docx (generate_prep_docx)
      +-- Upload .docx to Drive
      +-- Sensitivity check:
      |     sensitive -> Eyal only + Drive + note
      |     normal -> email to participants + Telegram group + Drive
      +-- Update meeting_prep_history
```

---

## 4. MEETING PREP TEMPLATES

```
TEMPLATE               MATCH SIGNALS              DATA QUERIES
---------------------  -------------------------  ---------------------------
founders_technical     "Tech Review", "R&D",      Tasks (Roye), Decisions,
                       Eyal+Roye, Tuesday         Open Questions, Commitments,
                                                  Gantt (Product & Tech)

founders_business      "Business Review", "BD",   Tasks (Paolo), Decisions,
                       Eyal+Paolo, Thursday       Open Questions, Commitments,
                                                  Gantt (BD & Partnerships),
                                                  Entity timeline (Lavazza)

monthly_strategic      "Board", "Strategic",      All tasks, All decisions,
                       All founders,              Gantt (all sections),
                       1st of month               Commitments, Entity mentions

generic                (fallback)                 Tasks (all attendees),
                                                  Recent decisions,
                                                  Open questions
```

---

## 5. EMERGENCY TIMELINE MODE DETAIL

```
EMERGENCY MODE (2-6 hours before meeting):

     +-- Outline sent to Eyal immediately
     |
     +-- Background generation starts simultaneously (asyncio task)
     |
     +-- Race condition handling:
           |
           +-- Eyal responds FIRST:
           |     Cancel background task
           |     Use Eyal's input (focus, reclassify, etc.)
           |
           +-- Background completes FIRST:
                 Submit prep for approval
                 Notify: "Emergency prep generated automatically"
```

---

## 6. RESTART-SAFE STATE MANAGEMENT

```
STATE                   STORAGE              RECOVERY ON STARTUP
---------------------   -------------------  ---------------------------
Outline pending         pending_approvals    reconstruct_prep_timers()
                        (content JSONB)      rebuilds asyncio tasks

Focus active            content.focus_active Supabase query fallback
                        + context.user_data  (user_data is cache only)

Reminder schedule       content.             recalculate from
                        next_reminder_at     event_start_time

Auto-publish timers     auto_publish_at      reconstruct_auto_publish_timers()
                        (existing v0.4)

Stale focus cleanup     orphan_cleanup       clear focus_active > 30min
```

---

## 7. APPROVAL FLOW (Updated — 7 Content Types)

```
CONTENT TYPES:
  +------------------------+-------------------------------------------+
  | Content Type           | What happens on approval                  |
  +------------------------+-------------------------------------------+
  | meeting_summary        | Distribute: Telegram group, Gmail,        |
  |                        | Sheets, Drive (.md + .docx)               |
  +------------------------+-------------------------------------------+
  | prep_outline [NEW]     | Telegram-only. Eyal interacts via inline  |
  |                        | buttons. Email rejected with message.     |
  |                        | On generate: creates meeting_prep.        |
  +------------------------+-------------------------------------------+
  | meeting_prep           | Sensitivity-aware distribution:           |
  |                        | sensitive -> Eyal + Drive + note           |
  |                        | normal -> team (Telegram + email + Drive) |
  |                        | Now generates .docx alongside .md [NEW]   |
  +------------------------+-------------------------------------------+
  | weekly_digest          | Send to Eyal only (Telegram + Email)      |
  +------------------------+-------------------------------------------+
  | gantt_update           | Write changes to Google Sheets            |
  +------------------------+-------------------------------------------+
  | morning_brief          | Mark scans approved, inject items         |
  |                        | to DB + RAG. Now shows pending preps.     |
  +------------------------+-------------------------------------------+
  | debrief                | Inject items to DB + RAG                  |
  +------------------------+-------------------------------------------+

  Expiry behavior for prep_outline:
    - Meeting still future -> auto-generate with defaults
    - Meeting has passed -> expire silently
    - Stale focus_active flags -> cleared after 30 minutes
```

---

## 8. NEW/MODIFIED FILES (Phase 5)

```
gianluigi/
+-- config/
|   +-- meeting_prep_templates.py    [NEW] 4 templates with data queries
|   +-- settings.py                  [MOD] 6 new meeting prep settings
+-- processors/
|   +-- meeting_type_matcher.py      [NEW] Scoring-based type classifier
|   +-- meeting_prep.py              [MOD] Outline generation, format_v2,
|   |                                      timeline modes, generate_from_outline
|   +-- morning_brief.py             [MOD] Pending prep outlines section
|   +-- debrief.py                   [MOD] Surface pending preps at start
+-- schedulers/
|   +-- meeting_prep_scheduler.py    [REWRITTEN] Propose-discuss-generate flow
+-- services/
|   +-- telegram_bot.py              [MOD] send_prep_outline, callbacks,
|   |                                      focus input, /status prep section
|   +-- supabase_client.py           [MOD] 3 new methods (classification,
|   |                                      prep outlines)
|   +-- word_generator.py            [MOD] generate_prep_docx()
+-- guardrails/
|   +-- approval_flow.py             [MOD] prep_outline type, email guard,
|   |                                      enhanced expiry, focus cleanup
+-- main.py                          [MOD] Startup timer reconstruction
+-- scripts/
|   +-- migrate_phase5.sql           [NEW] Schema migration
+-- tests/
    +-- test_meeting_type_matcher.py  [NEW] 26 tests
    +-- test_prep_outline.py          [NEW] 19 tests
    +-- test_prep_telegram_flow.py    [NEW] 12 tests
    +-- test_prep_timeline.py         [NEW] 29 tests
    +-- test_prep_distribution.py     [NEW] 10 tests
    +-- test_prep_queue.py            [NEW]  9 tests
    +-- test_meeting_prep.py          [MOD]  7 tests updated for new API
    +-- test_morning_brief.py         [MOD]  1 test updated
```

---

## 9. LLM COST: MEETING PREP

```
STEP                  MODEL        COST
--------------------  -----------  -----------
Outline agenda gen    Haiku        ~$0.001
Focus classification  Haiku        ~$0.0005
Focus RAG search      OpenAI emb   ~$0.001
Full prep synthesis   Sonnet       ~$0.02
                                   -----------
TOTAL PER PREP                     ~$0.02-0.03

vs. old flow (blind Sonnet gen):   ~$0.05-0.10

Phase 5 is CHEAPER because outline gathers data without LLM,
and only the final synthesis uses Sonnet.
```

---

## 10. TELEGRAM COMMANDS (Updated)

```
COMMAND          DESCRIPTION
---------------  ------------------------------------------
/start           Welcome message + status
/help            Show all commands
/mytasks         Show open tasks for Eyal
/tasks           Show all open tasks
/decisions       Show recent decisions
/status          System health + pending preps [ENHANCED]
/digest          Generate weekly digest now
/reprocess <id>  Reprocess a meeting transcript
/gantt           Show current Gantt chart state
/debrief         Start end-of-day debrief session
/emailscan       Trigger manual email scan (1/day limit)

INLINE BUTTONS (NEW):
  [Generate as-is]     Generate prep from outline
  [Add focus]          Tell Gianluigi what to focus on
  [Wrong type?]        Reclassify meeting type
  [Skip this prep]     Skip prep generation
  [Template picker]    Choose correct meeting type

FREE TEXT:       Ask any question — routed through multi-agent system
FOCUS INPUT:     Type focus instruction when prompted (non-blocking)
REPLIES:         Reply to approval messages to edit/approve/reject
```
