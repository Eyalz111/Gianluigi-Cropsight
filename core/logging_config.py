"""
Structured logging configuration for Gianluigi.

Provides a custom formatter that outputs JSON in production (for Cloud Logging)
and human-readable text in development. No library dependency — uses only
the stdlib logging module.

Existing logger.info(f"...") calls work unchanged; only the output format changes.

Usage:
    from core.logging_config import setup_logging

    setup_logging(level="INFO", environment="production")
"""

import json
import logging
import sys
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    """
    Log formatter that outputs JSON in production, plain text in dev.

    In production (environment="production"):
        {"timestamp": "...", "level": "INFO", "logger": "gianluigi", "message": "..."}

    In development (any other environment):
        2026-03-01 12:00:00 - gianluigi - INFO - Some message
    """

    DEV_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    def __init__(self, environment: str = "development"):
        """
        Initialize the formatter.

        Args:
            environment: 'production' for JSON, anything else for plain text.
        """
        super().__init__(fmt=self.DEV_FORMAT)
        self.environment = environment.lower()

    def format(self, record: logging.LogRecord) -> str:
        """
        Format a log record.

        In production: returns a JSON string.
        In development: returns standard human-readable format.

        Args:
            record: The log record to format.

        Returns:
            Formatted log string.
        """
        if self.environment == "production":
            log_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            return json.dumps(log_entry)

        # Dev mode: use standard formatting
        return super().format(record)


def setup_logging(level: str = "INFO", environment: str = "development") -> None:
    """
    Configure the root logger with the appropriate formatter.

    Replaces logging.basicConfig() with structured output.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        environment: 'production' for JSON output, otherwise plain text.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Create handler with structured formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter(environment=environment))
    root_logger.addHandler(handler)
