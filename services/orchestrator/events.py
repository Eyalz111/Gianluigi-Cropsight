"""Normalized inbound-message events for the orchestration spine.

The spine is the single seam every inbound message and outbound send flows
through, so future channels (WhatsApp) and modalities (voice-out) plug in
without touching call sites. Beat #1 implements Telegram + text/voice-in only;
the enums are intentionally minimal — no speculative WHATSAPP / VOICE_OUT
members until a second channel actually lands (see V2.5_STRATEGY.md §6).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Channel(str, Enum):
    """Where a message arrives / is sent. Add members as channels land."""

    TELEGRAM = "telegram"


class Modality(str, Enum):
    """How a message is carried. Voice-out (TTS reply) is beat #4."""

    TEXT = "text"
    VOICE = "voice"


@dataclass
class InboundEvent:
    """A channel/modality-agnostic inbound message.

    The channel adapter (e.g. the Telegram handler) does the channel-specific
    work — resolving the app-level user id, fetching conversation history,
    downloading audio — and hands the spine a normalized event. The spine
    transcribes voice (if needed) and dispatches the text to the brain.

    Attributes:
        sender_id: The *resolved app-level* user id (e.g. "eyal"), not the raw
            channel id — the adapter maps it before constructing the event.
        text: Present for TEXT, or filled by the spine after STT for VOICE.
        audio_bytes: Present for VOICE before transcription.
        message_id: Source-channel message id (Telegram message_id) — used for
            dedupe and inject provenance.
        raw_transcript: Filled by the spine after STT; carried into the
            confirmation UI as the "heard:" safety-net line.
    """

    channel: Channel
    modality: Modality
    sender_id: str
    chat_id: str | int
    text: str | None = None
    audio_bytes: bytes | None = None
    audio_mime: str = "audio/ogg"
    message_id: str | None = None
    raw_transcript: str | None = None
    conversation_history: list | None = None
