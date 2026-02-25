# Gianluigi

CropSight's AI Operations Assistant — institutional memory, task tracking, and meeting intelligence for the founding team.

## Overview

Gianluigi processes meeting transcripts, extracts decisions and action items, maintains searchable institutional memory, and keeps the CropSight team aligned. Think Jarvis for a founding team.

## Current Phase

**v0.1 — "Gianluigi Can Remember"**

Core capabilities being built:
- Meeting transcript processing (via Tactiq exports)
- Decision, task, and action item extraction
- Semantic search across meeting history
- Task management via Telegram
- Approval workflow (Eyal reviews before distribution)

## Architecture

```
Input → Filter → Process → Approve → Distribute
  │        │         │         │          │
  ├─ Telegram      │         │          ├─ Google Drive
  ├─ Gmail         │         │          ├─ Google Sheets
  ├─ Google Drive  │         │          ├─ Telegram
  │                │         │          └─ Email
  │                │         │
  │         Calendar/Sensitivity    Eyal Review
  │             Filters
  │                │
  │                │
  └────────────────┴── Claude API (Opus 4.6)
                           │
                       Supabase
                    (PostgreSQL + pgvector)
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| LLM | Claude API (Opus 4.6) |
| Database | Supabase (PostgreSQL + pgvector) |
| Interfaces | Telegram Bot, Gmail |
| Storage | Google Drive |
| Task Tracking | Google Sheets |
| Hosting | Google Cloud Run |

## Project Structure

```
gianluigi/
├── config/          # Settings and team configuration
├── core/            # Claude agent, system prompt, tools
├── services/        # External service integrations
├── processors/      # Data processing pipelines
├── guardrails/      # Filtering and safety logic
├── models/          # Pydantic data models
└── scripts/         # Setup and deployment scripts
```

## Setup

1. **Clone and install dependencies:**
   ```bash
   git clone <repo>
   cd gianluigi
   python -m venv venv
   source venv/bin/activate  # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. **Set up Supabase:**
   - Create project in EU region (Frankfurt)
   - Run `scripts/setup_supabase.sql` in SQL Editor

4. **Set up Google APIs:**
   - Follow `scripts/setup_google.md`

5. **Create Telegram bot:**
   - Message @BotFather on Telegram
   - Create bot and get token

6. **Run locally:**
   ```bash
   python main.py
   ```

## Documentation

See `GIANLUIGI_PROJECT_PLAN.md` for complete documentation including:
- Full architecture details (Section 2)
- Data model (Section 3)
- Guardrails and filtering (Sections 6-8)
- Approval workflow (Section 9)
- Tool definitions (Section 10)
- Development sequence (Section 15)

## Development

Build order (from Section 15):
1. Phase A: Foundation (config, Supabase, models)
2. Phase B: Core Processing (embeddings, transcript processor, agent)
3. Phase C: Interfaces (Telegram, Gmail, Drive, Sheets)
4. Phase D: Guardrails & Flows
5. Phase E: Integration & Deploy

## Team

- **Eyal Zror** — CEO, Primary Admin
- **Roye Tadmor** — CTO
- **Paolo Vailetti** — BD
- **Prof. Yoram Weiss** — Senior Advisor

## License

Private — CropSight internal use only.
