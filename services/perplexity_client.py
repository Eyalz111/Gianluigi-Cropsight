"""
Perplexity API client for intelligence research queries.

Uses the Sonar API (OpenAI-compatible endpoint) for web search with citations.
Supports single queries and parallel batch execution with concurrency control.

Usage:
    from services.perplexity_client import perplexity_client

    result = await perplexity_client.search("wheat harvest forecast 2026")
    results = await perplexity_client.search_batch(queries, max_concurrent=6)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"


class PerplexityError(Exception):
    """Raised when a Perplexity API call fails."""


@dataclass
class PerplexityResult:
    """Result from a single Perplexity search query."""

    query: str
    content: str
    citations: list[str] = field(default_factory=list)
    model: str = ""
    success: bool = True
    error: Optional[str] = None


class PerplexityClient:
    """
    Async client for Perplexity Sonar API.

    Supports single queries and parallel batch queries with concurrency control.
    All results include citations for source transparency.
    """

    def __init__(self):
        self._model = "sonar-pro"

    def is_available(self) -> bool:
        """Check if Perplexity API key is configured."""
        return bool(settings.PERPLEXITY_API_KEY)

    async def search(
        self,
        query: str,
        system_prompt: str = "",
    ) -> PerplexityResult:
        """
        Run a single Perplexity search query.

        Args:
            query: The research question.
            system_prompt: Optional context to frame the query.

        Returns:
            PerplexityResult with content and citations.
        """
        if not settings.PERPLEXITY_API_KEY:
            return PerplexityResult(
                query=query,
                content="",
                success=False,
                error="PERPLEXITY_API_KEY not set",
            )

        model = settings.PERPLEXITY_MODEL or self._model

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": query})

        payload = {
            "model": model,
            "messages": messages,
            "web_search_options": {"search_context_size": "low"},
            "return_citations": True,
        }

        headers = {
            "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    PERPLEXITY_API_URL, json=payload, headers=headers
                )

                if response.status_code == 429:
                    logger.warning(f"Perplexity rate limited on: '{query[:60]}'")
                    return PerplexityResult(
                        query=query,
                        content="",
                        success=False,
                        error="rate_limited",
                    )

                response.raise_for_status()
                data = response.json()

            content = data["choices"][0]["message"]["content"]
            citations = data.get("citations", [])
            model_used = data.get("model", model)

            return PerplexityResult(
                query=query,
                content=content,
                citations=citations,
                model=model_used,
                success=True,
            )

        except httpx.TimeoutException:
            logger.error(f"Perplexity timeout on: '{query[:60]}'")
            return PerplexityResult(
                query=query,
                content="",
                success=False,
                error="timeout",
            )
        except Exception as e:
            logger.error(f"Perplexity query failed: '{query[:60]}' — {e}")
            return PerplexityResult(
                query=query,
                content="",
                success=False,
                error=str(e),
            )

    async def search_batch(
        self,
        queries: list[dict],
        max_concurrent: int = 6,
    ) -> dict[str, PerplexityResult]:
        """
        Run multiple queries in parallel with concurrency control.

        Args:
            queries: List of dicts with 'query', optional 'system_prompt', and 'section' label.
            max_concurrent: Max simultaneous API calls.

        Returns:
            Dict mapping section label -> PerplexityResult.
        """
        if not queries:
            return {}

        semaphore = asyncio.Semaphore(max_concurrent)

        async def _bounded_search(item: dict) -> tuple[str, PerplexityResult]:
            async with semaphore:
                result = await self.search(
                    item["query"],
                    system_prompt=item.get("system_prompt", ""),
                )
                return item["section"], result

        tasks = [_bounded_search(q) for q in queries]
        results_raw = await asyncio.gather(*tasks, return_exceptions=True)

        results = {}
        for item in results_raw:
            if isinstance(item, Exception):
                logger.error(f"Batch search task failed: {item}")
            else:
                section, result = item
                results[section] = result

        successful = sum(1 for r in results.values() if r.success)
        logger.info(f"Perplexity batch: {successful}/{len(queries)} queries succeeded")
        return results


# Singleton
perplexity_client = PerplexityClient()
