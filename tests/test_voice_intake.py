"""Tests for Telegram voice-note intake (comms/voice beat #1, PR E).

Exercises TelegramBot._transcribe_and_route, _handle_voice (caps/guards), and the
soft-cap confirm callback — patching the telegram_bot singleton's collaborators so no
real Telegram/STT calls happen.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.telegram_bot import telegram_bot

EYAL = "8190904141"


@pytest.fixture(autouse=True)
def _stub_audit_log():
    """Stub supabase_client so voice_stt telemetry never hits the real DB in tests."""
    with patch("services.supabase_client.supabase_client") as db:
        db.log_action = MagicMock(return_value={"id": "log-test"})
        db.get_active_debrief_session = MagicMock(return_value=None)
        yield db


def _voice_update(duration, file_size, user_id=EYAL, msg_id=5, file_id="vf"):
    voice = MagicMock()
    voice.duration = duration
    voice.file_size = file_size
    voice.file_id = file_id
    msg = MagicMock()
    msg.voice = voice
    msg.message_id = msg_id
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = user_id
    update.message = msg
    return update


def _file_returning(audio: bytes):
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(audio))
    app = MagicMock()
    app.bot.get_file = AsyncMock(return_value=tg_file)
    return app


# --------------------------------------------------------------------------- #
# _transcribe_and_route                                                        #
# --------------------------------------------------------------------------- #
async def test_transcribe_and_route_injection_shows_transcript():
    async def fake_handle_inbound(event):
        event.raw_transcript = "ship the demo by friday"
        return {"action": "quick_injection_confirm", "extracted_items": [{"type": "task", "title": "x"}]}

    spine = MagicMock()
    spine.handle_inbound = AsyncMock(side_effect=fake_handle_inbound)

    with patch.object(telegram_bot, "_app", _file_returning(b"ogg-bytes")), patch.object(
        telegram_bot, "send_message", AsyncMock()
    ), patch.object(
        telegram_bot, "_send_quick_injection_confirmation", AsyncMock()
    ) as confirm, patch("services.orchestrator.spine.comms_spine", spine):
        await telegram_bot._transcribe_and_route("123", "file-1", 42, "eyal")

    event = spine.handle_inbound.call_args[0][0]
    assert event.modality.value == "voice"
    assert event.audio_bytes == b"ogg-bytes"
    confirm.assert_awaited_once()
    assert confirm.call_args.kwargs["raw_transcript"] == "ship the demo by friday"
    assert confirm.call_args.kwargs["source_message_id"] == 42


async def test_transcribe_and_route_stt_failed_no_confirm():
    spine = MagicMock()
    spine.handle_inbound = AsyncMock(return_value={"action": "stt_failed", "response": "no good"})

    with patch.object(telegram_bot, "_app", _file_returning(b"x")), patch.object(
        telegram_bot, "send_message", AsyncMock()
    ) as sm, patch.object(
        telegram_bot, "_send_quick_injection_confirmation", AsyncMock()
    ) as confirm, patch("services.orchestrator.spine.comms_spine", spine):
        await telegram_bot._transcribe_and_route("123", "f", 1, "eyal")

    confirm.assert_not_awaited()
    assert any("no good" in str(c.args) for c in sm.await_args_list)


async def test_transcribe_and_route_download_failure_no_spine_call():
    app = MagicMock()
    app.bot.get_file = AsyncMock(side_effect=RuntimeError("boom"))
    spine = MagicMock()
    spine.handle_inbound = AsyncMock()

    with patch.object(telegram_bot, "_app", app), patch.object(
        telegram_bot, "send_message", AsyncMock()
    ) as sm, patch("services.orchestrator.spine.comms_spine", spine):
        await telegram_bot._transcribe_and_route("123", "f", 1, "eyal")

    spine.handle_inbound.assert_not_awaited()
    assert any("couldn't fetch" in str(c.args).lower() for c in sm.await_args_list)


async def test_transcribe_and_route_question_answered_with_heard_footer():
    async def fake(event):
        event.raw_transcript = "what's overdue?"
        return {"action": "none", "response": "Two tasks are overdue."}

    spine = MagicMock()
    spine.handle_inbound = AsyncMock(side_effect=fake)

    with patch.object(telegram_bot, "_app", _file_returning(b"x")), patch.object(
        telegram_bot, "send_message", AsyncMock()
    ) as sm, patch.object(
        telegram_bot, "_send_quick_injection_confirmation", AsyncMock()
    ) as confirm, patch("services.orchestrator.spine.comms_spine", spine):
        await telegram_bot._transcribe_and_route("123", "f", 1, "eyal")

    confirm.assert_not_awaited()
    sent = " ".join(str(c.args) for c in sm.await_args_list)
    assert "Two tasks are overdue." in sent
    assert "heard:" in sent


# --------------------------------------------------------------------------- #
# Soft-cap confirm callback (review note 1: cancel = no spend, no session)     #
# --------------------------------------------------------------------------- #
def _voicecap_update(data):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user = MagicMock()
    query.from_user.id = int(EYAL)
    query.message = MagicMock()
    query.message.chat_id = int(EYAL)
    update = MagicMock()
    update.callback_query = query
    return update, query


async def test_voicecap_cancel_no_spend_no_session():
    update, query = _voicecap_update("voicecap:no")
    context = MagicMock()
    context.user_data = {"pending_long_voice": {"file_id": "f", "message_id": 1}}

    with patch.object(telegram_bot, "_transcribe_and_route", AsyncMock()) as tr:
        await telegram_bot._handle_callback_query(update, context)

    tr.assert_not_awaited()  # no STT spend, no session created
    assert "pending_long_voice" not in context.user_data
    query.edit_message_text.assert_awaited()


async def test_voicecap_yes_transcribes():
    update, query = _voicecap_update("voicecap:yes")
    context = MagicMock()
    context.user_data = {"pending_long_voice": {"file_id": "f1", "message_id": 7}}

    with patch.object(telegram_bot, "_transcribe_and_route", AsyncMock()) as tr, patch.object(
        telegram_bot, "_get_user_id", MagicMock(return_value="eyal")
    ):
        await telegram_bot._handle_callback_query(update, context)

    tr.assert_awaited_once_with(int(EYAL), "f1", 7, "eyal", 0)


# --------------------------------------------------------------------------- #
# _handle_voice — caps and gating                                             #
# --------------------------------------------------------------------------- #
async def test_handle_voice_flag_off_replies_and_stops():
    update = _voice_update(duration=10, file_size=1000)
    context = MagicMock()
    context.user_data = {}
    with patch.object(telegram_bot, "eyal_chat_id", EYAL), patch(
        "services.elevenlabs_client.elevenlabs_client"
    ) as el, patch.object(telegram_bot, "send_message", AsyncMock()) as sm, patch.object(
        telegram_bot, "_transcribe_and_route", AsyncMock()
    ) as tr:
        el.stt_available.return_value = False
        await telegram_bot._handle_voice(update, context)
    tr.assert_not_awaited()
    assert any("voice intake is off" in str(c.args).lower() for c in sm.await_args_list)


async def test_handle_voice_hard_reject_oversize():
    update = _voice_update(duration=60, file_size=25 * 1024 * 1024)  # 25 MB > 20 MB
    context = MagicMock()
    context.user_data = {}
    with patch.object(telegram_bot, "eyal_chat_id", EYAL), patch(
        "services.elevenlabs_client.elevenlabs_client"
    ) as el, patch("services.supabase_client.supabase_client") as db, patch.object(
        telegram_bot, "_session_stack", []
    ), patch.object(telegram_bot, "send_message", AsyncMock()) as sm, patch.object(
        telegram_bot, "_transcribe_and_route", AsyncMock()
    ) as tr:
        el.stt_available.return_value = True
        db.get_active_debrief_session.return_value = None
        await telegram_bot._handle_voice(update, context)
    tr.assert_not_awaited()
    assert "pending_long_voice" not in context.user_data
    assert any("20 mb" in str(c.args).lower() for c in sm.await_args_list)


async def test_handle_voice_soft_cap_asks_confirm():
    update = _voice_update(duration=600, file_size=1000)  # 10 min -> over soft cap
    context = MagicMock()
    context.user_data = {}
    with patch.object(telegram_bot, "eyal_chat_id", EYAL), patch(
        "services.elevenlabs_client.elevenlabs_client"
    ) as el, patch("services.supabase_client.supabase_client") as db, patch.object(
        telegram_bot, "_session_stack", []
    ), patch.object(telegram_bot, "send_message", AsyncMock()) as sm, patch.object(
        telegram_bot, "_transcribe_and_route", AsyncMock()
    ) as tr:
        el.stt_available.return_value = True
        db.get_active_debrief_session.return_value = None
        await telegram_bot._handle_voice(update, context)
    tr.assert_not_awaited()  # confirm first, no transcription yet
    assert context.user_data.get("pending_long_voice", {}).get("file_id") == "vf"
    assert any("anyway" in str(c.args).lower() for c in sm.await_args_list)


async def test_handle_voice_normal_note_routes_to_transcribe():
    update = _voice_update(duration=12, file_size=1000)  # under caps
    context = MagicMock()
    context.user_data = {}
    with patch.object(telegram_bot, "eyal_chat_id", EYAL), patch(
        "services.elevenlabs_client.elevenlabs_client"
    ) as el, patch("services.supabase_client.supabase_client") as db, patch.object(
        telegram_bot, "_session_stack", []
    ), patch.object(telegram_bot, "send_message", AsyncMock()), patch.object(
        telegram_bot, "_get_user_id", MagicMock(return_value="eyal")
    ), patch.object(telegram_bot, "_transcribe_and_route", AsyncMock()) as tr:
        el.stt_available.return_value = True
        db.get_active_debrief_session.return_value = None
        await telegram_bot._handle_voice(update, context)
    tr.assert_awaited_once_with(EYAL, "vf", 5, "eyal", duration_s=12)


async def test_transcribe_and_route_logs_stt_telemetry(_stub_audit_log):
    async def fake(event):
        event.raw_transcript = "ship it"
        return {"action": "quick_injection_confirm", "extracted_items": []}

    spine = MagicMock()
    spine.handle_inbound = AsyncMock(side_effect=fake)
    with patch.object(telegram_bot, "_app", _file_returning(b"ogg")), patch.object(
        telegram_bot, "send_message", AsyncMock()
    ), patch.object(
        telegram_bot, "_send_quick_injection_confirmation", AsyncMock()
    ), patch("services.orchestrator.spine.comms_spine", spine):
        await telegram_bot._transcribe_and_route("123", "f", 9, "eyal", duration_s=42)

    _stub_audit_log.log_action.assert_called_once()
    kwargs = _stub_audit_log.log_action.call_args.kwargs
    assert kwargs["action"] == "voice_stt"
    assert kwargs["triggered_by"] == "eyal"
    d = kwargs["details"]
    assert d["duration_s"] == 42
    assert d["chars"] == len("ship it")
    assert d["audio_bytes"] == 3
    assert "latency_ms" in d
    assert d["est_cost_usd"] == round(42 / 3600 * 0.40, 5)
