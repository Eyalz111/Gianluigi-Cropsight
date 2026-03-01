"""
Centralized LLM helper for all Claude API calls.

Provides a single entry point for LLM calls with:
- Singleton Anthropic client (reuses HTTP connections)
- Automatic prompt caching on system prompts
- Token usage logging to Supabase
- Consistent error handling

Usage:
    from core.llm import call_llm

    # Simple call (user prompt only)
    text, usage = call_llm(
        prompt="Classify this...",
        model=settings.model_simple,
        max_tokens=1024,
        call_site="task_dedup",
    )

    # With system prompt (auto-cached)
    text, usage = call_llm(
        prompt="Extract from this transcript...",
        model=settings.model_extraction,
        max_tokens=4096,
        system="You are an expert meeting analyst...",
        call_site="transcript_extraction",
        meeting_id="uuid-here",
    )

NOT used by: core/agent.py (has its own tool-use loop with pre-cached
system prompt and tools array — refactoring would be high-risk, low gain).
"""

import logging
from typing import Any

from anthropic import Anthropic

from config.settings import settings

logger = logging.getLogger(__name__)

# Singleton client — reuses HTTP connections across all calls
_client: Anthropic | None = None


def get_client() -> Anthropic:
    """
    Get or create the singleton Anthropic client.

    Reuses the same client instance across all calls so HTTP connections
    are pooled instead of creating a new connection per call.

    Returns:
        Anthropic client instance.
    """
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def call_llm(
    prompt: str,
    model: str,
    max_tokens: int,
    call_site: str,
    system: str | None = None,
    meeting_id: str | None = None,
) -> tuple[str, dict]:
    """
    Single entry point for all Claude API calls (except agent tool-use).

    If a system prompt is provided, it's automatically wrapped with
    cache_control so repeated calls with the same system prompt benefit
    from prompt caching (e.g., transcript extraction retries).

    Args:
        prompt: The user message content.
        model: Model ID (e.g., settings.model_simple).
        max_tokens: Maximum tokens in the response.
        call_site: Short label for token tracking (e.g., "task_dedup").
        system: Optional system prompt. Gets cache_control automatically.
        meeting_id: Optional meeting UUID for token tracking.

    Returns:
        Tuple of (response_text, usage_dict).
        usage_dict has: input_tokens, output_tokens,
        cache_read_input_tokens, cache_creation_input_tokens.
    """
    client = get_client()

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    # Add system prompt with cache_control if provided
    if system:
        kwargs["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    response = client.messages.create(**kwargs)
    response_text = response.content[0].text

    # Extract usage info
    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", 0),
        "output_tokens": getattr(response.usage, "output_tokens", 0),
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
    }

    # Log usage (best-effort, never raises)
    _log_usage(
        call_site=call_site,
        model=model,
        usage=usage,
        meeting_id=meeting_id,
    )

    return response_text, usage


def _log_usage(
    call_site: str,
    model: str,
    usage: dict,
    meeting_id: str | None = None,
) -> None:
    """
    Write token usage to the Supabase token_usage table.

    Best-effort: silently logs and continues on any error.
    Never raises exceptions — token tracking must not break the pipeline.

    Args:
        call_site: Short label (e.g., "task_dedup").
        model: Model ID used.
        usage: Dict with input_tokens, output_tokens, cache_read/creation.
        meeting_id: Optional meeting UUID.
    """
    try:
        from services.supabase_client import supabase_client

        row = {
            "call_site": call_site,
            "model": model,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        }
        if meeting_id:
            row["meeting_id"] = meeting_id

        supabase_client.client.table("token_usage").insert(row).execute()
    except Exception as e:
        logger.debug(f"Token usage logging failed (non-fatal): {e}")
