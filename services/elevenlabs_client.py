"""
ElevenLabs client — text-to-speech + speech-to-text, via the v1 API (httpx async).

TTS (Intelligence Signal video narration) is gated by INTELLIGENCE_SIGNAL_VIDEO_ENABLED
+ ELEVENLABS_API_KEY (see is_available). STT (Scribe — Telegram voice-note intake,
comms/voice beat #1) is gated only by VOICE_INTAKE_ENABLED + ELEVENLABS_API_KEY
(see stt_available); it is independent of the video flag.

Usage:
    from services.elevenlabs_client import elevenlabs_client

    if elevenlabs_client.is_available():
        audio_bytes = await elevenlabs_client.text_to_speech("Hello world")
    if elevenlabs_client.stt_available():
        text = await elevenlabs_client.speech_to_text(ogg_bytes)
"""

import logging
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_STT_API_URL = "https://api.elevenlabs.io/v1/speech-to-text"


class ElevenLabsClient:
    """
    Async client for ElevenLabs text-to-speech API.

    Returns raw MP3 audio bytes for a given text input.
    Guarded by INTELLIGENCE_SIGNAL_VIDEO_ENABLED and ELEVENLABS_API_KEY.
    """

    def is_available(self) -> bool:
        """Check if ElevenLabs is configured and video is enabled."""
        return bool(
            settings.INTELLIGENCE_SIGNAL_VIDEO_ENABLED
            and settings.ELEVENLABS_API_KEY
        )

    async def text_to_speech(
        self,
        text: str,
        voice_id: str | None = None,
        model_id: str = "eleven_v3",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
    ) -> Optional[bytes]:
        """
        Convert text to speech audio.

        Args:
            text: The narration text to convert.
            voice_id: ElevenLabs voice ID (default from settings).
            model_id: TTS model to use.
            stability: Voice stability (0.0-1.0).
            similarity_boost: Voice similarity boost (0.0-1.0).

        Returns:
            MP3 audio bytes, or None on failure.
        """
        if not self.is_available():
            logger.warning("ElevenLabs not available (disabled or no API key)")
            return None

        voice = voice_id or settings.ELEVENLABS_VOICE_ID
        url = f"{ELEVENLABS_API_URL}/{voice}"

        headers = {
            "xi-api-key": settings.ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

        payload = {
            "text": text,
            "model_id": model_id,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, json=payload, headers=headers)

                if response.status_code == 429:
                    logger.warning("ElevenLabs rate limited")
                    return None

                response.raise_for_status()

                audio_bytes = response.content
                logger.info(
                    f"ElevenLabs TTS: {len(text)} chars -> "
                    f"{len(audio_bytes)} bytes audio"
                )
                return audio_bytes

        except httpx.TimeoutException:
            logger.error("ElevenLabs TTS timeout")
            return None
        except Exception as e:
            logger.error(f"ElevenLabs TTS failed: {e}")
            return None

    def stt_available(self) -> bool:
        """Check if ElevenLabs STT (Scribe) is enabled and configured.

        Independent of the video flag — voice intake only needs the API key.
        """
        return bool(settings.VOICE_INTAKE_ENABLED and settings.ELEVENLABS_API_KEY)

    async def speech_to_text(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str = "audio/ogg",
        language_code: str | None = None,
        model_id: str = "scribe_v2",
    ) -> Optional[str]:
        """
        Transcribe audio to text via ElevenLabs Scribe.

        Args:
            audio_bytes: Raw audio. Telegram voice notes are OGG/Opus — Scribe
                accepts them directly, so no transcoding is needed.
            mime_type: MIME type of the audio (default audio/ogg).
            language_code: Optional ISO-639 hint; None lets Scribe auto-detect
                (handles Hebrew / English / code-switching).
            model_id: Scribe model ("scribe_v2" = current best; "scribe_v1" fallback).

        Returns:
            The transcribed text, or None on failure / empty input.
        """
        if not settings.ELEVENLABS_API_KEY:
            logger.warning("ElevenLabs STT unavailable (no API key)")
            return None
        if not audio_bytes:
            return None

        headers = {"xi-api-key": settings.ELEVENLABS_API_KEY}
        data = {"model_id": model_id}
        if language_code:
            data["language_code"] = language_code
        files = {"file": ("voice.ogg", audio_bytes, mime_type)}

        try:
            # STT on a minute-plus note can be slow — more headroom than TTS.
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    ELEVENLABS_STT_API_URL, headers=headers, data=data, files=files
                )

                if response.status_code == 429:
                    logger.warning("ElevenLabs STT rate limited")
                    return None

                response.raise_for_status()

                text = (response.json() or {}).get("text")
                if text:
                    logger.info(
                        f"ElevenLabs STT: {len(audio_bytes)} bytes -> {len(text)} chars"
                    )
                return text or None

        except httpx.TimeoutException:
            logger.error("ElevenLabs STT timeout")
            return None
        except Exception as e:
            logger.error(f"ElevenLabs STT failed: {e}")
            return None


# Singleton
elevenlabs_client = ElevenLabsClient()
