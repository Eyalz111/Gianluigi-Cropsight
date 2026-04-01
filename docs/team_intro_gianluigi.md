# Gianluigi — CropSight's AI Operations Assistant

## What Is Gianluigi?

Gianluigi is our AI-powered operations assistant — an "AI Office Manager" that listens to our meetings, remembers everything discussed, tracks what we committed to, and keeps me (Eyal) on top of the full operational picture.

Named after Gianluigi Buffon — always there, quietly catching everything that matters.

## What It Does Today

### Meeting Intelligence
- **Processes every meeting transcript** automatically (via Tactiq)
- **Extracts** decisions, action items, open questions, follow-ups, and stakeholder mentions
- **Sends you summaries** via email with a Word document attachment
- **Tracks topics across meetings** — knows that "Moldova" discussed today connects to what we said last week

### Task & Decision Tracking
- **Google Sheets dashboard** — Tasks sheet (who owes what, by when) and Decisions sheet (what we decided, why, confidence level)
- **Labels everything** with project names (Moldova Pilot, Pre-Seed Fundraising, SatYield, etc.)
- **Detects duplicates** — if the same task comes up in two meetings, it updates rather than duplicates

### CEO Dashboard (via Claude.ai)
- **35 operational tools** accessible through natural conversation
- "What's the status?" → pulls Gantt, tasks, decisions, calendar, alerts
- "What did we decide about Moldova?" → searches across all meetings
- "Show me topic threads" → cross-meeting evolution of each project
- Works in Hebrew too

### Sensitivity & Distribution
- **Auto-classifies meetings** as normal or sensitive (investor calls, legal, equity discussions)
- **Sensitive meetings** → summary goes to Eyal only
- **Normal meetings** → summary distributed to full team
- **CEO approval gate** — nothing goes to the team without Eyal's review and approval

### Gantt Integration
- Reads the operational Gantt chart (Google Sheets)
- Computes velocity, slippage ratio, milestone risks
- Proposes updates based on meeting outcomes (with approval)

## How It Works (Simplified)

```
Meeting (Tactiq transcript)
        ↓
   Google Drive
        ↓
   Gianluigi (Cloud Run, Europe)
        ↓
   Claude AI extracts structured data
        ↓
   Eyal reviews & approves (Telegram)
        ↓
   ┌────────────────────┐
   │  Team email + docs  │
   │  Google Sheets      │
   │  Supabase DB        │
   │  Institutional      │
   │  memory (RAG)       │
   └────────────────────┘
```

## Tech Under the Hood

| What | How |
|------|-----|
| AI Brain | Claude API (3 model tiers: Opus for accuracy, Sonnet for reasoning, Haiku for speed) |
| Memory | Supabase (PostgreSQL + vector search), 1536-dimension embeddings |
| Hosting | Google Cloud Run (Frankfurt, EU) |
| Integrations | Google Sheets, Drive, Calendar, Gmail, Telegram |
| Interface | Telegram (daily ops) + Claude.ai MCP (CEO dashboard) |

## What It Doesn't Do (Yet)

### Near-Term Possibilities
- **Automated meeting prep** — before each meeting, generate a brief with relevant history, open items, and suggested agenda
- **Morning operational brief** — daily email with what's due, what's overdue, calendar preview
- **Email intelligence** — scan inbox for business-relevant emails, classify and surface key items
- **Proactive alerts** — "Paolo's Lavazza task is 3 days overdue" → nudge via Telegram

### Medium-Term Vision
- **OKR layer** — connect tasks and decisions to quarterly objectives
- **Risk register** — auto-detect and track operational risks from meetings
- **Meeting effectiveness scoring** — are our meetings productive? trending better or worse?
- **Multi-language support** — full Hebrew interface (currently works for search, not full UI)
- **Competitor monitoring** — track mentions of competitors across all sources

### Long-Term
- **Autonomous workflows** — Gianluigi proposes and executes routine operational tasks with minimal oversight
- **Board reporting** — auto-generate investor updates from operational data
- **Team member interfaces** — each team member gets their own view (tasks, deadlines, relevant decisions)

## Key Design Principles

1. **Gianluigi proposes, Eyal approves** — no autonomous actions without CEO sign-off
2. **All team communication goes through Eyal** — no direct nudging of team members
3. **Sensitivity-first** — investor/legal/equity discussions are automatically protected
4. **Source citations** — every piece of information traces back to a specific meeting
5. **Memory is institutional** — when someone asks "what did we decide about X?", the answer is always there

## By the Numbers

| Metric | Value |
|--------|-------|
| MCP Tools | 35 |
| Test Coverage | 1,365+ tests |
| Canonical Projects Tracked | 10 |
| Development Phases | 10 (v0.1 → v1.0) |
| Lines of Code | ~25,000 |
| Development Period | ~4 weeks |

---

*Built by Eyal with Claude Code. Powered by Claude API, Supabase, and Google Cloud.*
