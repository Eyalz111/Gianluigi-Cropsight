# Phase 5: Meeting Prep Redesign — Planning Prompt for Claude Code

## Context

We've completed a full architecture review of Gianluigi post-Phase 4 (933 tests passing). Before implementing Phase 5, we produced an architecture review document (`ARCHITECTURE_REVIEW_ISSUES.md`) with 12 items — several of which are prerequisites or interact directly with Phase 5. We then designed the Phase 5 meeting prep redesign and stress-tested it from operational, technical, and resilience perspectives.

This prompt gives you the refined Phase 5 design with all decisions resolved. Please enter plan mode and produce a detailed implementation plan.

---

## Phase 5 Design: Meeting Prep Redesign

### Core Shift
From "blind generate → approve" to "**propose → discuss → generate → approve → distribute**"

Every meeting prep goes through an outline proposal stage where Eyal can adjust focus before full generation. No content is ever distributed to the team without Eyal's explicit approval.

### Operational Flow

**~24h before meeting (configurable per template):**
1. Scheduler detects meeting needs prep (via Meeting Selection Logic — see below)
2. Gianluigi sends Telegram **OUTLINE PROPOSAL** to Eyal with:
   - Meeting name, time, participants
   - Proposed data sections based on meeting-type template
   - Suggested agenda items
   - Inline keyboard: [Generate as-is] [Add focus] [Skip this prep]

3. Eyal responds:
   - **"Generate as-is"** → proceed to full generation
   - **"Add focus"** → enters a short interactive session (1-3 exchanges). Eyal provides focus instructions (e.g., "focus on MVP timeline, also check what Paolo said about Lavazza"). Gianluigi processes the instructions — this CAN include RAG searches, entity lookups, and context gathering to properly understand and incorporate Eyal's request. Gianluigi regenerates the outline incorporating the focus, shows updated version → [Generate] [Edit more] [Skip]
   - **"Skip this prep"** → no prep generated, logged in action_log
   - **No response** → follows the reminder/escalation cycle (see Non-Response Handling below)

**~12h before meeting (configurable):**
4. Full generation runs with:
   - Meeting-type-specific template (structured data queries, section layout, focus areas)
   - Eyal's focus adjustments (if any) passed as prompt context to Sonnet
   - Gantt snapshot for the relevant section(s)
   - Template-driven context gathering (not generic semantic search)

5. Submit for approval via existing approval_flow:
   - "Here's the prep doc for Founders Technical Review. [Approve] [Edit] [Reject]"

**On approval:**
6. Distribute to participants: email + Telegram group + Google Drive (.docx + .md)

### Output Formats
- **Outline proposal** = Telegram message only (lightweight, inline keyboard buttons)
- **Full prep doc** = Google Doc (.docx via word_generator.py) uploaded to Drive (Meeting Prep/ folder) + Telegram summary message + Gmail to meeting participants
- **Approval request** = Telegram DM with formatted preview + link to full doc in Drive

### Non-Response Handling (CRITICAL — aligns with ARCHITECTURE_REVIEW_ISSUES.md Item 12)

Meeting prep outline proposals are **non-blocking queue items** — they go into `pending_approvals` alongside morning briefs and transcript approvals. They do NOT lock the Telegram conversation or block other flows.

The "Add focus" multi-turn conversation IS an interactive session (uses session locking), but it's brief (1-3 exchanges) and follows existing debrief-style patterns.

**Reminder/escalation cycle for outline proposals:**
1. Outline sent → wait
2. After 4 hours with no response → Reminder #1: "Meeting prep outline for '[title]' is still waiting. [Generate as-is] [Add focus] [Skip]"
3. After 4 more hours (8h total) with no response → Reminder #2: "Last call on prep for '[title]' — I'll auto-generate with defaults if no response."
4. After 4 more hours (12h total) with no response → **Auto-generate with default template**, then submit to Eyal for approval. NEVER distribute to team without approval.

**Reminder/escalation cycle for full prep approval:**
- Same as meeting summary approval flow (defined in Item 1 of ARCHITECTURE_REVIEW_ISSUES.md): reminders at 2h and 6h, stays pending indefinitely. Never auto-distributes.

**Queue awareness:**
- Morning brief should acknowledge pending prep outlines: "You also have a prep outline waiting for tomorrow's Technical Review"
- Debrief start should mention pending prep items: "Before we start — you have a prep outline pending for tomorrow's meeting. Handle that first?"
- `/status` command shows all pending prep items in the queue

### Compressed Timeline Fallback

Meetings don't always appear on the calendar 36+ hours in advance. The system MUST handle late-detected meetings gracefully. The outline proposal stage should ALWAYS happen — even if compressed.

**Rules based on time until meeting:**

| Time until meeting | Behavior |
|---|---|
| >24h | Normal flow: outline proposal → wait for response → generate → approve → distribute |
| 12-24h | Compressed: outline proposal sent immediately with shorter reminder cycle (2h/2h/4h instead of 4h/4h/4h). Generation happens as soon as outline is approved or after 4h auto-generate. |
| 6-12h | Urgent: outline proposal sent immediately with note "Meeting is in [X]h — responding quickly helps me prepare better." Single reminder at 2h, auto-generate at 4h. |
| <6h | Emergency: outline proposal sent but simultaneously auto-generate with defaults in background. If Eyal responds to outline before generation completes, incorporate his input. If generation completes first, send for approval with note "Generated with defaults — you didn't have time to review the outline." |
| <2h | Skip prep entirely. Too late to be useful. Log it: "Prep skipped for '[title]' — detected too late ([X]h before meeting)." |

All timelines should be configurable per meeting type in the template config.

### Meeting Selection Logic

**Two sources, merged:**
1. **Gantt Meeting Cadence tab** (PRIMARY) — defines which meetings get prep and what template. Already parsed by `gantt_manager.get_meeting_cadence()`.
2. **Google Calendar** (TIMING) — provides actual schedule, time, attendees.

**Matching calendar events to templates — robust approach:**

Title fuzzy-match alone is fragile. Implement a scoring system:

```
match_score = 0

# Signal 1: Title fuzzy match (strongest)
if fuzzy_ratio(calendar_title, template.match_titles) > 80:
    match_score += 3

# Signal 2: Participant set match
if set(calendar_participants) == set(template.expected_participants):
    match_score += 2
elif set(template.expected_participants).issubset(set(calendar_participants)):
    match_score += 1

# Signal 3: Day-of-week match (from cadence)
if calendar_day_of_week == template.expected_day:
    match_score += 1

# Signal 4: Previously matched (learned from past calendar_classifications)
if calendar_title in learned_matches_for_template:
    match_score += 2

# Threshold
if match_score >= 3: → use this template, auto-prep if configured
if match_score == 2: → use this template but ask Eyal to confirm ("Is this a Founders Technical Review?")
if match_score < 2:  → no match, use generic template only if Eyal requests
```

Store confirmed matches in `calendar_classifications` (already exists) so the system learns: "Team call with Eyal+Roye+Paolo+Yoram on Tuesdays = Founders Technical Review." Over time, match_score improves as Signal 4 kicks in.

**Selection rules (configurable in `config/meeting_prep_templates.py`):**

| Meeting | Auto-prep? | Template |
|---|---|---|
| Founders Technical Review | Yes | founders_technical |
| Founders Business Review | Yes | founders_business |
| Monthly Strategic & Operational Review | Yes | monthly_strategic |
| CEO-CTO weekly | No (too frequent/informal) | — |
| Commercial Sync | No | — |
| Bookkeeping | No | — |
| Any other CropSight meeting | Only if Eyal asks | generic |

### Meeting Prep Templates

**New file: `config/meeting_prep_templates.py`**

Each template defines:
```python
MEETING_PREP_TEMPLATES = {
    "founders_technical": {
        "display_name": "Founders Technical Review",
        "match_titles": ["founders technical", "technical review", "tech review"],
        "expected_participants": ["eyal", "roye", "paolo", "yoram"],
        "expected_day": None,  # or "Tuesday" if recurring
        "auto_prep": True,
        "outline_lead_hours": 24,
        "generation_lead_hours": 12,
        "data_queries": [
            {"type": "tasks", "filter": {"assignee": "Roye", "status": "pending"}},
            {"type": "tasks", "filter": {"status": "overdue"}},
            {"type": "decisions", "scope": "last_meeting_same_type"},
            {"type": "gantt_section", "section": "Product & Technology"},
            {"type": "open_questions"},  # all open — Sonnet decides relevance during synthesis
            {"type": "commitments", "filter": {"assignee": "Roye"}},
        ],
        "structure": [
            "open_tasks_by_assignee",
            "follow_up_from_last",
            "gantt_snapshot",
            "open_questions",
            "commitment_status",
            "suggested_agenda",
        ],
        "focus_areas": "Technical execution, product timeline, blockers",
    },
    "founders_business": {
        "display_name": "Founders Business Review",
        "match_titles": ["founders business", "business review"],
        "expected_participants": ["eyal", "roye", "paolo", "yoram"],
        "auto_prep": True,
        "outline_lead_hours": 24,
        "generation_lead_hours": 12,
        "data_queries": [
            {"type": "tasks", "filter": {"assignee": "Paolo", "status": "pending"}},
            {"type": "tasks", "filter": {"status": "overdue"}},
            {"type": "decisions", "scope": "last_meeting_same_type"},
            {"type": "gantt_section", "section": "Business Development"},
            {"type": "gantt_section", "section": "Commercial"},
            {"type": "open_questions"},
            {"type": "commitments", "filter": {"assignee": "Paolo"}},
            {"type": "entity_timeline", "filter": {"type": "organization", "recent_days": 30}},
        ],
        "structure": [
            "open_tasks_by_assignee",
            "follow_up_from_last",
            "gantt_snapshot",
            "stakeholder_updates",
            "open_questions",
            "commitment_status",
            "suggested_agenda",
        ],
        "focus_areas": "Pipeline, partnerships, client relationships, commercial progress",
    },
    "monthly_strategic": {
        "display_name": "Monthly Strategic & Operational Review",
        "match_titles": ["monthly strategic", "strategic review", "monthly review", "operational review"],
        "expected_participants": ["eyal", "roye", "paolo", "yoram"],
        "auto_prep": True,
        "outline_lead_hours": 36,  # more lead time — heavier doc
        "generation_lead_hours": 24,
        "data_queries": [
            {"type": "tasks", "filter": {"status": "all"}},  # full task inventory
            {"type": "decisions", "scope": "last_30_days"},
            {"type": "gantt_section", "section": "all"},  # full Gantt snapshot
            {"type": "open_questions"},
            {"type": "commitments", "filter": {"status": "all"}},
            {"type": "entity_timeline", "filter": {"type": "organization", "recent_days": 30}},
        ],
        "structure": [
            "executive_summary",
            "task_scorecard",
            "commitment_scorecard",
            "gantt_full_snapshot",
            "key_decisions_last_month",
            "open_questions",
            "strategic_items",
            "suggested_agenda",
        ],
        "focus_areas": "Company progress, strategic alignment, resource allocation, milestone tracking",
    },
    "generic": {
        "display_name": "General Meeting",
        "match_titles": [],
        "auto_prep": False,  # only on request
        "outline_lead_hours": 12,
        "generation_lead_hours": 6,
        "data_queries": [
            {"type": "tasks", "filter": {"status": "pending"}},
            {"type": "decisions", "scope": "related_by_participants"},
            {"type": "open_questions"},
        ],
        "structure": [
            "relevant_open_tasks",
            "recent_decisions",
            "open_questions",
            "suggested_agenda",
        ],
        "focus_areas": "General context and open items",
    },
}
```

### Data Query Implementation Notes

- `{"type": "tasks", "filter": {...}}` → SQL query against the `tasks` table. Filters map to WHERE clauses. Straightforward.
- `{"type": "decisions", "scope": "last_meeting_same_type"}` → This requires knowing the meeting type. When a transcript is processed and matched to a cadence entry, store the `meeting_type` tag on the `meetings` table record. Then this query becomes: `SELECT * FROM decisions WHERE meeting_id = (SELECT id FROM meetings WHERE meeting_type = ? ORDER BY date DESC LIMIT 1)`. **Add a `meeting_type` column to the `meetings` table in the Phase 5 SQL migration.**
- `{"type": "open_questions"}` → Pull ALL open questions. Don't try to filter by "technical" vs "business" — let Sonnet decide relevance during synthesis based on the template's focus_areas. Keep it simple.
- `{"type": "gantt_section", "section": "Product & Technology"}` → Calls `gantt_manager.get_gantt_section()`. Verify that the current Gantt sheet has identifiable section headers. If it uses merged cells or specific row formatting, the gantt_manager needs to parse that. If the Gantt is a flat list, the section-to-rows mapping needs to be stored in the template config or `gantt_schema` table.
- `{"type": "entity_timeline", ...}` → Calls `supabase_client.get_entity_mentions()` filtered by type and recency.

### "Add Focus" Flow — Detailed Design

This is NOT just passing text to Sonnet. When Eyal says "also check what Paolo said about Lavazza," Gianluigi should:

1. Parse the instruction using Haiku (classify: is this a data request, a structural change, or a focus shift?)
2. If data request ("check what Paolo said about Lavazza"):
   - Search RAG for "Paolo Lavazza" → find relevant chunks
   - Search entity mentions for "Lavazza" → find context
   - Add the found context as a new data section in the outline
   - Show updated outline: "Added: Paolo's Lavazza reference from Feb 28 Founders Business Review"
3. If structural change ("skip the stakeholder section"):
   - Remove that section from the template structure
   - Show updated outline
4. If focus shift ("focus on MVP timeline"):
   - Add as a priority instruction that gets prepended to the Sonnet synthesis prompt
   - Show updated outline with the focus noted

This requires a small Haiku classification step + conditional RAG/entity lookup. Implement as a handler in `processors/meeting_prep_v2.py` with a method like `process_focus_instruction(instruction_text, current_outline, template)`.

Session state for the "Add focus" flow should be stored in the `pending_approvals` record (extend the `metadata` JSONB field) — NOT in a separate table. The outline proposal and focus adjustments are all part of the same approval lifecycle.

### Gantt Integration

Each template specifies which Gantt section(s) to include. The prep generator calls:
```python
gantt_data = await gantt_manager.get_gantt_section(
    section=template["gantt_section"],
    weeks=[current_week - 1, current_week, current_week + 1]
)
```

This should return: row items with status, milestones, what's behind/ahead of schedule. Format as a concise table in the prep doc.

**Verify:** Does the current Gantt in Google Sheets have named section headers that can be parsed programmatically? If sections are implicit (just row groupings without explicit headers), the section-to-row mapping needs to be defined somewhere — either in the template config or in `gantt_schema`.

### Interaction with Heartbeat System (ARCHITECTURE_REVIEW_ISSUES.md Item 7)

The meeting prep scheduler should integrate with the unified heartbeat system:
- Heartbeat checks upcoming calendar events and calculates exact trigger times based on each template's `outline_lead_hours`
- Instead of polling every 2 hours, the heartbeat schedules precise wake-ups: "Wake me at 14:00 tomorrow to send the outline for Wednesday's Technical Review"
- If a calendar event is added/moved/cancelled, the next heartbeat cycle detects the change and adjusts the prep schedule accordingly

If the heartbeat isn't implemented yet when Phase 5 ships, fall back to the current hourly polling — but design the scheduler interface so it can be swapped to heartbeat-driven later without refactoring.

### Cost Model

| Step | Model | Est. tokens | Cost |
|---|---|---|---|
| Outline proposal generation | Haiku | ~500 in, ~300 out | ~$0.001 |
| Focus instruction classification | Haiku | ~300 in, ~100 out | ~$0.0005 |
| Focus RAG search (if needed) | — | API calls only | ~$0.001 (embedding) |
| Full prep synthesis | Sonnet | ~3K in, ~1K out | ~$0.02 |
| Total per meeting prep | | | ~$0.02–0.03 |

### Files (New + Modified)

| Action | File | Purpose |
|---|---|---|
| Create | `config/meeting_prep_templates.py` | Template definitions per meeting type |
| Create | `processors/meeting_prep_v2.py` | New prep generator: outline, focus processing, template-driven generation |
| Rewrite | `schedulers/meeting_prep_scheduler.py` | New scheduling logic with outline proposal flow + compressed timeline fallback |
| Modify | `services/telegram_bot.py` | Outline proposal handlers, inline keyboard callbacks, focus conversation flow |
| Modify | `services/supabase_client.py` | Add `meeting_type` column to meetings, prep session queries |
| Modify | `guardrails/approval_flow.py` | Add `prep_outline` content type, expiry logic for prep items |
| Modify | `services/gantt_manager.py` | Add `get_gantt_section()` helper if not already robust enough |
| Keep | `processors/meeting_prep_generator.py` | Rename to `meeting_prep_v1.py`, keep as fallback, reuse shared utility functions |
| Create | `scripts/migrate_phase5.sql` | Add `meeting_type` to meetings table, any new columns needed |
| Create | `tests/test_meeting_prep_v2.py` | Full test coverage for new flow |

### Data Model Notes

- Store Eyal's focus adjustments and approval edits in the `pending_approvals.metadata` JSONB field (e.g., `{"focus_instructions": [...], "outline_version": 2, "template_used": "founders_technical", "compressed_timeline": false}`). This captures feedback for future template refinement.
- Add `meeting_type TEXT` column to `meetings` table — set during transcript processing when matched to a cadence entry. Used by `"scope": "last_meeting_same_type"` queries.
- Prep outline proposals use `pending_approvals` with `content_type='prep_outline'`. Full prep docs use `content_type='meeting_prep'` (existing).

### Prerequisites from Architecture Review

The following items from `ARCHITECTURE_REVIEW_ISSUES.md` should ideally be implemented before or alongside Phase 5:

- **Item 1** (Disable auto-publish) — Must be done. Prep docs must never auto-distribute.
- **Item 12** (Non-response resilience) — Directly affects this phase. Outline proposals are queue items, focus conversations use session locking, expiry logic needed.
- **Item 5** (Relax polling intervals) — Affects scheduler timing precision.
- **Item 7** (Unified heartbeat) — The ideal scheduling backbone for prep triggers. If not ready, Phase 5 can ship with polling fallback.
- **Item 3** (Health monitoring) — Prep scheduler failures should trigger alerts.
- **Item 11** (Friday weekly digest) — No direct dependency, but confirms Israeli work-week awareness in all scheduling.

### Open Questions for Planning

1. Does the Gantt sheet have programmatically identifiable section headers? Need to verify before designing `get_gantt_section()`.
2. What's the exact structure of the `gantt_schema` table — can it store section-to-row mappings?
3. Should the "Add focus" flow support image/file attachments from Eyal (e.g., "here's a doc to include in prep"), or text-only for v1?
4. For the compressed timeline fallback: should emergency mode (<6h) generate a lighter doc (fewer sections) or the full template?
