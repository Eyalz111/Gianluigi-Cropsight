"""Tests for voice-OUT: the "Listen" button + playback callback (beat #4, PR 2).

The send_message attach rule is gated on `settings.VOICE_OUT_ENABLED is True` (strict
identity), so tests set it to a real bool. Patch `_app` (not `app`, which is a property).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.telegram_bot import telegram_bot

LONG = "Here's where the U Bank deal stands, and what I'd suggest we do before the Thursday board call - in detail."  # well over 60


async def _send(text, *, flag, chat_id="123", eyal="123", reply_markup=None):
    sent = AsyncMock()
    ms = MagicMock()
    ms.VOICE_OUT_ENABLED = flag  # real bool, so `is True` behaves
    with patch("services.telegram_bot.settings", ms), patch.object(
        telegram_bot, "eyal_chat_id", eyal
    ), patch.object(telegram_bot, "_bot_send_message", sent):
        await telegram_bot.send_message(chat_id, text, reply_markup=reply_markup)
    return sent.call_args.kwargs.get("reply_markup")


# --------------------------- send_message attach rule --------------------------- #
async def test_attach_listen_for_eyal_prose():
    assert await _send(LONG, flag=True) is not None  # Listen button attached


async def test_no_listen_when_flag_off():
    assert await _send(LONG, flag=False) is None


async def test_no_listen_for_group_chat():
    assert await _send(LONG, flag=True, chat_id="-100999", eyal="123") is None


async def test_no_listen_for_short_ack():
    assert await _send("Thinking...", flag=True) is None


async def test_no_listen_when_message_already_has_buttons():
    existing = MagicMock()  # e.g. an approval/inject card's keyboard
    assert await _send(LONG, flag=True, reply_markup=existing) is existing  # unchanged


# --------------------------- listen playback callback --------------------------- #
def _query(text="Here's where things stand on the U Bank deal, in detail..."):
    q = MagicMock()
    q.message.chat_id = 123
    q.message.text = text
    return q


async def test_listen_callback_tts_and_send_audio_and_logs():
    el = MagicMock()
    el.tts_available.return_value = True
    el.text_to_speech = AsyncMock(return_value=b"mp3-bytes")
    app = MagicMock()
    app.bot.send_audio = AsyncMock()
    db = MagicMock()
    db.log_action = MagicMock()
    with patch("services.elevenlabs_client.elevenlabs_client", el), patch(
        "services.supabase_client.supabase_client", db
    ), patch.object(telegram_bot, "_app", app), patch.object(
        telegram_bot, "send_message", AsyncMock()
    ):
        await telegram_bot._handle_listen_callback(_query())

    el.text_to_speech.assert_awaited_once()
    app.bot.send_audio.assert_awaited_once()
    assert app.bot.send_audio.call_args.kwargs["audio"] == b"mp3-bytes"
    db.log_action.assert_called_once()
    assert db.log_action.call_args.kwargs["action"] == "voice_tts"


async def test_listen_callback_reads_full_long_message_not_truncated():
    """A long message (well past the old 600 cap) is spoken in full (up to ~4800)."""
    long_text = "Sentence number {}. ".format  # build ~1500 chars
    body = "".join(long_text(i) for i in range(75))  # ~1500 chars
    el = MagicMock()
    el.tts_available.return_value = True
    el.text_to_speech = AsyncMock(return_value=b"mp3")
    app = MagicMock()
    app.bot.send_audio = AsyncMock()
    with patch("services.elevenlabs_client.elevenlabs_client", el), patch(
        "services.supabase_client.supabase_client", MagicMock()
    ), patch.object(telegram_bot, "_app", app), patch.object(
        telegram_bot, "send_message", AsyncMock()
    ):
        await telegram_bot._handle_listen_callback(_query(text=body))
    spoken = el.text_to_speech.call_args.args[0]
    assert len(spoken) == len(body.strip()) > 600  # full message (stripped), not the old 600 cap


async def test_listen_callback_empty_text_no_tts():
    el = MagicMock()
    el.tts_available.return_value = True
    el.text_to_speech = AsyncMock()
    with patch("services.elevenlabs_client.elevenlabs_client", el), patch(
        "services.supabase_client.supabase_client", MagicMock()
    ), patch.object(telegram_bot, "send_message", AsyncMock()) as sm:
        await telegram_bot._handle_listen_callback(_query(text="   "))
    el.text_to_speech.assert_not_awaited()
    assert any("nothing to read" in str(c.args).lower() for c in sm.await_args_list)


async def test_listen_callback_tts_unavailable():
    el = MagicMock()
    el.tts_available.return_value = False
    el.text_to_speech = AsyncMock()
    with patch("services.elevenlabs_client.elevenlabs_client", el), patch(
        "services.supabase_client.supabase_client", MagicMock()
    ), patch.object(telegram_bot, "send_message", AsyncMock()):
        await telegram_bot._handle_listen_callback(_query())
    el.text_to_speech.assert_not_awaited()


async def test_listen_callback_tts_failure_no_audio_no_log():
    el = MagicMock()
    el.tts_available.return_value = True
    el.text_to_speech = AsyncMock(return_value=None)  # TTS failed
    app = MagicMock()
    app.bot.send_audio = AsyncMock()
    db = MagicMock()
    db.log_action = MagicMock()
    with patch("services.elevenlabs_client.elevenlabs_client", el), patch(
        "services.supabase_client.supabase_client", db
    ), patch.object(telegram_bot, "_app", app), patch.object(
        telegram_bot, "send_message", AsyncMock()
    ):
        await telegram_bot._handle_listen_callback(_query())
    app.bot.send_audio.assert_not_awaited()
    db.log_action.assert_not_called()
