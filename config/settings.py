"""
Application settings and environment variable configuration.

This module loads all configuration from environment variables and provides
typed access to settings throughout the application.

Uses Pydantic Settings for validation and type coercion.

Usage:
    from config.settings import settings

    api_key = settings.ANTHROPIC_API_KEY
    supabase_url = settings.SUPABASE_URL
"""

import os
from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration class that loads and validates all environment variables.

    All sensitive credentials (API keys, tokens) are loaded from environment
    variables and never hardcoded. Uses Pydantic Settings for validation.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ==========================================================================
    # Claude API
    # ==========================================================================
    ANTHROPIC_API_KEY: str = Field(default="", description="Anthropic API key")
    CLAUDE_MODEL: str = Field(
        default="claude-opus-4-6",
        description="Claude model to use"
    )
    # Tiered model settings — each tier falls back to CLAUDE_MODEL if not set
    CLAUDE_MODEL_EXTRACTION: str = Field(
        default="", description="Model for transcript extraction. Falls back to CLAUDE_MODEL."
    )
    CLAUDE_MODEL_AGENT: str = Field(
        default="", description="Model for agent queries/tool use. Falls back to CLAUDE_MODEL."
    )
    CLAUDE_MODEL_BACKGROUND: str = Field(
        default="", description="Model for meeting prep, edit application. Falls back to CLAUDE_MODEL."
    )
    CLAUDE_MODEL_SIMPLE: str = Field(
        default="", description="Model for doc summaries, edit parsing. Falls back to CLAUDE_MODEL."
    )

    # ==========================================================================
    # Supabase (PostgreSQL + pgvector)
    # ==========================================================================
    SUPABASE_URL: str = Field(default="", description="Supabase project URL")
    SUPABASE_KEY: str = Field(default="", description="Supabase anon/service key")

    # ==========================================================================
    # Google APIs (OAuth credentials)
    # ==========================================================================
    GOOGLE_CLIENT_ID: str = Field(default="", description="Google OAuth client ID")
    GOOGLE_CLIENT_SECRET: str = Field(default="", description="Google OAuth client secret")
    GOOGLE_REFRESH_TOKEN: str = Field(default="", description="Google OAuth refresh token")

    # ==========================================================================
    # Telegram Bot
    # ==========================================================================
    TELEGRAM_BOT_TOKEN: str = Field(default="", description="Telegram bot token from BotFather")
    TELEGRAM_GROUP_CHAT_ID: str = Field(default="", description="CropSight team group chat ID")
    TELEGRAM_EYAL_CHAT_ID: str = Field(default="", description="Eyal's Telegram chat ID for DMs")

    # Team member Telegram IDs for direct messaging
    EYAL_TELEGRAM_ID: int | None = Field(default=None, description="Eyal's Telegram user ID")
    ROYE_TELEGRAM_ID: int | None = Field(default=None, description="Roye's Telegram user ID")
    PAOLO_TELEGRAM_ID: int | None = Field(default=None, description="Paolo's Telegram user ID")
    YORAM_TELEGRAM_ID: int | None = Field(default=None, description="Yoram's Telegram user ID")

    # ==========================================================================
    # Gmail
    # ==========================================================================
    GIANLUIGI_EMAIL: str = Field(
        default="gianluigi.cropsight@gmail.com",
        description="Gianluigi's dedicated Gmail address"
    )

    # ==========================================================================
    # Google Drive Folder IDs
    # ==========================================================================
    CROPSIGHT_OPS_FOLDER_ID: str = Field(default="", description="Root CropSight Ops folder ID")
    RAW_TRANSCRIPTS_FOLDER_ID: str = Field(default="", description="Raw Transcripts folder ID")
    MEETING_SUMMARIES_FOLDER_ID: str = Field(default="", description="Meeting Summaries folder ID")
    MEETING_PREP_FOLDER_ID: str = Field(default="", description="Meeting Prep folder ID")
    WEEKLY_DIGESTS_FOLDER_ID: str = Field(default="", description="Weekly Digests folder ID")
    DOCUMENTS_FOLDER_ID: str = Field(default="", description="Documents folder ID for team uploads")

    # ==========================================================================
    # Google Sheets IDs
    # ==========================================================================
    TASK_TRACKER_SHEET_ID: str = Field(default="", description="Task Tracker Google Sheet ID")
    TASK_TRACKER_TAB_NAME: str = Field(default="Tasks", description="Tab name in the Task Tracker spreadsheet")
    STAKEHOLDER_TRACKER_SHEET_ID: str = Field(default="", description="Stakeholder Tracker Sheet ID")
    STAKEHOLDER_TAB_NAME: str = Field(default="Stakeholder Tracker", description="Tab name in the Stakeholder Tracker spreadsheet")

    # ==========================================================================
    # Embeddings
    # ==========================================================================
    OPENAI_API_KEY: str = Field(default="", description="OpenAI API key (fallback for embeddings)")
    EMBEDDING_API_KEY: str = Field(default="", description="OpenAI API key for embeddings")
    EMBEDDING_MODEL: str = Field(
        default="text-embedding-3-small",
        description="Embedding model name"
    )
    EMBEDDING_DIMENSION: int = Field(
        default=1536,
        description="Embedding vector dimension"
    )

    # ==========================================================================
    # Team Email Configuration
    # ==========================================================================
    EYAL_EMAIL: str = Field(default="", description="Eyal's email address")
    ROYE_EMAIL: str = Field(default="", description="Roye's email address")
    PAOLO_EMAIL: str = Field(default="", description="Paolo's email address")
    YORAM_EMAIL: str = Field(default="", description="Yoram's email address")
    TEAM_ROSTER_DB_ENABLED: bool = Field(
        default=False,
        description=(
            "Load the team roster from the team_members DB table (add people "
            "without a deploy) instead of the hardcoded config/team.py dict. On "
            "ANY error/empty, config/team.py falls back to the hardcoded roster, "
            "so it can never come back empty. Built once at import — a flip takes "
            "effect on the next process restart."
        ),
    )
    TASK_URGENCY_AREA_ENABLED: bool = Field(
        default=False,
        description=(
            "Extraction + manual injection populate tasks.urgency (H/M/L, "
            "time-pressure separate from priority), applying the no-invented-dates "
            "rule (ASAP -> urgency H, deadline null). Off = no urgency in the "
            "extraction prompt (tasks take the column default M). NOTE (2026-06 "
            "realignment): the separate 'area' field this flag used to cover is "
            "gone — tasks.category now carries the Gantt-area taxonomy and is "
            "always extracted/canonicalized, independent of this flag."
        ),
    )
    TASK_SHEET_URGENCY_AREA_ENABLED: bool = Field(
        default=False,
        description=(
            "Add Urgency (col K) to the Tasks sheet, APPENDED after the col-J "
            "UUID identity (never relocated, so reconcile keeps matching). "
            "Off = the A:J 10-column layout. (2026-06 realignment: the Area "
            "column this flag used to add as col L was removed — Category col G "
            "carries the Gantt-area taxonomy. Flag name kept to avoid prod env "
            "churn.)"
        ),
    )

    # ==========================================================================
    # Calendar Configuration
    # ==========================================================================
    CROPSIGHT_CALENDAR_COLOR_ID: str = Field(
        default="",
        description="Google Calendar color ID for CropSight meetings (purple)"
    )
    EYAL_CALENDAR_REFRESH_TOKEN: str = Field(
        default="",
        description="OAuth refresh token for Eyal's Google account (calendar.readonly scope). "
                    "Lets Gianluigi read Eyal's calendar AS Eyal — sees colors, declined status, etc. "
                    "Get via: python scripts/get_calendar_token.py"
    )

    # ==========================================================================
    # Application Settings
    # ==========================================================================
    PORT: int = Field(
        default=8080,
        description="HTTP port for health check server (Cloud Run)"
    )
    DRIVE_POLL_INTERVAL_MINUTES: int = Field(
        default=15,
        description="How often to check for new transcripts (minutes)"
    )
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    ENVIRONMENT: str = Field(default="development", description="Environment name")

    # ==========================================================================
    # Search & RAG Tuning
    # ==========================================================================
    SIMILARITY_THRESHOLD: float = Field(
        default=0.6, description="Default similarity threshold for semantic search"
    )
    SIMILARITY_THRESHOLD_CONTEXTUAL: float = Field(
        default=0.4, description="Lower threshold for contextual/parent chunk retrieval"
    )
    RECENCY_HALFLIFE_DAYS: int = Field(
        default=30, description="Half-life in days for time-weighted RAG recency boost"
    )
    CHUNK_SIZE: int = Field(default=1000, description="Embedding chunk size in characters")
    CHUNK_OVERLAP: int = Field(default=200, description="Embedding chunk overlap in characters")

    # ==========================================================================
    # Alert Thresholds
    # ==========================================================================
    ALERT_OVERDUE_CLUSTER_MIN: int = Field(
        default=3, description="Minimum overdue tasks per assignee to trigger alert"
    )
    ALERT_STALE_COMMITMENT_DAYS: int = Field(
        default=14, description="Days before an open commitment is considered stale"
    )
    ALERT_RECURRING_DISCUSSION_MEETINGS: int = Field(
        default=3, description="Entity in N+ meetings triggers recurring discussion alert"
    )
    ALERT_QUESTION_PILEUP_MIN: int = Field(
        default=5, description="Minimum open questions to trigger pileup alert"
    )
    ALERT_LOOKBACK_DAYS: int = Field(
        default=90, description="Only alert on items created within this many days"
    )
    ALERT_ENTITY_LOOKBACK_DAYS: int = Field(
        default=180, description="Only count entity mentions within this many days"
    )

    # ==========================================================================
    # Conversation Memory
    # ==========================================================================
    CONVERSATION_MAX_MESSAGES: int = Field(
        default=10, description="Max messages per chat in conversation memory"
    )
    CONVERSATION_TTL_MINUTES: int = Field(
        default=30, description="Minutes before conversation history expires"
    )

    # ==========================================================================
    # Scheduler Intervals (seconds)
    # ==========================================================================
    EMAIL_CHECK_INTERVAL: int = Field(
        default=7200, description="Email watcher check interval (seconds) — 2 hours"
    )
    TRANSCRIPT_WATCHER_ENABLED: bool = Field(
        default=False, description="Enable transcript watcher (disabled during dev to save Opus costs)"
    )
    TRANSCRIPT_POLL_INTERVAL: int = Field(
        default=900, description="Transcript watcher poll interval (seconds) — 15 minutes"
    )
    DOCUMENT_POLL_INTERVAL: int = Field(
        default=7200, description="Document watcher poll interval (seconds) — 2 hours"
    )
    MEETING_PREP_CHECK_INTERVAL: int = Field(
        default=14400, description="Meeting prep scheduler check interval (seconds)"
    )
    MEETING_PREP_HOURS_BEFORE: int = Field(
        default=24, description="Hours before meeting to generate prep document"
    )
    MEETING_PREP_OUTLINE_LEAD_HOURS: int = Field(
        default=24, description="Hours before meeting to send outline proposal"
    )
    MEETING_PREP_GENERATION_LEAD_HOURS: int = Field(
        default=12, description="Hours before meeting to generate full prep doc"
    )
    MEETING_PREP_REMINDER_HOURS: str = Field(
        default="4,8,12", description="Comma-separated hours for prep outline reminders"
    )
    MEETING_PREP_EMERGENCY_HOURS: int = Field(
        default=6, description="Hours threshold for emergency prep mode"
    )
    MEETING_PREP_SKIP_HOURS: int = Field(
        default=2, description="Hours threshold below which prep is skipped"
    )
    MEETING_PREP_FOCUS_TIMEOUT_MINUTES: int = Field(
        default=30, description="Minutes before stale focus_active flags are cleared"
    )
    WEEKLY_DIGEST_CHECK_INTERVAL: int = Field(
        default=3600, description="Weekly digest scheduler check interval (seconds)"
    )
    TASK_REMINDER_CHECK_INTERVAL: int = Field(
        default=28800, description="Task reminder scheduler check interval (seconds)"
    )
    ALERT_CHECK_INTERVAL: int = Field(
        default=43200, description="Alert scheduler check interval (seconds)"
    )
    ORPHAN_CLEANUP_INTERVAL: int = Field(
        default=86400, description="Orphan cleanup scheduler interval (seconds)"
    )

    # ==========================================================================
    # v1.0 — Debrief
    # ==========================================================================
    DEBRIEF_TTL_MINUTES: int = Field(
        default=60, description="Debrief session auto-expires after this many minutes"
    )
    DEBRIEF_EVENING_PROMPT_HOUR: int = Field(
        default=18, description="IST hour for scheduled evening debrief prompt (future)"
    )
    DEBRIEF_EVENING_PROMPT_ENABLED: bool = Field(
        default=True, description="Enable scheduled evening debrief prompt"
    )
    DEBRIEF_MAX_ITEMS: int = Field(
        default=30, description="Safety cap: max items per debrief session"
    )
    DEBRIEF_OPUS_THRESHOLD: int = Field(
        default=5, description="Use Opus validation only when items exceed this count"
    )

    # ==========================================================================
    # v1.0 — Gantt Integration
    # ==========================================================================
    GANTT_SHEET_ID: str = Field(default="", description="Gantt Google Sheet ID")
    GANTT_BACKUP_FOLDER_ID: str = Field(default="", description="Gantt backup Drive folder ID")
    GANTT_MAX_CELLS_PER_PROPOSAL: int = Field(
        default=20, description="Safety limit: max cell changes per Gantt proposal batch"
    )
    GANTT_LOG_TAB: str = Field(default="Log", description="Gantt sheet Log tab name")
    GANTT_CONFIG_TAB: str = Field(default="Config", description="Gantt sheet Config tab name")
    GANTT_MEETING_CADENCE_TAB: str = Field(
        default="Meeting Cadence", description="Gantt sheet Meeting Cadence tab name"
    )
    GANTT_MAIN_TAB: str = Field(default="2026-2027", description="Gantt main year-sheet tab name")
    GANTT_HEADER_ROWS: int = Field(default=5, description="Number of header rows in Gantt sheet")

    # ==========================================================================
    # v1.0 — Email Intelligence
    # ==========================================================================
    EYAL_PERSONAL_EMAIL: str = Field(default="", description="Eyal's personal Gmail for daily scan")
    PERSONAL_CONTACTS_BLOCKLIST: str = Field(default="", description="Comma-separated blocklist for personal email scan")
    EYAL_GMAIL_REFRESH_TOKEN: str = Field(default="", description="OAuth refresh token for Eyal's personal Gmail")
    EMAIL_DAILY_SCAN_HOUR: int = Field(default=7, description="IST hour for morning email scan")
    EMAIL_DAILY_SCAN_ENABLED: bool = Field(default=True, description="Enable daily scan of Eyal's personal Gmail")
    EMAIL_MAX_SCAN_RESULTS: int = Field(default=50, description="Max emails per daily scan")
    EMAIL_ATTACHMENTS_FOLDER_ID: str = Field(
        default="", description="Google Drive folder ID for persisting email attachments (Phase 13 B3)"
    )

    # Phase 13 B1: Dropbox sync
    DROPBOX_APP_KEY: str = Field(default="", description="Dropbox app key for OAuth")
    DROPBOX_REFRESH_TOKEN: str = Field(default="", description="Dropbox OAuth refresh token")
    DROPBOX_SYNC_FOLDER: str = Field(default="", description="Dropbox folder path to sync (e.g., /CropSight BD)")
    DROPBOX_MIRROR_DRIVE_FOLDER_ID: str = Field(default="", description="Drive folder ID for Dropbox mirror")
    DROPBOX_SYNC_ENABLED: bool = Field(default=False, description="Enable Dropbox → Drive sync scheduler")
    DROPBOX_SYNC_INTERVAL: int = Field(default=7200, description="Dropbox sync poll interval (seconds) — 2 hours")
    MORNING_BRIEF_ENABLED: bool = Field(default=True, description="Enable morning brief (daily consolidated touchpoint)")
    MORNING_BRIEF_HOUR: int = Field(default=7, description="IST hour for morning brief")
    THREAD_TRACKING_EXPIRY_DAYS: int = Field(default=30, description="Tracked email threads expire after N days")

    # ==========================================================================
    # v1.0 — MCP Server
    # ==========================================================================
    MCP_AUTH_TOKEN: str = Field(default="", description="Auth token for MCP server")
    MCP_PORT: int = Field(default=8080, description="MCP server port (shared with health server)")
    MCP_RATE_LIMIT_PER_HOUR: int = Field(
        default=100, description="Max MCP tool calls per hour per token"
    )

    # ==========================================================================
    # v1.0 — Weekly Review
    # ==========================================================================
    WEEKLY_REVIEW_CALENDAR_TITLE: str = Field(
        default="CropSight: Weekly Review with Gianluigi",
        description="Calendar event title for weekly review sessions"
    )
    WEEKLY_REVIEW_PREP_HOURS: int = Field(
        default=3, description="Hours before weekly review to compile data"
    )
    WEEKLY_REVIEW_NOTIFY_MINUTES: int = Field(
        default=30, description="Minutes before weekly review to send notification"
    )
    WEEKLY_REVIEW_MAX_CORRECTIONS: int = Field(
        default=10, description="Safety cap: max corrections per weekly review session"
    )
    WEEKLY_REVIEW_SESSION_EXPIRY_HOURS: int = Field(
        default=48, description="Weekly review session expires after this many hours"
    )
    WEEKLY_REVIEW_DAY: int = Field(
        default=4, description="Day of week for weekly review fallback prompt (0=Mon, 4=Fri)"
    )
    WEEKLY_REVIEW_ENABLED: bool = Field(
        default=False, description="Enable weekly review scheduler (safe rollout)"
    )
    WEEKLY_REVIEW_SCHEDULER_INTERVAL: int = Field(
        default=900, description="Weekly review scheduler check interval (seconds)"
    )

    # ==========================================================================
    # v1.0 — Reports
    # ==========================================================================
    REPORTS_BASE_URL: str = Field(default="", description="Base URL for HTML reports on Cloud Run")
    REPORTS_SECRET_TOKEN: str = Field(default="", description="Secret token for report access")

    # ==========================================================================
    # v1.0 — Drive Folders
    # ==========================================================================
    WEEKLY_REPORTS_FOLDER_ID: str = Field(default="", description="Weekly Reports Drive folder ID")
    GANTT_SLIDES_FOLDER_ID: str = Field(default="", description="Gantt Slides Drive folder ID")

    # ==========================================================================
    # Approval Mode (v0.2) + Reminders (post-Phase 4)
    # ==========================================================================
    APPROVAL_MODE: str = Field(
        default="manual",
        description="Approval mode: 'manual' (default) or 'auto_review' for timed auto-publish"
    )
    AUTO_REVIEW_WINDOW_MINUTES: int = Field(
        default=60,
        description="Minutes to wait before auto-publishing in auto_review mode"
    )
    APPROVAL_REMINDER_HOURS: str = Field(
        default="2,6",
        description="Comma-separated hours after submission to send reminder DMs"
    )
    APPROVAL_REMINDER_ENABLED: bool = Field(
        default=True,
        description="Enable gentle Telegram reminders for unreviewed approvals"
    )

    # ==========================================================================
    # Weekly Digest Scheduling (post-Phase 4)
    # ==========================================================================
    WEEKLY_DIGEST_DAY: int = Field(
        default=4,
        description="Day of week for digest (0=Mon, 4=Fri, 6=Sun)"
    )
    WEEKLY_DIGEST_HOUR: int = Field(
        default=14,
        description="Hour to start digest window"
    )
    WEEKLY_DIGEST_WINDOW_HOURS: int = Field(
        default=2,
        description="Hours the digest window stays open"
    )
    MORNING_BRIEF_SKIP_DAYS: str = Field(
        default="Saturday",
        description="Comma-separated day names to skip morning brief (e.g. Saturday)"
    )

    # ==========================================================================
    # RAG Source Weights (post-Phase 4)
    # ==========================================================================
    RAG_WEIGHT_DEBRIEF: float = Field(default=1.5, description="RAG weight for debrief content")
    RAG_WEIGHT_DECISION: float = Field(default=1.3, description="RAG weight for decisions")
    RAG_WEIGHT_EMAIL: float = Field(default=1.0, description="RAG weight for email content")
    RAG_WEIGHT_MEETING: float = Field(default=1.0, description="RAG weight for meeting transcripts")
    RAG_WEIGHT_DOCUMENT: float = Field(default=0.9, description="RAG weight for documents")
    RAG_WEIGHT_GANTT: float = Field(default=0.7, description="RAG weight for Gantt changes")

    # ==========================================================================
    # Health Monitoring (post-Phase 4)
    # ==========================================================================
    DAILY_HEALTH_REPORT_ENABLED: bool = Field(
        default=True,
        description="Enable daily health summary after morning brief"
    )
    DAILY_COST_ALERT_THRESHOLD: float = Field(
        default=5.0,
        description="USD daily cost threshold for warning alert"
    )
    TASK_ARCHIVAL_ENABLED: bool = Field(
        default=False,
        description="Enable daily archival of completed tasks to Sheets Archive tab"
    )
    TASK_ARCHIVAL_DAYS: int = Field(
        default=30,
        description="Archive completed tasks older than this many days"
    )

    # Phase 12 A2: Continuity-aware extraction
    CONTINUITY_AUTO_APPLY_ENABLED: bool = Field(
        default=False,
        description="Auto-apply high-confidence task matches from continuity extraction (requires A3 gate)"
    )
    INTERPERSONAL_SIGNAL_DETECTION: bool = Field(
        default=False,
        description="Enable interpersonal signal detection in extraction (CEO tier). Default OFF — enable when ready to test."
    )

    FOLLOW_UP_SENSITIVITY_ENABLED: bool = Field(
        default=False,
        description=(
            "Carry the meeting tier onto follow_up_meetings rows (audit P1-01/P1-05). "
            "Default OFF — requires the follow_up_meetings.sensitivity column "
            "(scripts/migrate_followup_sensitivity_p1_05.sql) applied first; flip ON "
            "after the migration so inserts/propagation don't hit a missing column."
        ),
    )

    # ==========================================================================
    # Intelligence Signal
    # ==========================================================================
    INTELLIGENCE_SIGNAL_ENABLED: bool = Field(
        default=False, description="Enable weekly intelligence signal scheduler"
    )
    INTELLIGENCE_SIGNAL_DAY: int = Field(
        default=3, description="Day of week for intelligence signal (0=Mon, 3=Thu)"
    )
    INTELLIGENCE_SIGNAL_HOUR: int = Field(
        default=18, description="IST hour for intelligence signal generation"
    )
    INTELLIGENCE_SIGNAL_RECIPIENTS: str = Field(
        default="",
        description="Comma-separated email recipients for signal distribution (empty = Eyal only)"
    )
    INTELLIGENCE_SIGNAL_AUTO_DISTRIBUTE: bool = Field(
        default=False,
        description="Skip approval gate and auto-distribute (keep False until quality proven)"
    )
    INTELLIGENCE_SIGNAL_VIDEO_ENABLED: bool = Field(
        default=False, description="Enable video generation (requires ffmpeg + Pillow + ElevenLabs)"
    )
    INTELLIGENCE_SIGNAL_SAFE_DISTRIBUTE: bool = Field(
        default=False,
        description=(
            "Restart-safe distribution. When True, approval marks the signal "
            "'approved_finalizing' and a reconstructable background worker does a "
            "bounded Drive-readiness poll + an at-most-once (double-send-guarded) send "
            "— replacing the in-process 30-min asyncio.sleep that a Cloud Run cycle "
            "silently loses. When False, the legacy synchronous distribute path runs "
            "unchanged. Does NOT move video generation (see POST_APPROVAL_VIDEO)."
        ),
    )
    INTELLIGENCE_SIGNAL_FOLDER_ID: str = Field(
        default="", description="Google Drive folder ID for Intelligence Signal outputs"
    )
    PERPLEXITY_API_KEY: str = Field(
        default="", description="Perplexity API key for intelligence research"
    )
    PERPLEXITY_MODEL: str = Field(
        default="sonar-pro", description="Perplexity model for research queries"
    )
    ELEVENLABS_API_KEY: str = Field(
        default="", description="ElevenLabs API key for voice narration"
    )
    ELEVENLABS_VOICE_ID: str = Field(
        default="EXAVITQu4vr4xnSDxMaL",
        description="ElevenLabs voice ID for signal narration (default: Sarah)"
    )

    # ==========================================================================
    # Comms / Voice (beat #1) — orchestration spine + voice intake. Default OFF.
    # See V2.5_STRATEGY.md §6 (human-assistant comms layer).
    # ==========================================================================
    ORCHESTRATION_SPINE_ENABLED: bool = Field(
        default=False,
        description="Route INBOUND Telegram messages through the orchestration spine (services/orchestrator). The outbound facade is a verbatim pass-through regardless of this flag; this gates only the inbound rerouting so it can be toggled/rolled back independently of voice intake. Default off."
    )
    VOICE_INTAKE_ENABLED: bool = Field(
        default=False,
        description="Enable Telegram voice-note intake: STT via ElevenLabs Scribe -> the existing quick-injection confirm flow. Un-gates ELEVENLABS_API_KEY for STT independently of the video flag (see elevenlabs_client.stt_available). Default off."
    )
    VOICE_OUT_ENABLED: bool = Field(
        default=False,
        description="Enable voice-OUT: a 'Listen' button on Gianluigi's substantive replies to Eyal; tapping it TTSs the message (ElevenLabs) and sends an inline audio player. Un-gates the key for TTS independently of the video flag (see elevenlabs_client.tts_available). Default off."
    )
    ELEVENLABS_VOICE_ID_GIANLUIGI: str = Field(
        default="RbNTU8eTHcsao6T0f1ve",
        description="Gianluigi's speaking voice for voice-OUT (Stephen - Well-spoken, British; chosen via the beat-#4 audition, handles EN + HE on eleven_v3). Separate from ELEVENLABS_VOICE_ID (video narration) so the speaking voice can diverge."
    )

    # ==========================================================================
    # Knowledge Foundation (v2.5) — all default OFF; shadow-run by default.
    # See V2.5_STRATEGY.md and the Phase 1 plan.
    # ==========================================================================
    KNOWLEDGE_SHADOW_MODE: bool = Field(
        default=False,
        description="Run knowledge read-back in SHADOW: extract twice (baseline+augmented), LOG the diff to audit_log, ship the baseline. Doubles extraction cost while on — turn True to start the shadow window (ideally after synthesis), False to pause. Never alters shipped output."
    )
    KNOWLEDGE_READBACK_ENABLED: bool = Field(
        default=False,
        description="Inject retrieved topic/area briefs + RAG into the extraction prompt. Flip True only after >=10 clean shadow meetings."
    )
    EXTRACTION_MUZZLE_REMOVED: bool = Field(
        default=False,
        description="Remove the 'aim for 3-7 action items' consolidation cap from extraction. Separate from read-back (different blast radius)."
    )
    KNOWLEDGE_NIGHTLY_ENABLED: bool = Field(
        default=False, description="Enable nightly knowledge-consolidation scheduler"
    )
    KNOWLEDGE_NIGHTLY_HOUR: int = Field(
        default=3, description="IST hour for nightly knowledge consolidation"
    )
    KNOWLEDGE_WEEKLY_ENABLED: bool = Field(
        default=False, description="Enable weekly knowledge-synthesis + reflection scheduler"
    )
    KNOWLEDGE_WEEKLY_DAY: int = Field(
        default=6, description="Day of week for weekly synthesis (0=Mon, 6=Sun)"
    )
    KNOWLEDGE_WEEKLY_HOUR: int = Field(
        default=4, description="IST hour for weekly knowledge synthesis"
    )
    KNOWLEDGE_CLUSTER_ENABLED: bool = Field(
        default=False, description="Enable semantic topic/area clustering -> proposals (Eyal approves)"
    )
    KNOWLEDGE_READBACK_COST_CEILING_USD: float = Field(
        default=0.05, description="Per-meeting added-cost budget for read-back; warn at 1x, trip per-meeting fallback at 2x"
    )
    KNOWLEDGE_READBACK_LATENCY_BUDGET_S: float = Field(
        default=15.0, description="Per-meeting added-latency budget (seconds) for read-back; monitored, not blocking"
    )

    # ==========================================================================
    # Outputs reconcile (v3) — DB-truth + Sheet-editable via column-ownership sync.
    # ==========================================================================
    RECONCILE_ENABLED: bool = Field(
        default=False, description="Enable the Tasks reconcile scheduler (midday + pre-nightly)"
    )
    RECONCILE_SHADOW_MODE: bool = Field(
        default=True,
        description="Reconcile computes + logs but does NOT write Sheet/DB/snapshot. Keep True until cutover (test on a duplicated sheet first)."
    )
    RECONCILE_MIDDAY_HOUR: int = Field(default=13, description="IST hour for the midday reconcile")
    RECONCILE_PRENIGHTLY_HOUR: int = Field(
        default=2, description="IST hour for the pre-nightly reconcile (must be < KNOWLEDGE_NIGHTLY_HOUR so the DB is correct before nightly reads tasks)"
    )

    # ==========================================================================
    # Gantt redesign (v3 chunk 2) — curated knowledge-view of the Gantt.
    # ==========================================================================
    GANTT_RECONCILE_ENABLED: bool = Field(
        default=False, description="Enable the pre-weekly-digest Gantt status rollup + timeframe reconcile"
    )
    GANTT_SHADOW_MODE: bool = Field(
        default=True, description="Gantt rollup/reconcile computes + logs but does NOT write the sheet/snapshot. Keep True until cutover (duplicated sheet first)."
    )
    GANTT_PREDIGEST_HOUR: int = Field(
        default=13, description="IST hour to refresh the Gantt (must be < WEEKLY_DIGEST_HOUR so the digest reads a fresh Gantt)"
    )
    GANTT_TAG_COLUMN: str = Field(
        default="DZ", description="Hidden column holding each Gantt row's topic UUID (must sit past the last week column on every sheet)"
    )
    GANTT_CUTOVER_PREVIEW: bool = Field(
        default=True, description="During cutover, DM Eyal a preview of the pre-digest Gantt write (reply STOP to cancel); drop after 3 clean cycles"
    )
    # v3 revised (improve EXISTING Gantt): restructure (add rows), linkage, nudges, high-bar pops
    GANTT_RESTRUCTURE_ENABLED: bool = Field(
        default=False, description="Enable the copy+add-rows engine (+1 Planning/+2 Execution per area). Cutover to live also requires confirm=True. Copy-first, never auto."
    )
    GANTT_LINKAGE_ENABLED: bool = Field(
        default=False, description="Enable per-lane->topics linkage proposals (knowledge_links 'gantt_covers'); proposal-only, DB-only"
    )
    GANTT_NUDGE_ENABLED: bool = Field(
        default=False, description="Surface the weekly 'Gantt updates' nudges (brief<->board divergence) in the weekly review"
    )
    GANTT_ALERT_ENABLED: bool = Field(
        default=False, description="Enable the rare high-bar Telegram pop for critical+blocked+board-active Gantt divergences (cooldown 1/topic/week)"
    )

    # ==========================================================================
    # Outputs re-architecture (v2.5 Phase 3) — PR1 input hygiene + PR2 morning
    # brief + PR3 engagement. All default OFF/shadow-safe. See plan
    # composed-brewing-giraffe.md.
    # ==========================================================================
    # PR1 — input hygiene. Three independent capabilities + one shared shadow,
    # so a regression isolates to the exact change that caused it.
    STRICT_CALENDAR_FILTER: bool = Field(
        default=False,
        description="Use the strict is_cropsight_meeting() chain (purple OR business-domain OR known-stakeholder-domain OR title-prefix; drops the personal-gmail '2+ team members' branch). OFF = legacy OR-chain."
    )
    STRICT_UNCERTAIN_EXCLUSION: bool = Field(
        default=False,
        description="Calendar consumers treat uncertain (None) meetings as EXCLUDE (morning brief/debrief currently INCLUDE them). Independent of the filter rewrite so the two can be isolated."
    )
    EMAIL_BUSINESS_GATE: bool = Field(
        default=False,
        description="Use the reordered soft email gate + sharpened classifier (excludes known-personal senders; cold business inbound still reaches Haiku). OFF = legacy whitelist chain."
    )
    INPUT_HYGIENE_SHADOW_MODE: bool = Field(
        default=True,
        description="Applies to all three PR1 capabilities: compute the NEW decision, RETURN the OLD, and log the human-scannable delta to audit_log (action='input_hygiene_shadow'). The 'what it would exclude' log. Flip False to enforce."
    )

    # PR2 — morning brief rework.
    MORNING_BRIEF_V2_ENABLED: bool = Field(
        default=False,
        description="Use the v2 decision-first, knowledge-aware morning brief formatter (incl. the thin Haiku headline behind its own try/except). OFF ships today's brief."
    )
    MORNING_BRIEF_V2_SHADOW: bool = Field(
        default=True,
        description="During rollout: compute v2, log it, and send a tagged '[v2 preview]' second message (button-less) for 2-3 days; v1 stays the authoritative 07:00 send until this is flipped False."
    )
    # Foresight flags read the knowledge layer's authoritative current_status
    # (blocked/stale), so no day-threshold knobs are needed for them. Watcher
    # staleness reuses the existing 24h heartbeat window.
    BRIEF_ERROR_THRESHOLD: int = Field(
        default=3, description="audit_log errors in 24h before the morning brief surfaces the System section (>=3 so a single transient blip stays silent)"
    )

    # PR3 — engagement instrumentation.
    BRIEF_FEEDBACK_ENABLED: bool = Field(
        default=False,
        description="Attach whole-brief 👍/👎 + 'what felt like noise?' feedback buttons and the brief_more overflow buttons to the morning brief. Requires the morning_brief_feedback table."
    )

    # ==========================================================================
    # Outputs re-architecture (v2.5 Phase 3) — chunk 2: meeting-summary context.
    # ==========================================================================
    SUMMARY_CONTEXT_ENABLED: bool = Field(
        default=False,
        description="Append exception-based executive-context clauses to meeting summaries: decision supersession ('(reverses the <date> decision: ...)') + a one-line topic 'Where this fits'. Tier-safe (a clause is omitted if the referenced prior item/topic is above the meeting's distribution tier). Off = summaries render exactly as before."
    )

    # ==========================================================================
    # Meeting-summaries operational upgrade (2026-06-10) — PR6: flip the
    # push outputs to read the priority×urgency×area floor (PR1/PR3/PR4 populate
    # it). Brief/reminders/digest only — the rich summary is its own flag.
    # OFF = today's deadline-and-priority-only outputs, byte-for-byte.
    # ==========================================================================
    OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED: bool = Field(
        default=False,
        description="Rank the morning-brief task line by urgency-then-priority (surfacing urgency=H ASAP tasks that have no deadline, which today's overdue filter drops) + annotate by category; add an urgency/category tag to task reminders (the EXPLICIT-deadline gate is unchanged — ASAP never fires a false deadline reminder); add per-category + per-urgency rollups to the weekly digest. OFF = today's outputs unchanged. Reads tasks.urgency/category (category = Gantt-area taxonomy since the 2026-06 realignment)."
    )

    # ==========================================================================
    # Meeting-summaries operational upgrade (2026-06-10) — PR7: the forward-
    # facing, richer meeting summary (executive TL;DR, urgency/area action items,
    # decision intelligence, risks/blockers, per-area focus, what-changed-since).
    # Renders via a SEPARATE summary_template_rich registry key so the legacy
    # template is never touched. OFF = today's summary, byte-for-byte. Supersedes
    # SUMMARY_CONTEXT_ENABLED when both are on (the rich render folds in the same
    # supersession + topic clauses). Every section is independently guarded — a
    # gather failure degrades to the baseline summary, never crashes the flow.
    # ==========================================================================
    SUMMARY_RICH_ENABLED: bool = Field(
        default=False,
        description="Render the forward-facing rich meeting summary: an executive TL;DR (LLM headline with deterministic fallback, never invents facts), Urgency+Category columns on Action Items, a Decision Intelligence block (rationale/options/confidence/supersession), Risks & Blockers + per-category focus from the topic/area briefs, and a 'What changed since last time' cross-meeting delta. Tier-safe (every block filtered to the meeting's distribution tier). OFF = today's summary unchanged. Reads tasks.urgency/category (category = Gantt-area taxonomy since the 2026-06 realignment)."
    )

    # ==========================================================================
    # Meeting-summaries operational upgrade (2026-06-10) — PR8: branded summary
    # ARTIFACTS (the distributed .docx + the email HTML). CropSight palette
    # (green #1A7A4C / gold #C9A227 / navy #0A1628) PLUS the Area + Urgency
    # columns the structured .docx/email table drops today (the audit gap).
    # OFF = today's plain python-docx / 4-column email, byte-for-byte.
    # ==========================================================================
    SUMMARY_BRANDED_ENABLED: bool = Field(
        default=False,
        description="Brand the distributed summary .docx + email with the CropSight palette (green #1A7A4C / gold #C9A227 / navy text) and add Category + Urgency columns to the Action Items table (closes the gap where the .docx/email dropped the new fields). OFF = today's plain document + 4-column email table, unchanged. Reads tasks.urgency/category (category = Gantt-area taxonomy since the 2026-06 realignment)."
    )

    # ==========================================================================
    # Outputs re-architecture (v2.5 Phase 3) — chunk 3: meeting-prep "Prep Ping".
    # Push-first ping + on-demand brief, replacing the old outline/Drive-doc prep.
    # When ON, main.py starts prep_ping_scheduler INSTEAD of the old one.
    # ==========================================================================
    PREP_PING_ENABLED: bool = Field(
        default=False,
        description="Use the new push-first meeting-prep: a deterministic ping ~LEAD min before each meeting (participant-anchored, topic-enriched) + an on-demand 'Prepare me' brief. OFF = the old outline/Drive-doc prep scheduler."
    )
    PREP_PING_LEAD_MINUTES: int = Field(
        default=90, description="Send the prep ping when a meeting is within this many minutes of starting."
    )
    PREP_PING_MIN_LEAD_MINUTES: int = Field(
        default=15, description="Too-late floor: don't ping if the meeting starts in fewer than this many minutes (cold-start / sub-lead safety)."
    )
    PREP_PING_CHECK_INTERVAL: int = Field(
        default=600, description="Seconds between prep-ping calendar checks (10 min; no LLM per poll)."
    )

    # ==========================================================================
    # Outputs re-architecture (v2.5 Phase 3) — chunk 4: weekly "Pulse" report.
    # One deterministic Friday push (a view over the knowledge layer, no LLM) +
    # an on-demand tier-filtered team-email package. When ON, the old weekly
    # digest auto-push and the heavy weekly-review Telegram session self-suppress
    # (their generator + the MCP review path stay alive). OFF = today unchanged.
    # ==========================================================================
    WEEKLY_PULSE_ENABLED: bool = Field(
        default=False,
        description="Push the deterministic weekly Pulse report (where-we-stand across all areas + needs-your-call + moved-this-week) Friday afternoon, with an on-demand [Send to team] package. OFF = the old weekly digest auto-push + heavy review session run as before."
    )
    WEEKLY_PULSE_HOUR: int = Field(
        default=15, description="IST hour to push the weekly Pulse (default 15:00 — just after the weekly-digest 14:00 slot)."
    )
    WEEKLY_PULSE_WINDOW_HOURS: int = Field(
        default=2, description="Hour window after WEEKLY_PULSE_HOUR within which the Pulse may fire (catches it on a CHECK_INTERVAL cadence)."
    )
    WEEKLY_PULSE_CHECK_INTERVAL: int = Field(
        default=3600, description="Seconds between weekly-Pulse day/hour-window checks (hourly; reuses WEEKLY_DIGEST_DAY for the day)."
    )

    # ==========================================================================
    # Rollout orchestrator (v2.5 Phase 3, chunk 5) — staged env-flag rollouts.
    # Daily reminder + tap-to-apply via Cloud Run admin API. Restart-safe via
    # audit_log. Plan = processors/rollout_plan.py (hardcoded Python list).
    # Eyal-gated; default OFF → no behavior change.
    # ==========================================================================
    ROLLOUT_SCHEDULER_ENABLED: bool = Field(
        default=False,
        description="Enable the rollout orchestrator: daily 09:00 IST reminder for the next due staged rollout + [Apply] button → Cloud Run admin API updates env vars. Eyal-gated; persistent reminders until applied."
    )
    ROLLOUT_CHECK_HOUR: int = Field(
        default=9, description="IST hour at which the rollout orchestrator fires its daily reminder."
    )
    ROLLOUT_CHECK_INTERVAL: int = Field(
        default=3600, description="Seconds between rollout-orchestrator ticks (hourly — fires only inside the configured hour, once per day)."
    )
    GCP_PROJECT_ID: str = Field(
        default="gianluigi-488420", description="GCP project for Cloud Run admin API calls."
    )
    GCP_REGION: str = Field(
        default="europe-west1", description="Cloud Run region for admin API calls."
    )
    CLOUD_RUN_SERVICE_NAME: str = Field(
        default="gianluigi", description="Cloud Run service name targeted by rollout env-var updates."
    )

    @property
    def model_extraction(self) -> str:
        """Model for transcript extraction (accuracy-critical, rare)."""
        return self.CLAUDE_MODEL_EXTRACTION or self.CLAUDE_MODEL

    @property
    def model_agent(self) -> str:
        """Model for agent queries and tool use (frequent, real-time)."""
        return self.CLAUDE_MODEL_AGENT or self.CLAUDE_MODEL

    @property
    def model_background(self) -> str:
        """Model for meeting prep, edit application (background, rare)."""
        return self.CLAUDE_MODEL_BACKGROUND or self.CLAUDE_MODEL

    @property
    def model_simple(self) -> str:
        """Model for doc summaries, edit parsing (simple tasks, rare)."""
        return self.CLAUDE_MODEL_SIMPLE or self.CLAUDE_MODEL

    def validate_required(self) -> list[str]:
        """
        Validate that all required environment variables are set.

        Returns:
            List of missing or invalid configuration keys.
        """
        errors = []

        required_vars = [
            ("ANTHROPIC_API_KEY", self.ANTHROPIC_API_KEY),
            ("SUPABASE_URL", self.SUPABASE_URL),
            ("SUPABASE_KEY", self.SUPABASE_KEY),
            ("TELEGRAM_BOT_TOKEN", self.TELEGRAM_BOT_TOKEN),
        ]

        for name, value in required_vars:
            if not value:
                errors.append(f"Missing required environment variable: {name}")

        # Validate URLs
        if self.SUPABASE_URL and not self.SUPABASE_URL.startswith("https://"):
            errors.append("SUPABASE_URL must start with https://")

        return errors

    def validate_optional(self) -> list[str]:
        """
        Check for optional but recommended configuration.

        Returns:
            List of warnings for missing optional configuration.
        """
        warnings = []

        optional_vars = [
            ("GOOGLE_CLIENT_ID", self.GOOGLE_CLIENT_ID, "Google API integration"),
            ("EMBEDDING_API_KEY", self.EMBEDDING_API_KEY or self.OPENAI_API_KEY, "Semantic search"),
            ("EYAL_EMAIL", self.EYAL_EMAIL, "Team email notifications"),
        ]

        for name, value, feature in optional_vars:
            if not value:
                warnings.append(f"Missing {name} - {feature} will not work")

        return warnings

    @property
    def team_emails(self) -> list[str]:
        """Get list of all team member emails (non-empty only)."""
        emails = [self.EYAL_EMAIL, self.ROYE_EMAIL, self.PAOLO_EMAIL, self.YORAM_EMAIL]
        return [e for e in emails if e]

    @property
    def personal_contacts_blocklist_list(self) -> list[str]:
        """Parse comma-separated blocklist into a list."""
        if not self.PERSONAL_CONTACTS_BLOCKLIST:
            return []
        return [e.strip() for e in self.PERSONAL_CONTACTS_BLOCKLIST.split(",") if e.strip()]

    @property
    def approval_reminder_hours_list(self) -> list[int]:
        """Parse comma-separated reminder hours into a list of ints."""
        if not self.APPROVAL_REMINDER_HOURS:
            return []
        return [int(h.strip()) for h in self.APPROVAL_REMINDER_HOURS.split(",") if h.strip()]

    @property
    def meeting_prep_reminder_hours_list(self) -> list[int]:
        """Parse comma-separated prep reminder hours into a list of ints."""
        if not self.MEETING_PREP_REMINDER_HOURS:
            return []
        return [int(h.strip()) for h in self.MEETING_PREP_REMINDER_HOURS.split(",") if h.strip()]

    @property
    def morning_brief_skip_days_list(self) -> list[str]:
        """Parse comma-separated skip day names into a list."""
        if not self.MORNING_BRIEF_SKIP_DAYS:
            return []
        return [d.strip() for d in self.MORNING_BRIEF_SKIP_DAYS.split(",") if d.strip()]

    @property
    def intelligence_signal_recipients_list(self) -> list[str]:
        """Parse intelligence signal recipients. Empty = Eyal only."""
        if not self.INTELLIGENCE_SIGNAL_RECIPIENTS:
            return [self.EYAL_EMAIL] if self.EYAL_EMAIL else []
        return [e.strip() for e in self.INTELLIGENCE_SIGNAL_RECIPIENTS.split(",") if e.strip()]

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.ENVIRONMENT.lower() == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.ENVIRONMENT.lower() == "development"


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Uses lru_cache to ensure settings are only loaded once.
    """
    return Settings()


# Singleton instance for easy import
settings = get_settings()
