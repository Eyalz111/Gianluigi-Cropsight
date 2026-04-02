"""Tests for the Perplexity API client."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.perplexity_client import PerplexityClient, PerplexityResult


@pytest.fixture
def client():
    return PerplexityClient()


@pytest.fixture
def mock_response():
    """Standard successful Perplexity API response."""
    return {
        "choices": [{"message": {"content": "Wheat prices rose 4% this week."}}],
        "citations": ["https://example.com/wheat-report"],
        "model": "sonar-pro",
    }


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_success(self, client, mock_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = mock_response

        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = "pplx-test-key"
            mock_settings.PERPLEXITY_MODEL = "sonar-pro"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.search("wheat harvest forecast")

        assert result.success is True
        assert "Wheat prices" in result.content
        assert len(result.citations) == 1
        assert result.error is None

    @pytest.mark.asyncio
    async def test_search_missing_api_key(self, client):
        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = ""

            result = await client.search("test query")

        assert result.success is False
        assert result.error == "PERPLEXITY_API_KEY not set"
        assert result.content == ""

    @pytest.mark.asyncio
    async def test_search_rate_limit_429(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 429

        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = "pplx-test-key"
            mock_settings.PERPLEXITY_MODEL = "sonar-pro"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.search("test query")

        assert result.success is False
        assert result.error == "rate_limited"

    @pytest.mark.asyncio
    async def test_search_timeout(self, client):
        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = "pplx-test-key"
            mock_settings.PERPLEXITY_MODEL = "sonar-pro"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = httpx.TimeoutException("Connection timed out")
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.search("test query")

        assert result.success is False
        assert result.error == "timeout"

    @pytest.mark.asyncio
    async def test_search_network_error(self, client):
        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = "pplx-test-key"
            mock_settings.PERPLEXITY_MODEL = "sonar-pro"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = httpx.ConnectError("Connection refused")
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.search("test query")

        assert result.success is False
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_search_with_system_prompt(self, client, mock_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = mock_response

        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = "pplx-test-key"
            mock_settings.PERPLEXITY_MODEL = "sonar-pro"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.search(
                    "wheat prices", system_prompt="Focus on European markets."
                )

                # Verify system prompt was included in the payload
                call_args = mock_client.post.call_args
                payload = call_args.kwargs.get("json") or call_args[1].get("json")
                messages = payload["messages"]
                assert messages[0]["role"] == "system"
                assert "European" in messages[0]["content"]

        assert result.success is True


class TestSearchBatch:
    @pytest.mark.asyncio
    async def test_batch_search_all_succeed(self, client, mock_response):
        queries = [
            {"section": "commodity_wheat", "query": "wheat prices"},
            {"section": "commodity_coffee", "query": "coffee prices"},
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = mock_response

        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = "pplx-test-key"
            mock_settings.PERPLEXITY_MODEL = "sonar-pro"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                results = await client.search_batch(queries)

        assert len(results) == 2
        assert "commodity_wheat" in results
        assert "commodity_coffee" in results
        assert all(r.success for r in results.values())

    @pytest.mark.asyncio
    async def test_batch_search_partial_failure(self, client):
        queries = [
            {"section": "good", "query": "wheat prices"},
            {"section": "bad", "query": "will fail"},
        ]

        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.raise_for_status = MagicMock()
        good_resp.json.return_value = {
            "choices": [{"message": {"content": "Good result"}}],
            "citations": [],
            "model": "sonar-pro",
        }

        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise httpx.TimeoutException("timeout")
            return good_resp

        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = "pplx-test-key"
            mock_settings.PERPLEXITY_MODEL = "sonar-pro"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = side_effect
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                results = await client.search_batch(queries)

        assert len(results) == 2
        success_count = sum(1 for r in results.values() if r.success)
        failure_count = sum(1 for r in results.values() if not r.success)
        assert success_count == 1
        assert failure_count == 1

    @pytest.mark.asyncio
    async def test_batch_search_empty_list(self, client):
        results = await client.search_batch([])
        assert results == {}


class TestIsAvailable:
    def test_available_with_key(self, client):
        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = "pplx-test-key"
            assert client.is_available() is True

    def test_not_available_without_key(self, client):
        with patch("services.perplexity_client.settings") as mock_settings:
            mock_settings.PERPLEXITY_API_KEY = ""
            assert client.is_available() is False
