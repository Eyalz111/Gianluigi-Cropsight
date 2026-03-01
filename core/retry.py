"""
Retry decorator for transient failures.

Provides exponential backoff retry logic for external API calls
(Google APIs, Supabase, etc.). Does NOT retry on client errors
(4xx) — only on transient server/connection errors.

Usage:
    from core.retry import retry

    @retry(max_attempts=3, backoff=2)
    async def call_google_api():
        ...
"""

import asyncio
import functools
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Exceptions considered transient and worth retrying
TRANSIENT_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    OSError,
)


def retry(
    max_attempts: int = 3,
    backoff: float = 2.0,
    base_delay: float = 1.0,
    transient_exceptions: tuple = TRANSIENT_EXCEPTIONS,
):
    """
    Retry decorator with exponential backoff.

    Only retries on transient errors (connection, timeout, server errors).
    Does NOT retry on client errors (bad request, auth, etc.).

    Args:
        max_attempts: Maximum number of attempts (including first try).
        backoff: Backoff multiplier (delay = base_delay * backoff^attempt).
        base_delay: Initial delay in seconds.
        transient_exceptions: Tuple of exception types to retry on.

    Returns:
        Decorator function.
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except transient_exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = base_delay * (backoff ** attempt)
                        logger.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_attempts}): "
                            f"{type(e).__name__}: {e}. Retrying in {delay:.1f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"{func.__name__} failed after {max_attempts} attempts: "
                            f"{type(e).__name__}: {e}"
                        )
            raise last_exception

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except transient_exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        delay = base_delay * (backoff ** attempt)
                        logger.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_attempts}): "
                            f"{type(e).__name__}: {e}. Retrying in {delay:.1f}s..."
                        )
                        import time
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"{func.__name__} failed after {max_attempts} attempts: "
                            f"{type(e).__name__}: {e}"
                        )
            raise last_exception

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
