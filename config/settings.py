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
    DRIVE_POLL_INTERVAL_MINUTES: int = Field(
        default=15,
        description="How often to check for new transcripts (minutes)"
    )
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    ENVIRONMENT: str = Field(default="development", description="Environment name")

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
