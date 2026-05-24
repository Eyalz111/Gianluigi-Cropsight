"""Tests for voice-OUT TTS gating (comms/voice beat #4, PR 1).

Tests patch `services.elevenlabs_client.settings` with a bare MagicMock (unset attrs read
truthy), so each test sets the flags it depends on EXPLICITLY.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.elevenlabs_client import ElevenLabsClient


@pytest.fixture
def client():
    return ElevenLabsClient()


def _mock_httpx_success(content=b"fake-mp3"):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.content = content
    mc = AsyncMock()
    mc.post.return_value = resp
    mc.__aenter__ = AsyncMock(return_value=mc)
    mc.__aexit__ = AsyncMock(return_value=False)
    return mc


class TestTtsAvailable:
    def test_available_when_flag_on_and_key(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.VOICE_OUT_ENABLED = True
            s.ELEVENLABS_API_KEY = "k"
            assert client.tts_available() is True

    def test_not_available_when_flag_off(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.VOICE_OUT_ENABLED = False
            s.ELEVENLABS_API_KEY = "k"
            assert client.tts_available() is False

    def test_not_available_without_key(self, client):
        with patch("services.elevenlabs_client.settings") as s:
            s.VOICE_OUT_ENABLED = True
            s.ELEVENLABS_API_KEY = ""
            assert client.tts_available() is False


class TestTextToSpeechGate:
    async def test_works_via_voice_out_flag_when_video_off(self, client):
        """Un-gating: voice-out flag on + video flag OFF -> TTS works."""
        with patch("services.elevenlabs_client.settings") as s:
            s.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = False
            s.VOICE_OUT_ENABLED = True
            s.ELEVENLABS_API_KEY = "k"
            s.ELEVENLABS_VOICE_ID = "v"
            with patch("httpx.AsyncClient", return_value=_mock_httpx_success()):
                result = await client.text_to_speech("hello")
        assert result == b"fake-mp3"

    async def test_video_path_unchanged_when_voice_out_off(self, client):
        """Regression: video flag on + voice-out OFF -> still works (video path intact)."""
        with patch("services.elevenlabs_client.settings") as s:
            s.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = True
            s.VOICE_OUT_ENABLED = False
            s.ELEVENLABS_API_KEY = "k"
            s.ELEVENLABS_VOICE_ID = "v"
            with patch("httpx.AsyncClient", return_value=_mock_httpx_success()):
                result = await client.text_to_speech("hello")
        assert result == b"fake-mp3"

    async def test_blocked_when_both_flags_off(self, client):
        """Neither flag -> blocked before any HTTP call, even with a key."""
        with patch("services.elevenlabs_client.settings") as s:
            s.INTELLIGENCE_SIGNAL_VIDEO_ENABLED = False
            s.VOICE_OUT_ENABLED = False
            s.ELEVENLABS_API_KEY = "k"
            with patch("httpx.AsyncClient") as cls:
                result = await client.text_to_speech("hello")
                cls.assert_not_called()
        assert result is None
