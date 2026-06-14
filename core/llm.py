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

call_llm_with_tools() handles tool-use API calls for the multi-agent
architecture, centralizing token tracking for Router, Conversation,
Analyst, and Operator agents.
"""

import logging
import time
from typing import Any

from anthropic import Anthropic

from config.settings import settings

logger = logging.getLogger(__name__)

# Singleton client — reuses HTTP connections across all calls
_client: Anthropic | None = None

# --- Out-of-credits alerting -------------------------------------------------
# When the Anthropic account runs out of credits EVERY call_llm fails, which
# silently takes down every AI feature (extraction, brief, agent replies, the
# intelligence signal). Detect that here — the single Anthropic gateway — and
# alert Eyal ONCE so an empty balance is never silent again. Best-effort and
# deduped; never raises into the LLM path.
_main_loop = None                       # set by main.py at startup
_last_credit_alert_ts: float = 0.0
_CREDIT_ALERT_COOLDOWN_S = 6 * 3600     # alert at most once per 6h


def register_alert_loop(loop) -> None:
    """main.py registers the app's event loop so sync/executor-thread call_llm
    failures can schedule the async Telegram alert onto it."""
    global _main_loop
    _main_loop = loop


def _is_credit_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return "credit balance is too low" in s or ("credit" in s and "too low" in s)


def _note_anthropic_credit_exhausted(exc: Exception) -> None:
    """Alert Eyal that Claude credits are exhausted (deduped, best-effort)."""
    global _last_credit_alert_ts
    try:
        now = time.time()
        if now - _last_credit_alert_ts < _CREDIT_ALERT_COOLDOWN_S:
            return
        _last_credit_alert_ts = now
        logger.critical(
            "Anthropic credit balance exhausted — ALL AI features are down until "
            "credits are added (console.anthropic.com → Plans & Billing)."
        )
        # Durable audit row (sync — reliable from any thread), so a backstop
        # check can surface it even if the live Telegram ping can't be scheduled.
        try:
            from services.supabase_client import supabase_client
            supabase_client.log_action(
                "anthropic_credit_exhausted",
                details={"error": str(exc)[:300]},
                triggered_by="system",
            )
        except Exception:
            pass
        # Best-effort live Telegram DM to Eyal, scheduled onto the main loop
        # (call_llm runs sync, often in an executor thread with no running loop).
        if _main_loop is not None:
            import asyncio
            from services.alerting import send_system_alert, AlertSeverity
            msg = (
                "🚨 Out of Anthropic (Claude) credits — every AI feature is paused "
                "(replies, morning brief, meeting extraction, the intelligence signal). "
                "Top up at console.anthropic.com → Plans & Billing to resume."
            )
            try:
                asyncio.run_coroutine_threadsafe(
                    send_system_alert(AlertSeverity.CRITICAL, "anthropic_billing", msg),
                    _main_loop,
                )
            except Exception as sched_err:
                logger.error(f"Could not schedule credit alert: {sched_err}")
    except Exception as e:
        logger.error(f"Credit-exhaustion alert handling failed: {e}")


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

    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        if _is_credit_error(e):
            _note_anthropic_credit_exhausted(e)
        raise
    # Guard against an empty/truncated content block (an overloaded or truncated
    # response can return content=[]) — a bare content[0].text raises a confusing
    # IndexError out of call_llm instead of a clean, retryable error. [audit P6-08]
    if response.content and hasattr(response.content[0], "text"):
        response_text = response.content[0].text
    else:
        raise RuntimeError(
            f"Claude API returned no text content (stop_reason="
            f"{getattr(response, 'stop_reason', '?')})"
        )

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


def call_llm_with_tools(
    messages: list[dict],
    model: str,
    max_tokens: int,
    system: list[dict],
    tools: list[dict],
    call_site: str,
) -> Any:
    """
    Make a Claude API call with tool use support.

    Centralizes all tool-use API calls through llm.py for consistent
    token tracking. Each agent passes a descriptive call_site label
    (e.g., "router", "conversation_agent") so costs appear as separate
    entries in the token_usage table.

    Args:
        messages: Conversation messages array.
        model: Model ID (e.g., settings.model_agent).
        max_tokens: Maximum tokens in the response.
        system: Pre-formatted system prompt with cache_control.
        tools: Tool definitions array.
        call_site: Short label for token tracking.

    Returns:
        Raw Anthropic API response object.
    """
    client = get_client()

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
    except Exception as e:
        if _is_credit_error(e):
            _note_anthropic_credit_exhausted(e)
        raise

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
    )

    return response


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
