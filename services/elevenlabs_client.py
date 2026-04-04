"""
ElevenLabs text-to-speech client for Intelligence Signal video narration.

Built disabled — only active when both INTELLIGENCE_SIGNAL_VIDEO_ENABLED=True
and ELEVENLABS_API_KEY is set.

Uses the ElevenLabs v1 text-to-speech API with httpx async client.

Usage:
    from services.elevenlabs_client import elevenlabs_client

    if elevenlabs_client.is_available():
        audio_bytes = await elevenlabs_client.text_to_speech("Hello world")
"""

import logging
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"


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


# Singleton
elevenlabs_client = ElevenLabsClient()
