"""The orchestration spine: one seam for inbound normalization + outbound sends.

This is the backbone the comms/voice phase is built on (V2.5_STRATEGY.md §6).
Beat #1 wires Telegram + text/voice-in through it; later beats (WhatsApp,
voice-out, the debrief call) plug into the same two surfaces:

OUTBOUND — thin verbatim pass-throughs to the Telegram bot's ``send_*``
methods, forwarding ``*args/**kwargs`` unchanged so behavior is byte-for-byte
preserved. This lets every call site migrate to the spine with zero behavior
change (PR B); the per-channel routing that picks *where* to reach Eyal will
live in :meth:`CommsSpine.send` in a later beat.

INBOUND — :meth:`CommsSpine.handle_inbound` normalizes any channel/modality
into one path that calls the brain (``gianluigi_agent.process_message``) and
returns the same result dict the Telegram layer already renders. Voice notes
are transcribed first; the Router (not the channel) then decides whether a
note is an injection-to-confirm or a question-to-answer.

``telegram_bot``, ``gianluigi_agent`` and ``elevenlabs_client`` are imported
lazily *inside* methods to avoid an import cycle — ``guardrails/approval_flow``
imports ``telegram_bot`` at module load, so the spine must not pull a
``spine -> telegram_bot -> ... -> spine`` loop at import time.
"""
from __future__ import annotations

import logging

from services.orchestrator.events import Channel, InboundEvent, Modality

logger = logging.getLogger(__name__)

# Minimum usable transcript before we bother the brain — guards against
# butt-dial / background noise that STT renders as "uh". Intentionally
# conservative: an ultra-short note gets a "resend?" rather than a bad inject.
_MIN_TRANSCRIPT_CHARS = 5
_MIN_TRANSCRIPT_WORDS = 2


class CommsSpine:
    """Channel/modality-agnostic messaging seam. Use the ``comms_spine`` singleton."""

    def _bot(self):
        # Lazy import — see module docstring (import-cycle avoidance).
        from services.telegram_bot import telegram_bot

        return telegram_bot

    # ------------------------------------------------------------------ #
    # Outbound facade — verbatim pass-throughs (forward *args/**kwargs).  #
    # Do NOT normalize/strip kwargs here: forwarding verbatim is what     #
    # makes the call-site migration (PR B) behavior-preserving. The known #
    # latent send_to_group(parse_mode=...) mismatch is preserved on       #
    # purpose; fixing it is a separate, intentional change.               #
    # ------------------------------------------------------------------ #
    async def send_message(self, *args, **kwargs) -> bool:
        return await self._bot().send_message(*args, **kwargs)

    async def send_to_eyal(self, *args, **kwargs) -> bool:
        return await self._bot().send_to_eyal(*args, **kwargs)

    async def send_to_group(self, *args, **kwargs) -> bool:
        return await self._bot().send_to_group(*args, **kwargs)

    async def send_meeting_summary(self, *args, **kwargs) -> bool:
        return await self._bot().send_meeting_summary(*args, **kwargs)

    async def send_approval_request(self, *args, **kwargs) -> bool:
        return await self._bot().send_approval_request(*args, **kwargs)

    async def send_prep_outline(self, *args, **kwargs) -> bool:
        return await self._bot().send_prep_outline(*args, **kwargs)

    async def send_stakeholder_approval_request(self, *args, **kwargs) -> bool:
        return await self._bot().send_stakeholder_approval_request(*args, **kwargs)

    async def send_task_reminder(self, *args, **kwargs) -> bool:
        return await self._bot().send_task_reminder(*args, **kwargs)

    async def send_raw(self, *args, **kwargs):
        """Escape hatch for the two sites that bypass the public API and call
        ``telegram_bot.app.bot.send_message`` directly (arbitrary chat_id)."""
        return await self._bot().app.bot.send_message(*args, **kwargs)

    async def send(
        self,
        *,
        to: str | int,
        text: str,
        channel: Channel = Channel.TELEGRAM,
        modality: Modality = Modality.TEXT,
        reply_markup=None,
        parse_mode: str | None = None,
    ) -> bool:
        """Channel/modality-aware outbound entry — the seam for later beats.

        Beat #1 implements Telegram + text only; any other channel/modality
        raises ``NotImplementedError`` so a future WhatsApp adapter or voice-out
        lands here without changing a single call site.
        """
        if channel != Channel.TELEGRAM:
            raise NotImplementedError(
                f"channel {channel!r} not implemented yet (beat #1 = Telegram)"
            )
        if modality != Modality.TEXT:
            raise NotImplementedError(
                f"modality {modality!r} not implemented yet (voice-out is beat #4)"
            )
        return await self._bot().send_message(
            to, text, parse_mode=parse_mode or "Markdown", reply_markup=reply_markup
        )

    # ------------------------------------------------------------------ #
    # Inbound normalization.                                              #
    # ------------------------------------------------------------------ #
    async def handle_inbound(self, event: InboundEvent) -> dict:
        """Normalize an inbound event -> the brain -> result dict.

        VOICE events are transcribed first. A missing or sub-floor transcript
        short-circuits to ``stt_failed`` so the brain never tries to extract
        items from noise. TEXT (and transcribed VOICE) dispatch straight to
        ``process_message`` — the Router decides what to do with it.
        """
        if event.modality == Modality.VOICE and not event.text:
            transcript = await self._transcribe(event)
            if not self._usable_transcript(transcript):
                return {
                    "action": "stt_failed",
                    "response": "I couldn't make out that voice note — resend it or type it?",
                }
            event.text = transcript
            event.raw_transcript = transcript

        if not event.text:
            return {"action": "noop", "response": ""}

        # Lazy import — see module docstring.
        from core.agent import gianluigi_agent

        return await gianluigi_agent.process_message(
            user_message=event.text,
            user_id=event.sender_id,
            conversation_history=event.conversation_history,
            allow_writes=event.allow_writes,
            max_sensitivity_level=event.max_sensitivity_level,
        )

    async def _transcribe(self, event: InboundEvent) -> str | None:
        """Transcribe the event's audio via ElevenLabs Scribe (added in PR D)."""
        if event.audio_bytes is None:
            return None
        from services.elevenlabs_client import elevenlabs_client

        return await elevenlabs_client.speech_to_text(
            event.audio_bytes, mime_type=event.audio_mime
        )

    @staticmethod
    def _usable_transcript(transcript: str | None) -> bool:
        if not transcript:
            return False
        t = transcript.strip()
        return len(t) >= _MIN_TRANSCRIPT_CHARS and len(t.split()) >= _MIN_TRANSCRIPT_WORDS


comms_spine = CommsSpine()
