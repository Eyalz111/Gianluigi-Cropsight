"""Tests for ElevenLabs Scribe speech-to-text (comms/voice beat #1, PR D).

NOTE: tests patch `services.elevenlabs_client.settings` with a bare MagicMock, whose
unset attributes read as truthy. So every test sets the flags it depends on EXPLICITLY
(VOICE_INTAKE_ENABLED / INTELLIGENCE_SIGNAL_VIDEO_ENABLED / ELEVENLABS_API_KEY).
"""
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.elevenlabs_client import ElevenLabsClient


@pytest.fixture
def client():
    return ElevenLabsClient()


def _mock_httpx(response):
    """Build a patch context for httpx.AsyncClient whose .post returns `response`."""
    mock_client = AsyncMock()
    mock_client.post.return_value = response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    cm = patch("httpx.AsyncClient")
    return cm, mock_client


class TestSttAvailable:
    def test_available_when_flag_on_and_key_set(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.VOICE_INTAKE_ENABLED = True
            s.ELEVENLABS_API_KEY = "test-key"
            assert client.stt_available() is True

    def test_not_available_when_flag_off(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.VOICE_INTAKE_ENABLED = False
            s.ELEVENLABS_API_KEY = "test-key"
            assert client.stt_available() is False

    def test_not_available_without_key(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.VOICE_INTAKE_ENABLED = True
            s.ELEVENLABS_API_KEY = ""
            assert client.stt_available() is False


class TestSpeechToText:
    async def test_success_returns_transcript(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.ELEVENLABS_API_KEY = "test-key"
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"text": "ship the demo by friday"})
            cm, mock_client = _mock_httpx(resp)
            with cm as cls:
                cls.return_value = mock_client
                result = await client.speech_to_text(b"ogg-bytes")
        assert result == "ship the demo by friday"
        # Posts multipart with the audio file + scribe_v2; no language pin (auto-detect).
        kwargs = mock_client.post.call_args.kwargs
        assert kwargs["data"]["model_id"] == "scribe_v2"
        assert "language_code" not in kwargs["data"]
        assert kwargs["files"]["file"][1] == b"ogg-bytes"
        assert kwargs["headers"]["xi-api-key"] == "test-key"

    async def test_language_code_passed_through_when_given(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.ELEVENLABS_API_KEY = "test-key"
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"text": "shalom"})
            cm, mock_client = _mock_httpx(resp)
            with cm as cls:
                cls.return_value = mock_client
                await client.speech_to_text(b"x", language_code="heb")
        assert mock_client.post.call_args.kwargs["data"]["language_code"] == "heb"

    async def test_rate_limit_returns_none(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.ELEVENLABS_API_KEY = "test-key"
            resp = MagicMock()
            resp.status_code = 429
            cm, mock_client = _mock_httpx(resp)
            with cm as cls:
                cls.return_value = mock_client
                result = await client.speech_to_text(b"x")
        assert result is None

    async def test_timeout_returns_none(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.ELEVENLABS_API_KEY = "test-key"
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("slow")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            with patch("httpx.AsyncClient", return_value=mock_client):
                result = await client.speech_to_text(b"x")
        assert result is None

    async def test_empty_text_in_response_returns_none(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.ELEVENLABS_API_KEY = "test-key"
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"text": ""})
            cm, mock_client = _mock_httpx(resp)
            with cm as cls:
                cls.return_value = mock_client
                result = await client.speech_to_text(b"x")
        assert result is None

    async def test_no_key_returns_none_without_request(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.ELEVENLABS_API_KEY = ""
            with patch("httpx.AsyncClient") as cls:
                result = await client.speech_to_text(b"x")
                cls.assert_not_called()
        assert result is None

    async def test_empty_audio_returns_none(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.ELEVENLABS_API_KEY = "test-key"
            with patch("httpx.AsyncClient") as cls:
                result = await client.speech_to_text(b"")
                cls.assert_not_called()
        assert result is None


class TestUngatingRegression:
    """Adding STT must not change the video TTS gate, and STT must be independent of it."""

    def test_video_gate_unchanged_when_voice_flag_off(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            s.ELEVENLABS_API_KEY = "test-key"
            s.VOICE_INTAKE_ENABLED = False
            assert client.is_available() is True   # video TTS still gated only by its flag
            assert client.stt_available() is False  # STT off

    def test_stt_independent_of_video_flag(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = False
            s.ELEVENLABS_API_KEY = "test-key"
            s.VOICE_INTAKE_ENABLED = True
            assert client.stt_available() is True    # STT works without the video flag
            assert client.is_available() is False    # video TTS stays off
