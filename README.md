# Gianluigi

CropSight's AI Operations Assistant — an "AI Office Manager" for a 4-person AgTech founding team. Processes meeting transcripts, tracks tasks and decisions with cross-meeting topic threading, maintains institutional memory via hybrid RAG, and serves as the CEO's private operations dashboard via Claude.ai MCP.

## What It Does

- **Meeting intelligence:** Processes transcripts (Tactiq), extracts decisions, tasks, open questions, stakeholders, and follow-ups using Claude Opus
- **Cross-meeting memory:** Topic threading links discussions across meetings, compressed operational snapshots for context continuity
- **Task & decision tracking:** Supabase DB + Google Sheets with canonical project labels, decision rationale/confidence, review triggers
- **CEO dashboard:** 35 MCP tools accessible via Claude.ai — status updates, Gantt analytics, memory search, task management, weekly reviews
- **Team distribution:** Sensitivity-aware email + Telegram distribution with CEO approval gate
- **Operational Gantt:** Bidirectional Sheets integration with proposal-based editing, snapshots, rollback
- **Email intelligence:** Personal Gmail scanning, morning briefs, email classification

## Architecture

```
Tactiq (transcript) --> Google Drive --> Gianluigi (Cloud Run)
                                              |
                        +---------------------+---------------------+
                        |                     |                     |
                   Claude API            Supabase (EU)        Google Workspace
                   (Opus/Sonnet/Haiku)   (PostgreSQL+pgvector) (Sheets/Drive/
                        |                     |                 Calendar/Gmail)
                        |                     |                     |
                   Extraction +          Hybrid RAG +          Task Tracker +
                   Classification        Embeddings            Gantt Chart
                        |                     |                     |
                        +---------------------+---------------------+
                                              |
                              +---------------+---------------+
                              |               |               |
                         Telegram Bot    Claude.ai MCP    Gmail Distribution
                         (daily ops)     (CEO dashboard)  (team summaries)
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Claude API — Opus (extraction), Sonnet (agents), Haiku (classification) |
| Database | Supabase (PostgreSQL + pgvector, EU Frankfurt) |
| Embeddings | OpenAI text-embedding-3-small (1536d) |
| Chat | Telegram Bot (python-telegram-bot) |
| Email | Gmail API |
| Files | Google Drive API |
| Tasks/Gantt | Google Sheets API |
| Calendar | Google Calendar API (read-only) |
| Hosting | Google Cloud Run (europe-west1) |
| Transcription | Tactiq (Chrome extension) |
| CEO Interface | Claude.ai via MCP (SSE transport, FastMCP SDK) |
| Language | Python 3.11+, async |

## MCP Tools (35)

Grouped by category:

| Category | Tools | Purpose |
|----------|-------|---------|
| SYSTEM (6) | get_system_context, get_full_status, get_system_health, get_cost_summary, get_pending_approvals, get_upcoming_meetings | Operational state |
| MEMORY (4) | search_memory, get_meeting_history, get_open_questions, get_stakeholder_info | Search & history |
| TASKS (3) | get_tasks, create_task, update_task | Task management |
| DECISIONS (3) | get_decisions, update_decision, get_decisions_for_review | Decision lifecycle |
| TOPICS (4) | get_topic_thread, list_topic_threads, merge_topic_threads, rename_topic_thread | Cross-meeting threading |
| GANTT (5) | get_gantt_status, get_gantt_horizon, get_gantt_metrics, propose_gantt_update, approve_gantt_proposal | Operational planning |
| REVIEW (3) | get_weekly_summary, start_weekly_review, confirm_weekly_review | Weekly review |
| QUICK (2) | quick_inject, confirm_quick_inject | Quick data injection |
| SESSION (2) | get_last_session_summary, save_session_summary | Session continuity |
| PROJECTS (2) | list_canonical_projects, add_canonical_project | Project label management |

## Key Design Principles

- **Gianluigi proposes, Eyal approves.** No direct team actions without CEO approval.
- **All team interactions go through Eyal.** No direct nudging of team members.
- **Brain is interface-agnostic.** Capabilities are Python functions. Telegram and MCP are interfaces.
- **Sensitivity follows data.** Tags applied at ingestion, affect distribution at every stage.
- **Source citations.** Every extracted item references its source meeting.

## Project Structure

```
config/          # Settings, team config, meeting prep templates, escalation rules
core/            # LLM interface, system prompt, retry logic, cost calculator
guardrails/      # Approval flow, sensitivity classifier, Gantt validation, MCP auth
processors/      # Transcript extraction, cross-reference, topic threading, debrief,
                 # meeting prep, weekly review/digest, Gantt intelligence, decision review
services/        # Google Sheets/Drive/Calendar/Gmail, Telegram bot, MCP server,
                 # Supabase client, Gantt manager, word generator, embeddings
schedulers/      # Transcript watcher, morning brief, weekly digest/review, task archival
scripts/         # Migration SQL, deployment, QA, Sheets rebuild, label backfill
tests/           # 1365+ tests
docs/            # Architecture docs, Claude.ai project prompt, QA review notes
```

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env  # Fill in API keys, tokens, sheet IDs

# Run
python main.py
```

## Deploying

```bash
gcloud run deploy gianluigi \
  --source . \
  --region europe-west1 \
  --memory 1Gi \
  --min-instances 1 \
  --max-instances 1 \
  --cpu 1 \
  --no-cpu-throttling \
  --timeout 3600 \
  --allow-unauthenticated
```

Secrets are set on the Cloud Run service and persist across deploys.

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point — starts all services via asyncio |
| `V1_DESIGN.md` | Comprehensive v1.0 design specification |
| `CLAUDE.md` | Project context for Claude Code sessions |
| `KNOWN_ISSUES.md` | Current bugs and limitations |
| `config/settings.py` | All environment variables and configuration |
| `services/mcp_server.py` | MCP server with 35 tools |
| `docs/claude_project_prompt.md` | Claude.ai project custom instructions |

## CropSight

Israeli AgTech startup. ML-powered crop yield forecasting using satellite imagery, climate data, and agronomic parameters. Pre-revenue, PoC stage.

**Team:** Eyal Zror (CEO), Roye Tadmor (CTO), Paolo Vailetti (BD), Prof. Yoram Weiss (Advisor)
