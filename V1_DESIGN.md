# Gianluigi v1.0 — "The AI Office Manager"
# Design Document & Claude Code Implementation Guide

**Author:** Eyal Zror (CEO, CropSight) + Claude (Architecture)
**Date:** March 14, 2026
**Status:** Approved for implementation
**Previous version:** v0.5 (579 tests, deployed Cloud Run, live-tested)

---

## Table of Contents

1. [Pre-Work: Save Current Version](#1-pre-work-save-current-version)
2. [Vision & Architecture Shift](#2-vision--architecture-shift)
3. [Multi-Agent Architecture](#3-multi-agent-architecture)
4. [New Capabilities (Detailed Specs)](#4-new-capabilities)
   - 4.1 Gantt Integration (Bidirectional)
   - 4.2 Email Intelligence
   - 4.3 End-of-Day Debrief Flow
   - 4.4 Weekly Review Session
   - 4.5 Meeting Prep (Redesigned)
   - 4.6 Gantt Slide Generation (Weekly PPTX)
   - 4.7 MCP Server (Claude.ai Interface)
   - 4.8 HTML Weekly Report
5. [Heartbeat Architecture](#5-heartbeat-architecture)
6. [RAG System Upgrades](#6-rag-system-upgrades)
7. [New Supabase Tables](#7-new-supabase-tables)
8. [Guardrails (Expanded)](#8-guardrails-expanded)
9. [Free-Text Resilience](#9-free-text-resilience)
10. [Production Robustness](#10-production-robustness)
11. [Infrastructure Changes](#11-infrastructure-changes)
12. [Build Sequence](#12-build-sequence)
13. [Testing Strategy](#13-testing-strategy)

---

## 1. Pre-Work: Save Current Version

**Do these steps FIRST before any v1.0 development.**

### 1.1 Tag the current version
```bash
git add -A
git commit -m "v0.5-stable: final state before v1.0 development"
git tag v0.5-stable
git push origin main --tags
```

### 1.2 Create KNOWN_ISSUES.md
Document all bugs found during live testing. Include at minimum:
- Sheets tab targeting issue (tasks going to wrong tab) — fixed Mar 13
- Email approval routing (replies now processed) — fixed Mar 13
- Edit count message accuracy — fixed Mar 13
- Meeting prep quality issues (too much noise, wrong context, timing problems)
- Conversation memory issues in multi-turn flows (data handling, formatting drift)
- Any other bugs discovered during the ~1 week assessment period

### 1.3 Update CLAUDE.md
Replace the current CLAUDE.md with the updated version provided alongside this document.

### 1.4 Infrastructure Prep (before coding)
- [ ] Add Gantt Google Sheet ID to `.env`: `GANTT_SHEET_ID=...`
- [ ] Identify Gantt sheet tab names and their structure (currently: "2026-2027", "2028-2029", "2030-2031", "Meeting Cadence", "Log", "Config")
- [ ] Expand Gmail OAuth scopes: add `gmail.readonly` for Eyal's personal Gmail (for daily email scan)
- [ ] Add Eyal's personal Gmail to `.env`: `EYAL_PERSONAL_EMAIL=...`
- [ ] Add `python-pptx` to `requirements.txt`
- [ ] Plan new Supabase tables (see Section 7) — prepare SQL migration script
- [ ] Create a Google Calendar event: "CropSight: Weekly Review with Gianluigi" (recurring, purple, ~30 min, Friday afternoon or whenever suits)

---

## 2. Vision & Architecture Shift

### From "Meeting Processor" to "Information Router with State"

**v0.5 mental model:** Something arrives → process it → store it → maybe distribute it.

**v1.0 mental model:** Gianluigi always maintains a current picture of CropSight's operational state and continuously updates it from every information source. The Gantt is the central operational artifact. All information flows toward keeping the Gantt, tasks, and stakeholders current and actionable.

### Information Sources (Inputs)
- Meeting transcripts (Tactiq → Drive) — existing
- Documents (Drive folder) — existing
- Emails (Gianluigi's inbox — constant monitoring) — existing, enhanced
- Emails (Eyal's personal inbox — daily filtered scan) — NEW
- End-of-day debriefs (Telegram conversation) — NEW
- Calendar events (read, detect un-transcribed meetings) — enhanced
- Claude.ai sessions via MCP (structured work sessions) — NEW

### Operational State (The "Brain")
- Supabase: meetings, decisions, tasks, entities, commitments, open questions
- Google Sheets: Gantt (master operational plan), Task Tracker, Stakeholder Tracker
- Embeddings: searchable memory across all sources

### Outputs
- Meeting summaries → Drive + email + Telegram — existing
- Task Tracker updates → Google Sheets — existing
- Stakeholder Tracker updates → Google Sheets — existing
- Gantt updates → Google Sheets (with versioning/rollback) — NEW
- Weekly Gantt slide → PPTX → Drive + distribution — NEW
- Meeting prep docs → Drive + email (template-driven) — redesigned
- Weekly HTML report → Cloud Run hosted — NEW
- MCP tool responses → Claude.ai — NEW

### Key Design Principles
- **Gianluigi proposes, Eyal approves.** Never write to the Gantt, distribute to team, or make structural changes without explicit CEO approval.
- **All team interactions go through Eyal.** Gianluigi does NOT directly nudge team members. It reports to Eyal, who decides how to follow up. The only direct team contact is approved distributions (summaries, prep docs, email notifications).
- **Brain is interface-agnostic.** Capabilities are Python functions that read/write Supabase and Google APIs. Telegram and Claude.ai/MCP are just interfaces that call these functions.
- **Future-proof the data model.** Add `workspace_id` to new tables (default "cropsight"). Namespace data for potential multi-workspace future.

---

## 3. Multi-Agent Architecture

### Why Multi-Agent
v1.0 has diverse capability requirements: natural dialogue (debriefs, weekly review), accuracy-critical analysis (transcript extraction, email intelligence), and reliable execution (Gantt writes, Sheets operations, slide generation). A single agent with 25+ tools and a massive system prompt becomes unreliable. Splitting into specialized agents with focused prompts improves quality.

### Implementation: Plain Python + Anthropic SDK (NO CrewAI)
No external agent framework. Each "agent" is a Python function that calls the Anthropic API with a specific system prompt and model. Coordination is simple Python logic (if/elif routing).

### Agent Definitions

#### Router Agent (Haiku)
- **Purpose:** Classify incoming messages and route to correct agent
- **Model:** claude-haiku (cheapest, fastest)
- **Input:** Raw user message + minimal context (current conversation mode, user ID)
- **Output:** One of: `question`, `task_update`, `information_injection`, `gantt_request`, `debrief`, `approval_response`, `weekly_review`, `meeting_prep_request`, `ambiguous`
- **System prompt:** Short classification prompt with examples of each category
- **File:** `core/router.py`

#### Conversation Agent (Sonnet)
- **Purpose:** Handle all interactive dialogues — Telegram chat, debrief sessions, weekly review conversations, meeting prep review, Q&A
- **Model:** claude-sonnet (good reasoning, conversational)
- **Context:** Conversation history (managed via structured session state, NOT raw message history), relevant data fetched by tools
- **Tools available:** `search_memory`, `get_tasks`, `get_gantt_status`, `get_meeting_prep`, `get_upcoming_meetings`, `get_stakeholder_info`, `get_weekly_summary`
- **System prompt:** Gianluigi's personality, communication style, CropSight context. Focused on dialogue quality, follow-up questions, structured confirmation before injection.
- **File:** `core/conversation_agent.py`

#### Analyst Agent (Opus)
- **Purpose:** Accuracy-critical extraction — transcript processing, email intelligence, debrief extraction, document analysis
- **Model:** claude-opus (highest accuracy, used only when extraction quality matters)
- **Context:** Full source content (transcript, email, debrief text) + extraction schema + examples
- **Output:** Structured JSON (decisions, tasks, commitments, entities, stakeholder mentions, Gantt update suggestions)
- **System prompt:** Extraction-focused with the full tone guardrails, source citation rules, sensitivity classification, and output schemas
- **File:** `core/analyst_agent.py`
- **Note:** Uses prompt caching on system prompt (existing pattern from v0.5)

#### Operator Agent (Sonnet)
- **Purpose:** Execute approved operations — Gantt writes, Sheets updates, slide generation, file management, email sending
- **Model:** claude-sonnet (reliable tool use)
- **Tools available:** `write_gantt_cell`, `snapshot_gantt`, `rollback_gantt`, `update_task_sheet`, `update_stakeholder_sheet`, `generate_slide`, `save_to_drive`, `send_email`, `send_telegram`
- **System prompt:** Execution-focused. Validate before write. Never write without approval confirmation. Log everything.
- **File:** `core/operator_agent.py`

### Agent Communication Flow Examples

**New transcript arrives:**
```
Transcript file detected (scheduler)
  → Analyst Agent: extract structured data (decisions, tasks, commitments, entities, Gantt suggestions)
  → Store raw extraction in Supabase
  → Conversation Agent: format approval message for Eyal
  → Send to Telegram for approval
  → On approval: Operator Agent executes (write to Sheets, Drive, distribute)
```

**Eyal sends debrief on Telegram:**
```
Message arrives on Telegram
  → Router Agent: classifies as "debrief"
  → Conversation Agent: enters debrief mode, asks follow-up questions, builds structured session state
  → When Eyal confirms: Analyst Agent extracts structured data from debrief
  → Conversation Agent: presents extraction for approval
  → On approval: Operator Agent executes
```

**Weekly review in Claude.ai via MCP:**
```
Eyal calls get_weekly_summary() via MCP
  → Returns compiled data (week stats, task status, Gantt changes, meeting cadence)
  → Claude.ai presents, Eyal discusses
  → Eyal asks for Gantt changes → propose_gantt_update() via MCP
  → Eyal approves → approve_gantt_update() via MCP → Operator Agent writes
  → Eyal requests slide → generate_gantt_slide() via MCP → returns file
```

---

## 4. New Capabilities

### 4.1 Gantt Integration (Bidirectional)

#### 4.1.1 Gantt Schema Map
Store the Gantt's structure in Supabase so Gianluigi understands the spreadsheet layout without hardcoding row numbers.

```python
# Supabase table: gantt_schema
{
    "sheet_name": "2026-2027",
    "section": "Product & Technology",
    "subsection": "Execution",
    "row_number": 22,
    "owner_column": "C",
    "due_column": "D",
    "first_week_column": "E",  # W9
    "week_offset": 9,  # first week number
    "protected": False,  # True for formula rows like Escalations
    "notes": "Main execution tracking for R&D work"
}
```

Build a schema parser that reads the Gantt sheet structure and populates this table. Re-run it when the Gantt structure changes. The schema map should also identify:
- Formula rows (NEVER write to these): "Escalations & Blockers", "Meeting Count (helper)", "All Meetings (Aggregated)"
- Header rows
- Section divider rows
- The Meeting Cadence tab structure
- The Log tab structure
- The Config tab structure

#### 4.1.2 Gantt Read Operations
- `get_gantt_status(week_number=None)` → Returns current state of all sections for a given week (default: current week). Includes: what's active, what's blocked, what milestones are approaching, availability restrictions.
- `get_gantt_section(section_name, week_range=None)` → Deep dive into a specific section.
- `get_meeting_cadence(week_number=None)` → Returns expected meetings this week from the Meeting Cadence tab, compared against what actually happened (transcripts received, calendar events).
- `get_gantt_horizon(weeks_ahead=8)` → Upcoming milestones, transitions, key dates.

#### 4.1.3 Gantt Write Operations (Always with Approval)
- `propose_gantt_update(changes: list)` → Creates a proposal with specific cell-level changes. Each change specifies: sheet_name, row (by section/subsection, not raw number), column (by week number), old_value, new_value, reason.
- `approve_gantt_update(proposal_id)` → Executes the approved proposal: snapshots affected cells, writes changes, logs to Change Log tab.
- `reject_gantt_update(proposal_id, reason=None)` → Marks proposal as rejected with optional reason.

#### 4.1.4 Gantt Versioning & Rollback
Before every approved write:
1. Snapshot affected cells in `gantt_snapshots` table (timestamp, sheet_name, cell_references, old_values, new_values, proposal_id, approved_by)
2. Apply changes via Google Sheets API
3. Write entry to the Log tab: date, week, section, change description, by (Gianluigi), related source (meeting/email/debrief/weekly_review)

Rollback capabilities:
- `rollback_gantt_update(proposal_id)` → Restores cells from snapshot
- `rollback_gantt_week(week_date)` → Restores all changes made in a given week
- `get_gantt_change_history(date_range=None)` → Shows all changes with diffs

Additionally: before each weekly review session, create a full sheet-level backup (copy entire Gantt to a "Gantt Backups" folder in Drive, named with date). This is the "nuclear option" rollback.

#### 4.1.5 Gantt Status Changes
Gianluigi can propose changing item statuses: Active → Completed, Active → Blocked, Blocked → Active, Planned → Active. These map to cell formatting (colors) in the spreadsheet. Use Google Sheets API formatting calls to change cell background colors according to the Gantt's color legend:
- Active = green fill
- Planned = blue fill
- Completed = gray fill
- Blocked = red fill

Read the exact color codes from the existing Gantt (check the Config tab or first few data rows for reference).

#### 4.1.6 Section and Subsection Awareness
When proposing updates, Gianluigi must be explicit about WHERE in the Gantt: "I want to add '[P] USA Sales Recruit' to **Sales & Business Dev → Execution**, starting W19, owner: P." Never propose adding something "somewhere in the Gantt."

The Gantt schema map enables this. When the Gantt structure changes (new section added, rows reorganized), Eyal runs a re-scan command or Gianluigi detects the structural change on its next read and asks for confirmation.

### 4.2 Email Intelligence

#### 4.2.1 Constant Layer (Gianluigi's Inbox)
Enhance the existing email watcher. Currently routes questions and processes approval replies. Add:
- **Full extraction on all incoming emails:** Every email to gianluigi.cropsight@gmail.com gets analyzed by the Analyst Agent (Haiku for classification, Sonnet for extraction if relevant).
- **Outbound awareness:** Track emails sent by Gianluigi (summaries, prep docs, notifications). Know what was communicated to whom.
- **Attachment auto-download:** When a relevant email has attachments (PDF, DOCX, XLSX, PPTX), download them and run them through the document processor. Link the document to the email's extracted data. Size limit: 25MB. Skip: .exe, .zip, .dmg, .bat, images (for now).
- **Thread tracking:** If Gianluigi is CC'd on an email thread, track the full thread — not just individual messages.

#### 4.2.2 Daily Scan Layer (Eyal's Personal Gmail)
New capability. Requires `gmail.readonly` OAuth scope on Eyal's personal account.

**Schedule:** Once daily, early morning (configurable, default 07:00 IST).

**Filter chain (whitelist approach — only process matching emails):**
1. Sender or recipient email matches: team member emails (Roye, Paolo, Yoram) OR known stakeholder domains from entity registry
2. Subject line contains CropSight-related keywords (from entity registry: company names, project names, key terms)
3. Thread is already being tracked (a reply in a thread Gianluigi previously processed)
4. Any email explicitly labeled "CropSight" (if Eyal sets up a Gmail label)

**What does NOT get processed:**
- Personal contacts (maintain a personal-contacts blocklist in config/team.py)
- Marketing/newsletter emails
- Anything that doesn't match the filter chain

**Processing:**
1. Haiku classifies each matching email: relevant (extract) / borderline (flag for review) / false positive (skip)
2. Relevant emails → Sonnet extracts: tasks, decisions, commitments, stakeholder mentions, deadline changes, Gantt-relevant information
3. Compile into a "Daily Email Intelligence Brief" — structured data, NOT raw email content
4. Send brief to Eyal via Telegram: "Morning email scan found 4 CropSight-related emails. Key items: [summary]. Review and approve injection?"
5. Eyal approves → data injected into Supabase, tasks/stakeholders updated
6. Log processed emails in `email_scans` table (email_id, date, sender, classification, extracted_items, approved)

**Cost estimation:** ~15 emails scanned/day × Haiku classification ($0.001) + ~5 relevant emails × Sonnet extraction ($0.01) = ~$0.07/day = ~$2/month.

**Privacy guardrails:**
- Never store raw email body text from non-CropSight emails
- Never send email content to the team — only extracted intelligence referenced as "from email correspondence"
- Maintain a log of "emails processed" vs "emails skipped" that Eyal can review
- Personal-contacts blocklist: Eyal maintains a list of personal email addresses that are NEVER processed regardless of content

#### 4.2.3 Outbound Email Awareness
When Gianluigi can see Eyal's sent emails (via daily scan), it can detect outbound actions:
- "Eyal sent an email to investor David about the pre-seed round" → update stakeholder tracker last-contact date
- "Eyal sent MVP specs to Orit" → update Moldova pilot status
This is purely informational extraction — Gianluigi never sends emails from Eyal's personal account.

### 4.3 End-of-Day Debrief Flow

#### 4.3.1 Trigger Methods
- **Explicit:** Eyal sends `/debrief` on Telegram or types "end of day update" / "quick debrief" / similar natural language
- **Calendar-prompted:** Gianluigi detects un-transcribed purple calendar events (CropSight meetings that happened but no transcript was received within 2 hours of meeting end). Sends prompt: "You had 'Moldova Technical Review' (14:00-15:00) on your calendar today, but I didn't receive a transcript. Was this an in-person meeting? Want to do a quick debrief?"
- **Scheduled prompt:** If enabled, Gianluigi sends a gentle end-of-day prompt at a configured time (e.g., 18:00 IST on workdays): "Any updates from today I should capture?"

#### 4.3.2 Debrief Session State
Use a structured session object to manage the multi-turn conversation, NOT raw conversation history. This prevents the memory/formatting issues from v0.5.

```python
# Supabase table: debrief_sessions
{
    "session_id": "uuid",
    "date": "2026-03-14",
    "status": "in_progress",  # in_progress | confirming | approved | cancelled
    "items_captured": [
        {
            "type": "task",  # task | decision | commitment | stakeholder | gantt_update | information
            "text": "Moldova wheat data expected next week",
            "assignee": "Roye",
            "deadline": "2026-03-21",
            "category": "Product & Tech",
            "sensitivity": "normal",
            "confirmed": true
        }
    ],
    "pending_questions": [
        "Who should own the wheat data tracking?"
    ],
    "calendar_events_covered": ["Moldova Technical Review"],
    "calendar_events_remaining": ["Advisory Sync"],
    "raw_messages": ["<user's original messages>"],
    "created_at": "...",
    "updated_at": "..."
}
```

#### 4.3.3 Conversation Flow
Each turn, the Conversation Agent receives:
1. The structured session state (NOT full conversation history)
2. The last 2-3 messages (for conversational continuity)
3. Today's calendar events (from Google Calendar)
4. Current open tasks and recent activity (from Supabase)

The agent:
1. Processes the user's input → extracts items → adds to `items_captured`
2. Cross-references against known data (entity registry, existing tasks, calendar)
3. Asks clarifying/verification questions → adds to `pending_questions`
4. When the user indicates they're done, presents the full extraction for approval
5. On approval → Analyst Agent validates, then Operator Agent injects into Supabase and proposes any Gantt/Sheet updates

**Critical design rule:** The Conversation Agent outputs structured JSON internally (which the code formats for Telegram display). The agent does NOT format Telegram messages directly. This separation prevents the formatting drift issues from v0.5.

#### 4.3.4 Debrief-to-Gantt Bridge
If debrief items are Gantt-relevant (timeline changes, new work items, milestone updates, status changes), the Analyst Agent should flag them as Gantt update candidates. These get bundled into a Gantt update proposal that follows the standard approval flow.

### 4.4 Weekly Review Session

#### 4.4.1 Scheduling
Calendar-driven, NOT hardcoded. Gianluigi watches for a recurring purple calendar event titled "CropSight: Weekly Review with Gianluigi" (or configurable title pattern). When the event is within 3 hours, Gianluigi begins preparation.

**Prep timeline:**
- T-3 hours: Start generating weekly summary data, compile Gantt status, generate HTML report, generate PPTX slide
- T-30 minutes: Send Telegram notification: "Ready for our weekly review. Prep materials are ready. [HTML Report Link]"
- T-0: Eyal initiates the interactive session (on Telegram or via Claude.ai MCP)

If Eyal moves the calendar event to a different day/time, Gianluigi automatically adjusts. No code changes needed.

#### 4.4.2 Weekly Review Agenda (Auto-Generated)

**Part 1: Week in Review**
- Meetings held vs. expected (from Meeting Cadence tab)
- Transcripts processed, emails scanned, debriefs conducted
- Decisions captured this week (count + highlights)
- Tasks: created / completed / overdue / total open
- Commitments: fulfilled / still open / new

**Part 2: Gantt Update Proposals**
- Proposed changes derived from this week's meetings, emails, and debriefs
- Each proposal shows: section → subsection → item → what to change → why (source reference)
- Eyal approves individually or batch-approves

**Part 3: Attention Needed**
- Overdue tasks (by assignee, days overdue)
- Stale items (tasks not updated in >2 weeks)
- Recurring unresolved discussions (open questions that keep appearing in meetings)
- Missed meetings (expected per cadence but didn't happen)
- Approaching milestones (within next 4 weeks)

**Part 4: Next Week Preview**
- Calendar for next week (meetings scheduled, prep docs status)
- Key deadlines approaching
- Gantt items active next week
- Suggested priorities

**Part 5: Horizon Check**
- Where are we relative to strategic milestones?
- Pre-Seed window status
- MVP delivery timeline
- Any red flags on the horizon

**Part 6: Outputs Generated**
After discussion and approvals:
- Weekly Gantt slide (.pptx) generated and saved to Drive
- HTML report updated with approved changes
- Weekly digest document saved to Drive
- Gantt updated with approved changes
- Log tab entries added
- All outputs sent/distributed as appropriate

**Part 7: Post-Output Review**
Eyal can review outputs and request corrections: "That task was assigned to the wrong section" / "The slide shows wrong dates" → Gianluigi corrects and regenerates affected outputs.

### 4.5 Meeting Prep (Redesigned)

#### 4.5.1 Scope
- **Auto-generated for:** All management meetings (4 founders): Founders Technical Review, Founders Business Review, Monthly Strategic & Operational Review
- **On-request:** Any meeting Eyal asks for, either ad-hoc via Telegram ("Prep me for Thursday's Advisory Sync") or during the weekly review ("Generate prep for next week's Fundraising Pipeline Review")
- **NOT auto-generated for:** CEO-CTO weekly (too frequent/informal), Commercial Sync (Eyal knows context), Bookkeeping (not relevant)

#### 4.5.2 Template-Driven Generation
Each meeting type has a dedicated prep template defining what data to pull and how to structure it.

**Founders Technical Review (E/R/P/Y) template:**
- Open tasks assigned to Roye (the technical lead), status and overdue flags
- Decisions from last Founders Technical meeting needing follow-up
- Gantt: Product & Technology section status for current + next 2 weeks
- Commitments by Roye or about technical work — fulfilled or open
- Open technical questions still unresolved
- Suggested agenda (top 3-5 items ranked by urgency/importance)

**Founders Business Review (E/R/P/Y) template:**
- All open tasks by assignee across all categories
- Gantt progress across ALL sections (high-level status)
- Key decisions pending team input
- Stakeholder pipeline update (new contacts, recent interactions)
- Fundraising status (if Pre-Seed window approaching)
- Suggested agenda

**Monthly Strategic & Operational Review (E/R/P/Y) template:**
- Month-in-review: meetings held, decisions made, tasks completed
- Gantt: full picture — what moved, what's behind, what's ahead
- Strategic milestone progress
- OKR status check
- Escalations & blockers summary
- Suggested strategic topics

Store templates in `config/meeting_prep_templates.py`. Each template is a dict specifying: `meeting_type`, `data_queries` (list of Supabase/Sheets queries to run), `structure` (output format), `focus_areas` (what Claude should emphasize).

#### 4.5.3 Timing & Approval Flow
1. Gianluigi generates prep doc **~24 hours** before the meeting
2. Sends to Eyal via Telegram: "Here's the prep for tomorrow's Founders Technical Review. Review and approve, or tell me what to change."
3. Eyal reviews. Can:
   - Approve as-is
   - Request changes: "Add the cloud provider decision to the agenda" / "Remove the stakeholder section, not relevant"
   - Add focus areas: "I want to discuss the MVP timeline specifically"
   - Reject (don't send)
4. Once approved, Gianluigi distributes to all meeting participants:
   - Email with Drive link (via Gianluigi's Gmail)
   - Telegram group notification
   - Saved to Google Drive: `Meeting Prep/` folder
5. **Distribution timing:** Participants receive at least 12 hours before the meeting (this gives a full evening/morning to read)
6. If meeting is Sunday morning (common in Israel), generation happens Thursday/Friday, Eyal reviews before Shabbat, team receives Friday afternoon or Saturday night.

Timing per meeting type is configurable in the template: `prep_lead_hours: 24` (default), adjustable.

### 4.6 Gantt Slide Generation (Weekly PPTX)

#### 4.6.1 Output Format
Match the structure of the existing `CropSight_Gantt_Slide_Q1Q2_2026.pptx`:
- Header bar: "CropSight — Operational Gantt Q1–Q2 2026" | "Management Review | Current: W{X} (date) | Gianluigi-generated"
- Week columns spanning current quarter + next quarter (or configurable range)
- "You are here" marker on current week
- Capacity/restriction annotations (from Gantt "Availability & Restrictions" row)
- Owner legend: [E] Eyal [R] Roye [P] Paolo [Y] Yoram
- 5 section rows: Strategic Milestones, Product & Technology, Sales & Business Dev, Fundraising & Investor Rel., Legal & Finance
- Colored bars (Active=green, Planned=blue, Blocked=red, Completed=gray)
- Milestone markers (diamonds/stars)
- Bottom bar: Q3/Q4 horizon summary
- Footer: "CropSight Ltd. | Operational Gantt v{X} | Confidential | Generated {date}"

#### 4.6.2 Implementation
Use `python-pptx`. Build a slide generator that:
1. Reads current Gantt state from Google Sheets (via Sheets API)
2. Reads the Gantt schema map for section/row mapping
3. Renders the slide using positioned shapes (matching the layout of the reference PPTX)
4. Saves to Drive: `Weekly Digests/Gantt_W{X}_{date}.pptx`

The reference PPTX uses auto_shapes (not tables) for the grid layout. The generator should follow the same pattern for visual consistency.

#### 4.6.3 Trigger
Generated as part of the weekly review session (Part 5 outputs). Can also be requested ad-hoc: "Generate a Gantt slide for W11-W25" via Telegram or MCP.

### 4.7 MCP Server (Claude.ai Interface)

#### 4.7.1 Architecture
A new FastAPI endpoint on the Cloud Run service that speaks the MCP protocol (SSE-based). This sits alongside the existing Telegram bot and email watcher.

**File:** `services/mcp_server.py`

#### 4.7.2 Authentication
Bearer token authentication. Generate a secret token, store in GCP Secret Manager (alongside other secrets). Configure in Claude.ai's MCP connection settings. Every request without valid token → 401 rejected.

Only Eyal connects to the MCP server. Team members do not have Claude.ai MCP access.

#### 4.7.3 MCP Tools — Phased Rollout

**Phase 1 (Read-only, launch with v1.0):**
```
search_memory(query: str, source_types: list[str] = None) → search results
get_tasks(assignee: str = None, status: str = None, category: str = None) → task list
get_gantt_status(week: int = None, section: str = None) → gantt state
get_upcoming_meetings(days: int = 7) → calendar events with prep status
get_weekly_summary() → compiled weekly review data
get_stakeholder_info(name: str = None, organization: str = None) → stakeholder records
get_meeting_history(limit: int = 10, topic: str = None) → recent meetings
get_open_questions(status: str = "open") → unresolved questions
get_commitments(status: str = None, assignee: str = None) → commitment tracker
get_system_context() → CropSight context, current state, recent activity (for Claude to "become" Gianluigi-aware)
```

**Phase 2 (Write operations, after 1-2 weeks of stable read usage):**
```
propose_gantt_update(section: str, subsection: str, item: str, changes: dict) → proposal_id
approve_gantt_update(proposal_id: str) → execution result
reject_gantt_update(proposal_id: str, reason: str = None) → confirmation
create_task(title: str, assignee: str, deadline: str, category: str, priority: str) → task_id
update_task(task_id: str, updates: dict) → confirmation
complete_task(task_id: str) → confirmation
```

**Phase 3 (Session management, after weekly review is stable):**
```
start_weekly_review() → triggers prep generation, returns initial data
generate_gantt_slide(week_start: int = None, week_end: int = None) → file path/link
get_meeting_prep(meeting_type: str, date: str) → prep document content
get_last_session_summary() → summary of last Claude.ai session
save_session_summary(summary: str) → stored for next session continuity
rollback_gantt_update(proposal_id: str) → rollback result
get_gantt_change_history(days: int = 30) → change log
```

#### 4.7.4 MCP Tool Response Format
All MCP tool responses should return structured JSON that Claude.ai can format nicely. Include:
- `status`: "success" / "error"
- `data`: the actual response content
- `metadata`: source timestamps, record counts, freshness info
- `suggestions`: optional — things Claude might want to do next ("You might want to check the related tasks" / "This stakeholder was last contacted 3 weeks ago")

Never return raw transcript text or email content through MCP. Return summaries, structured extractions, and references (with Drive links for full documents).

#### 4.7.5 Session Continuity
Since Claude.ai conversations don't persist, implement a `session_notes` mechanism:
- At the end of each Claude.ai session, Claude calls `save_session_summary()` with a summary of what was discussed and decided
- At the start of the next session, Claude calls `get_last_session_summary()` to catch up
- Store in Supabase: `mcp_sessions` table (session_date, summary, decisions_made, pending_items)

#### 4.7.6 get_system_context Tool
This is the "onboarding" tool. When Claude.ai starts a new conversation with MCP, it can call this first to load CropSight context:
- Company info (from system_prompt.py, condensed)
- Team members and roles
- Current week number and Gantt period
- Recent activity summary (last 7 days)
- Pending items requiring attention
- Quick stats (open tasks, upcoming meetings, overdue items)

This ensures Claude.ai "feels like Gianluigi" from the first message without requiring the user to provide context.

### 4.8 HTML Weekly Report

#### 4.8.1 Purpose
A single-page web report generated for each weekly review session. Provides the visual layer that Telegram can't offer. Viewed alongside the Telegram/Claude.ai conversation.

#### 4.8.2 Content
- **Header:** Week number, date range, generation timestamp
- **Gantt snapshot:** Simplified visual of current quarter (HTML/CSS rendered, not an image)
- **Task dashboard:** Open tasks by assignee, overdue items highlighted
- **Meeting cadence status:** Expected vs. actual meetings this week
- **Decisions log:** This week's decisions
- **Milestones timeline:** Upcoming milestones with countdown
- **Commitments scorecard:** Open/fulfilled/overdue

#### 4.8.3 Implementation
Generate as a self-contained HTML file using Python string templates (Jinja2 or similar). Host on Cloud Run as a static file served via a unique URL (e.g., `/reports/weekly/2026-W11.html`). No login required — the URL serves as the access control (unguessable path with a token). Send the link via Telegram.

**File:** `processors/weekly_report.py`

#### 4.8.4 Interaction with Claude.ai
When using Claude.ai for the weekly review, include the report URL in the `get_weekly_summary()` MCP response so Claude can present it: "Here's your weekly report: [link]. Let's go through it."

---

## 5. Heartbeat Architecture

### Unified Heartbeat Scheduler
Replace the current 8 separate scheduler files with a unified heartbeat system.

**File:** `schedulers/heartbeat.py`

### Heartbeat Rhythms

| Rhythm | Interval | Triggers |
|--------|----------|----------|
| **Pulse** | Every 5 minutes | Drive watcher (transcripts, documents), Email watcher (Gianluigi inbox) |
| **Morning** | Daily 07:00 IST | Daily email scan (Eyal's Gmail), Calendar scan (today's meetings, detect gaps), Meeting prep generation (if meeting within 24 hours), System health check |
| **Evening** | Daily 18:00 IST (configurable) | End-of-day debrief prompt (if un-transcribed purple meetings detected), Task deadline check (what's due tomorrow?), Daily activity summary (internal log) |
| **Weekly Prep** | 3 hours before Weekly Review calendar event | Generate weekly summary data, Compile Gantt status, Generate HTML report, Generate PPTX slide, Send "ready" notification |
| **Weekly Post** | Sunday 20:00 IST | Generate and distribute weekly digest (if not already done in review session), Orphan cleanup sweep |
| **Alert** | Every 12 hours | Proactive alerts: overdue clusters, stale commitments, recurring discussions, question pileup |

### Implementation
```python
class HeartbeatScheduler:
    def __init__(self):
        self.rhythms = {
            "pulse": {"interval_minutes": 5, "handler": self.pulse},
            "morning": {"cron": "0 7 * * *", "handler": self.morning},
            "evening": {"cron": "0 18 * * 0-4", "handler": self.evening},  # Sun-Thu (Israeli workweek)
            "weekly_prep": {"trigger": "calendar_event", "handler": self.weekly_prep},
            "weekly_post": {"cron": "0 20 * * 0", "handler": self.weekly_post},  # Sunday
            "alert": {"interval_hours": 12, "handler": self.alert},
        }
```

The weekly_prep heartbeat is special — it's calendar-driven, not cron-driven. The scheduler checks the calendar every morning (as part of the morning heartbeat) and schedules the weekly_prep execution for 3 hours before the review event.

---

## 6. RAG System Upgrades

### 6.1 New Source Types
Extend the `source_type` field in the embeddings table to include:
- `meeting` (existing)
- `document` (existing)
- `email` — extracted intelligence from emails
- `debrief` — end-of-day debrief session content
- `gantt_change` — history of Gantt modifications
- `meeting_prep` — generated prep documents

### 6.2 Source Priority Ranking
When the same topic appears in multiple sources with conflicting information, apply priority:
1. **Debrief** (highest — Eyal's direct input)
2. **Meeting decisions** (team-level agreement)
3. **Email intelligence** (correspondence-level)
4. **Document content** (reference material)
5. **Gantt history** (lowest — derived data)

Implement as a `source_weight` multiplier in the RRF fusion scoring. Debrief chunks get 1.5x weight, meeting decisions get 1.3x, emails get 1.0x, documents get 0.9x, gantt_change gets 0.7x.

### 6.3 Freshness and Conflict Detection
When the RAG returns results from multiple sources on the same topic with different information, the Conversation Agent should flag it: "Note: the meeting transcript suggests W20 for MVP delivery, but your Thursday debrief mentioned W22. Which is correct?"

Implement as a post-retrieval check: if top results from different sources contain conflicting entities (dates, names, statuses), add a `conflicts_detected` flag to the response.

### 6.4 Embedding Lifecycle
- **Active data:** Full weight in search. Tasks with status "pending"/"in_progress", open questions, current decisions.
- **Resolved data:** Reduced weight (0.5x) but not removed. Completed tasks, resolved questions, superseded decisions. Still useful for "what did we do about X?" queries.
- **Archived data:** Minimal weight (0.2x). Meetings older than 6 months, rejected proposals. Preserved for institutional memory.

Tag embeddings with `lifecycle_status`: "active" / "resolved" / "archived". Apply lifecycle multiplier during retrieval scoring.

### 6.5 Chunking Strategy per Source Type
- **Meetings:** By conversation segment (existing behavior)
- **Emails:** Per email (most emails are single-chunk size). Include subject line and sender in chunk metadata.
- **Debriefs:** Per topic within the debrief (split on natural topic boundaries identified by the Analyst Agent)
- **Gantt changes:** Per change event (a weekly review session produces one summary chunk)
- **Documents:** By logical section (existing behavior)

### 6.6 Contextual Chunk Enrichment
For all new source types, prepend metadata to the chunk text before embedding (existing pattern from v0.5):
```
[Source: email | Date: 2026-03-14 | From: Orit (Moldova client) | Topic: wheat data delivery]
Orit confirmed wheat data will be available next week for integration testing...
```

---

## 7. New Supabase Tables

### SQL Migration Script
Add to `scripts/setup_supabase.sql` or create a new `scripts/migrate_v1.sql`:

```sql
-- Gantt schema map
CREATE TABLE gantt_schema (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    sheet_name TEXT NOT NULL,
    section TEXT NOT NULL,
    subsection TEXT,
    row_number INTEGER NOT NULL,
    owner_column TEXT DEFAULT 'C',
    due_column TEXT DEFAULT 'D',
    first_week_column TEXT DEFAULT 'E',
    week_offset INTEGER DEFAULT 9,
    protected BOOLEAN DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Gantt update proposals
CREATE TABLE gantt_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    status TEXT DEFAULT 'pending',  -- pending, approved, rejected, rolled_back
    source_type TEXT,  -- meeting, email, debrief, weekly_review, manual
    source_id UUID,  -- reference to meeting, debrief session, etc.
    changes JSONB NOT NULL,  -- [{sheet, section, subsection, row, column, old_value, new_value, reason}]
    proposed_at TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT,
    rejection_reason TEXT
);

-- Gantt snapshots (for rollback)
CREATE TABLE gantt_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    proposal_id UUID REFERENCES gantt_proposals(id),
    sheet_name TEXT NOT NULL,
    cell_references TEXT[] NOT NULL,  -- ['B22', 'C22', ...]
    old_values JSONB NOT NULL,
    new_values JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Debrief sessions
CREATE TABLE debrief_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    date DATE NOT NULL,
    status TEXT DEFAULT 'in_progress',  -- in_progress, confirming, approved, cancelled
    items_captured JSONB DEFAULT '[]',
    pending_questions JSONB DEFAULT '[]',
    calendar_events_covered TEXT[] DEFAULT '{}',
    calendar_events_remaining TEXT[] DEFAULT '{}',
    raw_messages JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Email intelligence
CREATE TABLE email_scans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    scan_type TEXT NOT NULL,  -- 'constant' (gianluigi inbox) or 'daily' (eyal gmail)
    email_id TEXT NOT NULL,  -- Gmail message ID
    date TIMESTAMPTZ NOT NULL,
    sender TEXT,
    recipient TEXT,
    subject TEXT,
    classification TEXT,  -- 'relevant', 'borderline', 'false_positive', 'skipped'
    extracted_items JSONB,  -- [{type, text, ...}]
    attachments_processed TEXT[],  -- Drive file IDs of downloaded attachments
    approved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- MCP session notes
CREATE TABLE mcp_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    session_date DATE NOT NULL,
    summary TEXT NOT NULL,
    decisions_made JSONB DEFAULT '[]',
    pending_items JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Weekly reports
CREATE TABLE weekly_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    week_number INTEGER NOT NULL,
    year INTEGER NOT NULL,
    report_url TEXT,  -- Cloud Run URL for HTML report
    slide_drive_id TEXT,  -- Google Drive file ID for PPTX
    digest_drive_id TEXT,  -- Google Drive file ID for digest document
    gantt_backup_drive_id TEXT,  -- Google Drive file ID for Gantt backup
    data JSONB,  -- Full compiled data for the report
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Meeting prep templates (reference, actual templates in code)
CREATE TABLE meeting_prep_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT DEFAULT 'cropsight',
    meeting_type TEXT NOT NULL,
    calendar_event_id TEXT,
    meeting_date TIMESTAMPTZ NOT NULL,
    prep_content JSONB NOT NULL,
    status TEXT DEFAULT 'pending',  -- pending, approved, distributed, rejected
    approved_at TIMESTAMPTZ,
    distributed_at TIMESTAMPTZ,
    recipients TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 8. Guardrails (Expanded)

### 8.1 Existing Guardrails (Keep All)
- Professional tone (no emotional characterizations)
- Source citations
- Sensitivity filtering (legal/investor → CEO-only)
- Personal content exclusion
- External participant caution
- Information security
- Inbound guardrails (5-layer)
- Human-in-the-loop (CEO approval before team distribution)

### 8.2 New Guardrails for v1.0

#### Gantt Write Protection
- NEVER write to formula rows (Escalations & Blockers, Meeting Count, All Meetings Aggregated)
- NEVER write without explicit approval
- ALWAYS snapshot before writing
- Validate row/column targets before writing (check against gantt_schema, reject if target is protected)
- Rate-limit: max 20 cell changes per approval batch (prevent runaway updates)
- Validate data types: don't put text in numeric cells, don't break date formats

#### Email Intelligence Boundaries
- Never process personal emails (personal-contacts blocklist)
- Never store raw email body from non-CropSight emails in Supabase
- Never send email content to team — only extracted intelligence ("from email correspondence")
- Log all processing decisions (processed vs. skipped) for Eyal's periodic review

#### Information Flow Control
- Sensitivity tags follow data from ingestion to output. Sensitive email intelligence stays CEO-only.
- Cross-source sensitivity: if a meeting is classified normal but an email about the same topic is sensitive, the combined intelligence should be treated as sensitive.
- Debrief sensitivity: if Eyal mentions investor conversations in a debrief, auto-classify as sensitive.

#### MCP Security
- Bearer token authentication on all MCP endpoints
- Never expose raw transcript text through MCP — return summaries and references
- Rate-limiting: max 100 MCP calls per hour (prevent abuse if token leaks)
- Log all MCP tool calls in audit_log

#### Debrief Guardrails
- Debrief extraction goes through sensitivity classifier before storage
- Ask user if sensitive items should be CEO-only: "Some of what you mentioned sounds investor-related. Should I mark this as sensitive?"
- Never inject debrief data without explicit approval of the structured extraction

---

## 9. Free-Text Resilience

### Design Principle
Every user input (Telegram or MCP) should be understood regardless of format, typos, abbreviations, or language mixing. Never require exact command syntax for core operations.

### Intent Classification (Router Agent)
The Router Agent uses Haiku to classify every incoming message. Categories:
- `question` → "What did we decide about X?"
- `task_update` → "Mark Paolo's MOU as done" / "P - MOU - complete"
- `information_injection` → "We decided to go with AWS yesterday"
- `gantt_request` → "Push MVP delivery to W22" / "Move Roye's stuff to next week"
- `debrief` → "End of day update: had a call with..." / natural debrief language
- `approval_response` → "Approve" / "Looks good" / "✅" / "Change task 3 deadline..."
- `weekly_review` → "Let's do the weekly" / "Start review"
- `meeting_prep_request` → "Prep me for Thursday's meeting"
- `ambiguous` → ask for clarification

### Entity Resolution
When the user says "Roye's stuff" or "the Moldova thing" or "P's task", Gianluigi needs to resolve these to specific records:
- "Roye" / "R" / "roye" → team member Roye Tadmor
- "Moldova thing" / "the moldov client" → entity "Moldova pilot" / stakeholder "Orit"
- "P's task" → tasks assigned to Paolo

Use the entity registry (existing) with fuzzy matching. For task resolution, search by assignee + recent mentions + keyword match.

### Confirmation Before Action
Any write operation triggered by free-text must be confirmed:
- User: "push the MVP to W22"
- Gianluigi: "I'll update the Gantt: Product & Technology → Execution → '[R] MVP Work' end date moves from W20 to W22. This affects 2 weeks of the timeline. Approve?"

Never execute a write on ambiguous input. Always show what was understood and ask for confirmation.

### Multi-Format Input
The debrief should accept:
- Long paragraphs: "So today I met with Orit and she confirmed the wheat data is coming next week, also spoke with Shimony about VAT filing..."
- Bullet style: "- Orit: wheat data next week\n- Shimony: VAT by March 31\n- Advisory sync moved to Thursday"
- Voice-note style (short, conversational): "quick update, Orit said data next week, need to tell Roye"

The Analyst Agent's extraction prompt should include examples of all these styles with expected extraction output.

---

## 10. Production Robustness

### 10.1 State Consistency
With multiple input channels, protect against race conditions:
- Use Supabase row-level operations (upsert with conflict handling) for task updates
- Queue Gantt writes (never parallel writes to the same sheet)
- Debrief sessions use session_id to prevent duplicate injections

### 10.2 Graceful Degradation
- **Anthropic API down:** Queue incoming work, process when API returns. Respond on Telegram: "I'm having trouble processing right now — I'll catch up when my AI service is back."
- **Supabase down:** Respond with cached data if available, flag uncertainty: "I might not have the latest data right now."
- **Google Sheets API rate-limited:** Queue writes, retry with exponential backoff (existing pattern)
- **Google Drive down:** Queue file operations, notify Eyal

### 10.3 Monitoring & Health
- **Heartbeat health:** Track when each heartbeat last ran. If a heartbeat misses its window by >2x, alert Eyal.
- **Data freshness:** Track last transcript processed, last email scanned, last Gantt read. Surface in `/status` command and in `get_system_context()` MCP tool.
- **Daily system health report:** (part of morning heartbeat) Brief Telegram message: "All systems operational. Yesterday: 1 transcript processed, 8 emails scanned, 0 debriefs. Gantt last updated: 2 days ago."
- **Error alerting:** Critical errors → immediate Telegram DM to Eyal (existing behavior, extend to new components)

### 10.4 Data Growth Awareness
- Monthly Supabase size check (query pg_database_size)
- Estimate: ~3-5MB/month at current usage patterns
- Free tier: 500MB database → ~8-12 years of runway
- Alert if approaching 80% of free tier limit

### 10.5 Deployment Resilience
- All state persisted in Supabase (no critical in-memory state)
- On Cloud Run restart: rebuild timers, reconnect watchers, resume from Supabase state
- Debrief sessions survive restarts (session state in Supabase)
- Gantt proposals survive restarts (all in Supabase)

---

## 11. Infrastructure Changes

### 11.1 New Environment Variables
```bash
# Gantt
GANTT_SHEET_ID=...                    # Google Sheets ID of the operational Gantt
GANTT_BACKUP_FOLDER_ID=...            # Google Drive folder for Gantt backups

# Email Intelligence
EYAL_PERSONAL_EMAIL=...               # Eyal's personal Gmail (for daily scan)
PERSONAL_CONTACTS_BLOCKLIST=...       # Comma-separated personal email addresses to never process

# MCP Server
MCP_AUTH_TOKEN=...                    # Bearer token for MCP authentication
MCP_PORT=8080                         # Port for MCP endpoint (shared with health server)

# Weekly Review
WEEKLY_REVIEW_CALENDAR_TITLE=CropSight: Weekly Review with Gianluigi

# Reports
REPORTS_BASE_URL=...                  # Cloud Run URL base for HTML reports
REPORTS_SECRET_TOKEN=...              # Token for report URL unguessability
```

### 11.2 New Dependencies (requirements.txt additions)
```
python-pptx>=0.6.21       # Gantt slide generation
jinja2>=3.1.0              # HTML report templating
```

### 11.3 OAuth Scope Expansion
Current scopes: Gmail (send, for Gianluigi's account), Drive (read/write), Calendar (read), Sheets (read/write)

New: Add `gmail.readonly` for Eyal's personal Gmail account. This requires a separate OAuth consent flow for Eyal's account. Store the refresh token as `EYAL_GMAIL_REFRESH_TOKEN` in secrets.

### 11.4 Cloud Run Updates
- May need to increase memory from 512Mi to 1Gi if slide generation or large email processing needs more RAM
- MCP SSE endpoint needs long-lived connections — ensure Cloud Run timeout is set appropriately (default 300s should be fine for tool calls, but review)
- Add MCP route to the health server / main app

### 11.5 New Google Drive Folders
```
CropSight Ops/
├── (existing folders...)
├── Weekly Reports/          ← HTML reports and weekly digests
├── Gantt Backups/           ← Weekly Gantt snapshots before review sessions
└── Gantt Slides/            ← Generated PPTX slides
```

---

## 12. Build Sequence

### Phase 0: Pre-Work (Day 1)
1. Tag v0.5-stable in git
2. Create KNOWN_ISSUES.md
3. Update CLAUDE.md
4. Run Supabase migration (new tables)
5. Set up new env vars
6. Create new Drive folders
7. Add python-pptx to requirements.txt

### Phase 1: Multi-Agent Foundation (Days 2-4)
1. `core/router.py` — intent classifier (Haiku)
2. `core/conversation_agent.py` — dialogue handler (Sonnet)
3. `core/analyst_agent.py` — extraction engine (Opus) — largely refactored from existing agent.py
4. `core/operator_agent.py` — execution engine (Sonnet)
5. Update `core/agent.py` to orchestrate between agents (or replace with orchestrator)
6. Ensure existing Telegram flows still work through the new agent structure
7. **Test:** Run existing test suite — all 579 tests should still pass (this is a refactor, not a rewrite)

### Phase 2: Gantt Integration (Days 5-8)
1. `services/gantt_manager.py` — read/write/snapshot/rollback operations
2. Gantt schema parser — reads spreadsheet structure, populates gantt_schema table
3. Gantt update proposal flow — propose, approve, execute, log
4. Gantt status query functions (for MCP and Telegram)
5. Integration with existing approval flow
6. **Test:** Read Gantt, propose a test update, snapshot, write, rollback, verify

### Phase 3: Debrief Flow (Days 9-11)
1. `processors/debrief.py` — session state management, extraction, injection
2. Debrief conversation flow in Conversation Agent
3. Calendar gap detection (un-transcribed purple meetings)
4. Debrief → Gantt bridge (flag Gantt-relevant items)
5. **Test:** Simulate a debrief session, verify extraction quality, test approval flow

### Phase 4: Email Intelligence (Days 12-14)
1. Enhance `schedulers/email_watcher.py` — full extraction on Gianluigi inbox
2. `schedulers/personal_email_scanner.py` — daily scan of Eyal's Gmail
3. Email filter chain implementation in `config/team.py`
4. Attachment auto-download and processing
5. Daily Email Intelligence Brief generation and approval flow
6. **Test:** Process test emails, verify filter chain, test attachment handling

### Phase 5: Meeting Prep Redesign (Days 15-17)
1. `config/meeting_prep_templates.py` — template definitions per meeting type
2. Refactor `processors/meeting_prep.py` — template-driven generation
3. Timing logic — calendar-aware generation, configurable lead times
4. Approval and distribution flow
5. **Test:** Generate prep for each meeting type, verify content quality and timing

### Phase 6: Weekly Review + Outputs (Days 18-22)
1. `processors/weekly_review.py` — compile weekly data, generate agenda
2. `processors/weekly_report.py` — HTML report generation (Jinja2 template)
3. `processors/gantt_slide.py` — PPTX generation (python-pptx)
4. Weekly review conversation flow in Conversation Agent
5. Calendar-driven scheduling in heartbeat
6. Post-output review and correction flow
7. **Test:** Full weekly review simulation — data compilation, report generation, slide generation, interactive session, corrections

### Phase 7: MCP Server (Days 23-26)
1. `services/mcp_server.py` — FastAPI endpoint with MCP protocol
2. Phase 1 MCP tools (read-only): search_memory, get_tasks, get_gantt_status, etc.
3. Authentication (bearer token)
4. get_system_context tool
5. Test with Claude.ai connection
6. **Test:** Connect from Claude.ai, verify all read tools return correct data

### Phase 8: Heartbeat Unification + Integration Testing (Days 27-30)
1. `schedulers/heartbeat.py` — unified scheduler replacing individual scheduler files
2. Wire all heartbeats (pulse, morning, evening, weekly_prep, weekly_post, alert)
3. End-to-end integration testing
4. Deploy to Cloud Run
5. Live testing with real data

### Phase 9: MCP Phase 2 (Days 31-33, after stable usage)
1. Add write tools to MCP server
2. Weekly review via Claude.ai flow
3. Session continuity (save/load session summaries)
4. Generate and view Gantt slides through Claude.ai

---

## 13. Testing Strategy

### 13.1 Test Categories

**Unit tests (per component):**
- Router: intent classification accuracy across 50+ example messages
- Analyst: extraction quality on sample transcripts, emails, debriefs
- Operator: Gantt read/write/snapshot/rollback operations
- Conversation: session state management, structured output formatting
- Gantt schema parser: correctly maps spreadsheet structure
- Email filter: correctly classifies CropSight vs. personal emails
- Meeting prep templates: correct data queries per meeting type

**Integration tests (cross-component):**
- Transcript → extraction → approval → Gantt update proposal → write → verify
- Email → classification → extraction → approval → task creation → Sheets update
- Debrief → extraction → Gantt bridge → approval → write → verify
- Weekly review → data compilation → HTML report → PPTX generation → post-review corrections
- MCP tool call → Supabase query → response formatting

**Regression tests:**
- All existing 579 tests must continue to pass
- Multi-agent refactor should not break existing Telegram commands
- Existing approval flow must work identically

**Resilience tests:**
- Gantt write to protected row → rejected
- Gantt rollback after bad write → clean restore
- Debrief with conflicting information → conflict flagged
- Email with sensitive content → classified and restricted
- Ambiguous Telegram message → clarification requested (not silent failure)
- API timeout → graceful degradation message

### 13.2 Test Data
- Use the existing MVP Focus transcript as baseline test case
- Create sample emails (5 CropSight-relevant, 5 personal) for email filter testing
- Create sample debrief inputs (various formats: long paragraph, bullets, brief notes)
- Create a test Gantt sheet (copy of real Gantt in a test spreadsheet)
- Create sample calendar events (mix of purple CropSight, personal, ambiguous)

---

## Appendix A: File Structure Changes

```
gianluigi/
├── main.py                              # Updated: initialize multi-agent + heartbeat
├── config/
│   ├── settings.py                      # Updated: new env vars
│   ├── team.py                          # Updated: personal contacts blocklist, email filters
│   └── meeting_prep_templates.py        # NEW: prep templates per meeting type
├── core/
│   ├── router.py                        # NEW: intent classifier (Haiku)
│   ├── conversation_agent.py            # NEW: dialogue handler (Sonnet)
│   ├── analyst_agent.py                 # NEW: extraction engine (Opus) — refactored from agent.py
│   ├── operator_agent.py               # NEW: execution engine (Sonnet)
│   ├── agent.py                         # DEPRECATED or refactored into orchestrator
│   ├── llm.py                           # Keep: centralized LLM helper
│   ├── system_prompt.py                 # Updated: per-agent system prompts
│   ├── tools.py                         # Updated: expanded tool definitions
│   ├── retry.py                         # Keep
│   ├── error_alerting.py                # Keep
│   └── logging_config.py               # Keep
├── models/
│   └── schemas.py                       # Updated: new Pydantic models for Gantt, debrief, email, MCP
├── services/
│   ├── supabase_client.py               # Updated: new table operations
│   ├── telegram_bot.py                  # Updated: debrief flow, weekly review commands
│   ├── gmail.py                         # Updated: personal email scanning
│   ├── google_drive.py                  # Updated: structural awareness, new folders
│   ├── google_sheets.py                 # Updated: Gantt read/write operations
│   ├── google_calendar.py               # Updated: gap detection, weekly review scheduling
│   ├── gantt_manager.py                 # NEW: Gantt CRUD, schema, versioning, rollback
│   ├── mcp_server.py                    # NEW: MCP endpoint for Claude.ai
│   ├── embeddings.py                    # Updated: new source types, lifecycle
│   ├── conversation_memory.py           # REFACTORED: structured session state
│   ├── health_server.py                 # Updated: MCP route added
│   └── word_generator.py               # Keep
├── processors/
│   ├── transcript_processor.py          # Refactored: uses Analyst Agent
│   ├── cross_reference.py               # Keep: task dedup, status inference
│   ├── entity_extraction.py             # Keep: enhanced for email/debrief sources
│   ├── proactive_alerts.py              # Keep
│   ├── meeting_prep.py                  # REWRITTEN: template-driven
│   ├── weekly_digest.py                 # Updated: part of weekly review flow
│   ├── weekly_review.py                 # NEW: weekly review data compilation
│   ├── weekly_report.py                 # NEW: HTML report generation
│   ├── gantt_slide.py                   # NEW: PPTX generation
│   ├── debrief.py                       # NEW: debrief session management
│   ├── email_intelligence.py            # NEW: email extraction and brief generation
│   └── document_processor.py            # Keep
├── guardrails/
│   ├── approval_flow.py                 # Updated: Gantt approvals, debrief approvals
│   ├── calendar_filter.py               # Keep
│   ├── sensitivity_classifier.py        # Updated: email and debrief sources
│   ├── content_filter.py                # Keep
│   ├── inbound_filter.py               # Keep
│   └── gantt_guard.py                   # NEW: Gantt write protection rules
├── schedulers/
│   ├── heartbeat.py                     # NEW: unified heartbeat scheduler
│   ├── transcript_watcher.py            # Moved into heartbeat pulse
│   ├── document_watcher.py              # Moved into heartbeat pulse
│   ├── meeting_prep_scheduler.py        # Moved into heartbeat morning
│   ├── weekly_digest_scheduler.py       # Moved into heartbeat weekly_post
│   ├── email_watcher.py                 # Updated: moved into heartbeat pulse
│   ├── personal_email_scanner.py        # NEW: daily scan, heartbeat morning
│   ├── task_reminder_scheduler.py       # Moved into heartbeat evening
│   ├── alert_scheduler.py               # Moved into heartbeat alert
│   └── orphan_cleanup_scheduler.py      # Moved into heartbeat weekly_post
├── templates/
│   └── weekly_report.html               # NEW: Jinja2 template for HTML report
├── scripts/
│   ├── setup_supabase.sql               # Updated: new tables
│   ├── migrate_v1.sql                   # NEW: migration from v0.5 schema
│   ├── seed_entities.py                 # Keep
│   ├── upload_secrets.py                # Updated: new secrets
│   └── parse_gantt_schema.py            # NEW: reads Gantt structure, populates gantt_schema
├── tests/                               # Expanded significantly
├── Dockerfile                           # Updated: python-pptx, jinja2
├── cloudbuild.yaml                      # Keep
├── requirements.txt                     # Updated: new dependencies
├── CLAUDE.md                            # Updated: v1.0 context
├── V1_DESIGN.md                         # THIS DOCUMENT
└── KNOWN_ISSUES.md                      # NEW: v0.5 known issues
```

---

## Appendix B: Reference PPTX Layout (Gantt Slide)

The weekly Gantt slide should match the structure of `CropSight_Gantt_Slide_Q1Q2_2026.pptx`:

- **Slide dimensions:** Standard 10" x 7.5" (widescreen)
- **Header:** Dark bar at top with title, subtitle (week info), and legend
- **Week columns:** ~16-17 weeks visible, each ~446442 EMU wide
- **Current week indicator:** Colored marker with "You are here" label
- **Section rows:** 5 main sections with colored left labels, each containing positioned bar shapes for timeline items
- **Bar colors:** Green (active), Blue (planned), Red (blocked), Gray (completed) — use semi-transparent fills
- **Milestone markers:** Diamond shapes (for milestone events) and star shapes (for major milestones)
- **Owner annotations:** [E], [R], [P], [Y] prefixed to bar labels
- **Capacity annotations:** Red-tinted overlay on weeks with reduced capacity
- **Q3 horizon bar:** Bottom summary bar with upcoming major milestones
- **Footer:** Company name, version, confidential notice, generation date

Reference file is available for the slide generator to measure exact positions from.

---

*End of v1.0 Design Document*
*This file should be placed in the repo root and referenced from CLAUDE.md*
