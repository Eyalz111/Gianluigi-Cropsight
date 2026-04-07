# v2.2 Session 3 Plan: Deal Intelligence + Gantt UX

**Status:** COMPLETE. Implemented 2026-04-07. Migrations applied. 84 new tests + 3 updated.
**Created:** 2026-04-07

---

## Phase 4: Deal & Relationship Intelligence [Size: L]

**Problem:** Zero visibility into commercial reality. Morning brief only shows internal operations.

**Depends on:** Sensitivity tiers (for deal distribution filtering)

### Migration SQL (`scripts/migrate_v2_deals.sql`):

```sql
CREATE TABLE IF NOT EXISTS deals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    organization TEXT NOT NULL,
    contact_person TEXT,
    stage TEXT DEFAULT 'lead',  -- lead/contacted/meeting_held/proposal/negotiation/pilot/closed_won/closed_lost/on_hold
    value_estimate TEXT,        -- text not numeric (pre-revenue, amounts are fuzzy)
    probability INTEGER,        -- 0-100
    owner TEXT DEFAULT 'Eyal',
    next_action TEXT,
    next_action_date DATE,
    last_interaction_date DATE,
    source TEXT,                -- how we found them
    notes TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS deal_interactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id UUID REFERENCES deals(id),
    interaction_type TEXT NOT NULL,  -- meeting/email/call/note
    summary TEXT NOT NULL,
    date DATE NOT NULL,
    source_id UUID,           -- meeting_id or email_scan_id (nullable)
    source_type TEXT,         -- 'meeting', 'email', 'manual'
    created_by TEXT DEFAULT 'gianluigi',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS external_commitments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deal_id UUID REFERENCES deals(id),       -- nullable (commitment may not be deal-linked)
    organization TEXT NOT NULL,
    contact_person TEXT,
    commitment TEXT NOT NULL,                 -- what was promised
    promised_by TEXT DEFAULT 'Eyal',          -- who on our side promised
    promised_to TEXT,                         -- who on their side
    deadline DATE,
    status TEXT DEFAULT 'open',              -- open/fulfilled/overdue/cancelled
    source_meeting_id UUID,                  -- where the promise was made
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_deals_stage ON deals(stage);
CREATE INDEX idx_deals_next_action_date ON deals(next_action_date);
CREATE INDEX idx_deals_last_interaction ON deals(last_interaction_date);
CREATE INDEX idx_deal_interactions_deal ON deal_interactions(deal_id);
CREATE INDEX idx_external_commitments_deadline ON external_commitments(deadline);
CREATE INDEX idx_external_commitments_status ON external_commitments(status);
CREATE INDEX idx_external_commitments_deal ON external_commitments(deal_id);
```

### Files to create:
- `processors/deal_intelligence.py` — Deal signal detection, deal pulse generation, auto-interaction creation

### Files to modify:
- `services/supabase_client.py` — CRUD: `create_deal()`, `update_deal()`, `get_deals()`, `get_deal_timeline()`, `get_overdue_deal_actions()`, `get_stale_deals()`, `create_deal_interaction()`, `create_external_commitment()`, `update_external_commitment()`, `get_overdue_commitments()`
- `services/mcp_server.py`:
  - Remove deprecated `get_commitments` tool (-1 tool)
  - Add `deal_ops` composite tool (+1 tool): action parameter routes to get/create/update/timeline/commitment
  - `deal_ops(action="commitment", ...)` — create/update/list external commitments (no separate tool)
  - Net MCP tool count: stays at 43
- `services/google_sheets.py` — Add 3 columns to stakeholder sheet: Deal Stage, Deal Value, Last Interaction. Update `STAKEHOLDER_COLUMNS` and `get_all_stakeholders()`.
- `processors/morning_brief.py` — New "Deal Pulse" section (max 3 items: overdue follow-ups + stale deals) AND "External Commitments" section (overdue promises to external parties)
- `processors/transcript_processor.py` — After extraction, call deal signal detection (non-fatal, like entity extraction)
- `core/system_prompt.py` — Add deal signal AND external commitment extraction guidance to Opus extraction prompt

### External commitments vs deal interactions:
- `deal_interactions`: what happened (meeting, email, call) — historical record
- `external_commitments`: what was promised to an external party with a deadline — forward-looking obligation
- A stale deal = no recent contact. An overdue commitment = a broken promise. Different alerts, different urgency.
- Morning brief shows both separately: "Deal Pulse" for stale deals, "Commitments Due" for external promises

### Key design decisions (from web research):
- Start with 10-15 key contacts, not comprehensive DB
- ONE staleness rule: 7 days no contact → flag as stale
- Zero-friction: auto-create deal_interactions from meetings/emails, manual only for stage changes
- Deal Pulse capped at 3 items in morning brief (alert fatigue research: 4+ causes avoidance)
- Commitments Due capped at 3 items (same discipline)
- Auto-created deals from signals start as LEAD — require manual promotion
- Stakeholder sheet evolves into "Deals & Relationships" (add columns, don't restructure)

### Tests: ~45 new (CRUD, deal_ops MCP tool, deal pulse, signal detection, Sheets columns, stale/overdue queries, external commitments CRUD).

---

## Phase 5: Task/Gantt UX Enhancement [Size: M]

**Problem:** No single "what should I do today" view. Gantt disconnected from task reality.

**Depends on:** Phase 4 (deal pulse for composite view). Phase 3 (task replies) is a soft dependency.

### Files to modify:
- `services/mcp_server.py` — Extend `get_full_status()` with `view: str = "standard"` parameter. `view="ceo_today"` adds: overdue tasks with titles, this week's tasks, Gantt milestones, deal pulse, drift alerts.
- `processors/gantt_intelligence.py` — New function `detect_gantt_drift()`: compare Gantt status vs task completion rates, flag mismatches
- `processors/morning_brief.py` — 3 new sections: "Task Urgency" (max 3 high-priority overdue), "Gantt Milestones This Week", "Drift Alerts". Each capped at 2-3 lines. Empty sections omitted.

### Gantt drift detection:
- Query Gantt items marked active/in-progress
- Query tasks in same category/label
- If >50% of tasks in a Gantt area are overdue but Gantt shows "on track" → drift alert
- Returns: `[{section, gantt_status, overdue_task_count, total_tasks, drift_description}]`

### Morning brief stays under 3 minutes. No new MCP tools — extends existing `get_full_status`.

### Tests: ~18 new (ceo_today view, standard view unchanged, drift detection, morning brief sections, empty section omission, brief length cap).

---

## MCP Tool Count Ledger

| Action | Tool | Delta | Running Total |
|--------|------|-------|---------------|
| Starting count | — | — | 43 |
| Remove deprecated | `get_commitments` | -1 | 42 |
| Add deal composite | `deal_ops` | +1 | 43 |
| Extend existing | `get_full_status` (add `view` param) | 0 | 43 |
| **Final total** | — | — | **43** |

---

## Summary

| Phase | Feature | Size | New Tests | Migration |
|-------|---------|------|-----------|-----------|
| 4 | Deals + External Commitments + Stakeholders | L | ~45 | Yes (3 new tables) |
| 5 | Task/Gantt UX | M | ~18 | No |
| **Total** | | | **~63** | **1 SQL** |

## Web Research Warnings (apply throughout)
- Start deals with 10-15 contacts max
- Morning brief under 3 minutes, lead with decisions
- 1-2 nudges per session, batch everything else
- Zero-friction data entry (infer from meetings/emails)
- "Proposes, Eyal approves" is validated best practice
- Avoid the 44th tool trap (removing get_commitments to add deal_ops keeps it at 43)
