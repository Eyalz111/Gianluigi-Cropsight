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
    STAKEHOLDER_TRACKER_SHEET_ID: str = Field(default="", description="Stakeholder Tracker Sheet ID")

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

    # ==========================================================================
    # Calendar Configuration
    # ==========================================================================
    CROPSIGHT_CALENDAR_COLOR_ID: str = Field(
        default="",
        description="Google Calendar color ID for CropSight meetings (purple)"
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
        default=300, description="Email watcher check interval (seconds)"
    )
    TRANSCRIPT_POLL_INTERVAL: int = Field(
        default=300, description="Transcript watcher poll interval (seconds)"
    )
    DOCUMENT_POLL_INTERVAL: int = Field(
        default=300, description="Document watcher poll interval (seconds)"
    )
    MEETING_PREP_CHECK_INTERVAL: int = Field(
        default=14400, description="Meeting prep scheduler check interval (seconds)"
    )
    MEETING_PREP_HOURS_BEFORE: int = Field(
        default=24, description="Hours before meeting to generate prep document"
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

    # ==========================================================================
    # v1.0 — MCP Server
    # ==========================================================================
    MCP_AUTH_TOKEN: str = Field(default="", description="Auth token for MCP server")
    MCP_PORT: int = Field(default=8080, description="MCP server port (shared with health server)")

    # ==========================================================================
    # v1.0 — Weekly Review
    # ==========================================================================
    WEEKLY_REVIEW_CALENDAR_TITLE: str = Field(
        default="CropSight: Weekly Review with Gianluigi",
        description="Calendar event title for weekly review sessions"
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
    # Approval Mode (v0.2)
    # ==========================================================================
    APPROVAL_MODE: str = Field(
        default="manual",
        description="Approval mode: 'manual' (default) or 'auto_review' for timed auto-publish"
    )
    AUTO_REVIEW_WINDOW_MINUTES: int = Field(
        default=60,
        description="Minutes to wait before auto-publishing in auto_review mode"
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
