# Phase 10 Addition: Dynamic Canonical Projects System

## Context

The Phase 10 plan has canonical project names hardcoded in `config/projects.py`. This is wrong — it'll go stale within a month and requires a code deploy to update. We need to make it dynamic using the same pattern Gianluigi already uses everywhere: auto-discover → propose → Eyal approves.

This replaces the static `config/projects.py` approach. All other Phase 10 items remain unchanged.

## What to Build

### 1. Supabase Table (migration script)

```sql
CREATE TABLE canonical_projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    aliases TEXT[] DEFAULT '{}',
    status TEXT DEFAULT 'active',  -- active, archived
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE unmatched_labels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label TEXT NOT NULL,
    meeting_id UUID REFERENCES meetings(id),
    meeting_title TEXT,
    context TEXT,  -- brief context of where this label appeared
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_unmatched_labels_created ON unmatched_labels(created_at);
```

Seed with current projects:
- Moldova Pilot / wheat yield PoC, Gagauzia region / aliases: ["Moldova PoC", "Gagauzia project", "Moldova wheat", "Moldova delivery"]
- Pre-Seed Fundraising / IIA Tnufa + next round / aliases: ["fundraising", "Tnufa", "investor round"]
- SatYield Accuracy Model / core ML product / aliases: ["the model", "accuracy model", "yield model"]
- Product V1 / first commercial version / aliases: ["MVP", "product launch"]
- Business Plan / financial projections + strategy / aliases: ["business model", "financial plan"]
- EU Grant / European funding programs / aliases: ["EU funding", "European grant", "Horizon"]
- Website & Marketing / cropsight.io + content / aliases: ["website", "marketing", "landing page"]
- Investor Outreach / angel + fund pipeline / aliases: ["investor pipeline", "outreach", "angel investors"]
- Operational Tooling / Gianluigi system / aliases: ["Gianluigi", "ops tooling", "AI assistant"]
- Team & HR / hiring, roles, operations / aliases: ["hiring", "team building", "HR"]

### 2. Update Extraction Prompt

Replace the hardcoded canonical names list in `core/system_prompt.py` with a dynamic read from `canonical_projects` table.

In `processors/transcript_processor.py` (or wherever the extraction prompt is assembled):
- Query `canonical_projects` where status='active'
- Build the LABEL EXTRACTION RULES section dynamically, including aliases
- Pass into the extraction prompt

Label matching logic during/after extraction:
- Exact match on canonical name → use it
- Match on any alias → use the parent canonical name
- No match → store in `unmatched_labels` table with meeting context, AND still use the label as-is on the decision/task (don't block extraction)

### 3. Weekly Review Integration

Add a "New Labels" subsection to the weekly review attention section in `processors/weekly_review.py`:

Query `unmatched_labels` from the past 7 days, group by label (case-insensitive), count occurrences:

```
📋 New Labels This Week (not in canonical projects):
- "EU Grant Application" — appeared in 2 meetings (Advisory Sync Mar 20, Founders Review Mar 22)
- "Advisory Board Setup" — 1 meeting, 2 tasks
- "Data Pipeline Refactor" — 1 meeting

→ Use add_canonical_project() to promote, or merge_topic_threads() to combine with existing.
```

Only show labels that appeared in 1+ meetings. Skip labels that match existing canonical names or aliases (they shouldn't be in unmatched_labels, but defensive check).

### 4. Two New MCP Tools

```
[PROJECTS] add_canonical_project(name: str, description: str, aliases: list[str] = []) → confirmation
  # Adds to canonical_projects table
  # Retroactively updates any unmatched_labels entries that match the new name/aliases
  # Retroactively updates any decisions/tasks with matching labels to use the canonical name

[PROJECTS] list_canonical_projects(status: str = "active") → project list
  # Returns all canonical projects with their aliases
```

### 5. Update Claude.ai Project Prompt (Track B)

The canonical project names section in B3 should note:

```
CROPSIGHT PROJECTS:
(loaded dynamically — call list_canonical_projects() for current list)

When Eyal mentions a project variation, use the canonical name for tool calls.
When the weekly review surfaces new unmatched labels, ask Eyal whether to
add them as canonical projects, merge into existing ones, or skip.
```

### 6. Delete config/projects.py

After migration + seeding, this file is no longer needed. The Supabase table is the single source of truth. Remove any imports of CANONICAL_PROJECT_NAMES from other files.

## What NOT to Build (Phase 11)

- Auto-suggestion of merges ("EU Grant Application" looks similar to "EU Grant" — merge?)
- Automatic alias detection from embedding similarity
- Project archival automation (archive projects with no mentions in 60+ days)
- Project-level dashboards or analytics

For now, the human does the intelligence during weekly review. The system just surfaces and stores.

## Implementation Notes

- This adds ~0.5 days to Phase 10 estimate (total: 3.5-4.5 days)
- The 2 new MCP tools bring the total to 35 (update B1 tool reference accordingly)
- Add [PROJECTS] category prefix to both new tools
- The unmatched_labels table can be cleaned up periodically — delete entries older than 90 days or entries that have been promoted to canonical projects
- Seed migration should run as part of the Phase 10 migration script alongside any Phase 9 migrations not yet applied
