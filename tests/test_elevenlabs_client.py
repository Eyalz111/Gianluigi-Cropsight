"""Tests for the ElevenLabs text-to-speech client."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from services.elevenlabs_client import ElevenLabsClient


@pytest.fixture
def client():
    return ElevenLabsClient()


class TestIsAvailable:
    def test_available_when_enabled_and_key_set(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            mock_settings.ELEVENLABS_API_KEY = "test-key"

            assert client.is_available() is True

    def test_not_available_when_disabled(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = False
            mock_settings.ELEVENLABS_API_KEY = "test-key"

            assert client.is_available() is False

    def test_not_available_without_key(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            mock_settings.ELEVENLABS_API_KEY = ""

            assert client.is_available() is False


class TestTextToSpeech:
    @pytest.mark.asyncio
    async def test_success(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            mock_settings.ELEVENLABS_API_KEY = "test-key"
            mock_settings.ELEVENLABS_VOICE_ID = "voice-123"

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.content = b"fake-mp3-audio-bytes"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.text_to_speech("Hello world")

        assert result == b"fake-mp3-audio-bytes"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_available(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = False
            mock_settings.ELEVENLABS_API_KEY = ""

            result = await client.text_to_speech("Hello world")

        assert result is None

    @pytest.mark.asyncio
    async def test_rate_limit_returns_none(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            mock_settings.ELEVENLABS_API_KEY = "test-key"
            mock_settings.ELEVENLABS_VOICE_ID = "voice-123"

            mock_resp = MagicMock()
            mock_resp.status_code = 429

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.text_to_speech("Hello world")

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            mock_settings.ELEVENLABS_API_KEY = "test-key"
            mock_settings.ELEVENLABS_VOICE_ID = "voice-123"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = httpx.TimeoutException("timeout")
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.text_to_speech("Hello world")

        assert result is None

    @pytest.mark.asyncio
    async def test_uses_custom_voice_id(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            mock_settings.ELEVENLABS_API_KEY = "test-key"
            mock_settings.ELEVENLABS_VOICE_ID = "default-voice"

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.content = b"audio"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                await client.text_to_speech("Hello", voice_id="custom-voice")

                # Verify URL contains custom voice ID
                call_args = mock_client.post.call_args
                url = call_args[0][0]
                assert "custom-voice" in url

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self, client):
        with patch("services.elevenlabs_client.settings") as mock_settings:
            mock_settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            mock_settings.ELEVENLABS_API_KEY = "test-key"
            mock_settings.ELEVENLABS_VOICE_ID = "voice-123"

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = httpx.ConnectError("Connection refused")
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await client.text_to_speech("Hello world")

        assert result is None
