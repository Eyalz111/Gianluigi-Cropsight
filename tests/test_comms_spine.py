"""Tests for the orchestration spine (PR A — comms/voice beat #1).

PATCH-TARGET MAP (read before adding tests):
  * The spine lazily does ``from services.telegram_bot import telegram_bot`` /
    ``from core.agent import gianluigi_agent`` / ``from services.elevenlabs_client
    import elevenlabs_client`` *inside* its methods. So the spine's OWN tests patch
    those singletons in their HOME modules:
        patch("services.telegram_bot.telegram_bot", ...)
        patch("core.agent.gianluigi_agent", ...)
        patch("services.elevenlabs_client.elevenlabs_client", ...)
  * Migrated CALL-SITE tests (PR B) instead patch ``<caller_module>.comms_spine``.
  Don't confuse the two — that's the whole point of routing through the spine.
"""
import inspect

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.events import Channel, InboundEvent, Modality
from services.orchestrator.spine import CommsSpine, comms_spine

# The 8 outbound methods that must forward verbatim to telegram_bot.
FACADE_METHODS = [
    "send_message",
    "send_to_eyal",
    "send_to_group",
    "send_meeting_summary",
    "send_approval_request",
    "send_prep_outline",
    "send_stakeholder_approval_request",
    "send_task_reminder",
]

# Representative args/kwargs per method — kwargs include the ones production
# passes but the suite might not otherwise exercise (e.g. parse_mode).
FORWARD_CASES = [
    ("send_message", ("123", "hi"), {"parse_mode": "HTML", "reply_markup": "kb"}),
    ("send_to_eyal", ("hi",), {"parse_mode": "HTML", "reply_markup": "kb"}),
    ("send_to_group", ("teaser",), {"parse_mode": "HTML"}),
    ("send_meeting_summary", ("Title", "summary", "link"), {"sensitive": True}),
    ("send_approval_request", ("Title", "preview", "mid"), {"tasks": [{"x": 1}]}),
    ("send_prep_outline", ({"o": 1}, "approval-id"), {"confidence": "ask"}),
    ("send_stakeholder_approval_request", ("Name", "Org", {"k": "v"}), {"is_new": False}),
    ("send_task_reminder", ("chat-9", "desc", "tomorrow"), {"overdue": True}),
]


def _mock_bot():
    bot = MagicMock()
    for name in FACADE_METHODS:
        setattr(bot, name, AsyncMock(return_value=True))
    bot.app = MagicMock()
    bot.app.bot = MagicMock()
    bot.app.bot.send_message = AsyncMock(return_value="sent")
    return bot


@pytest.mark.parametrize("method,args,kwargs", FORWARD_CASES)
async def test_facade_forwards_verbatim(method, args, kwargs):
    bot = _mock_bot()
    with patch("services.telegram_bot.telegram_bot", bot):
        result = await getattr(comms_spine, method)(*args, **kwargs)
    getattr(bot, method).assert_awaited_once_with(*args, **kwargs)
    assert result is True


async def test_send_raw_forwards_to_app_bot():
    bot = _mock_bot()
    with patch("services.telegram_bot.telegram_bot", bot):
        result = await comms_spine.send_raw(chat_id="999", text="ping")
    bot.app.bot.send_message.assert_awaited_once_with(chat_id="999", text="ping")
    assert result == "sent"


@pytest.mark.parametrize("method", FACADE_METHODS)
def test_facade_cannot_drop_kwargs(method):
    """Signature contract (review pt 1): each passthrough takes *args + **kwargs,
    so no kwarg production passes can be silently dropped — and the underlying
    bot method actually exists."""
    from services.telegram_bot import telegram_bot

    params = inspect.signature(getattr(comms_spine, method)).parameters.values()
    kinds = {p.kind for p in params}
    assert inspect.Parameter.VAR_KEYWORD in kinds, f"{method} must accept **kwargs"
    assert inspect.Parameter.VAR_POSITIONAL in kinds, f"{method} must accept *args"
    assert callable(getattr(telegram_bot, method)), f"telegram_bot.{method} missing"


async def test_handle_inbound_text_dispatches_to_brain():
    fake = {"action": "none", "response": "ok"}
    agent = MagicMock()
    agent.process_message = AsyncMock(return_value=fake)
    history = [{"role": "user", "content": "earlier"}]
    with patch("core.agent.gianluigi_agent", agent):
        event = InboundEvent(
            channel=Channel.TELEGRAM,
            modality=Modality.TEXT,
            sender_id="eyal",
            chat_id="123",
            text="what's overdue?",
            conversation_history=history,
        )
        result = await comms_spine.handle_inbound(event)
    assert result == fake
    agent.process_message.assert_awaited_once_with(
        user_message="what's overdue?", user_id="eyal", conversation_history=history,
        allow_writes=True, max_sensitivity_level=4
    )


async def test_handle_inbound_voice_transcribes_then_dispatches():
    eleven = MagicMock()
    eleven.speech_to_text = AsyncMock(return_value="ship the demo by friday")
    agent = MagicMock()
    agent.process_message = AsyncMock(return_value={"action": "quick_injection_confirm"})
    with patch("services.elevenlabs_client.elevenlabs_client", eleven), patch(
        "core.agent.gianluigi_agent", agent
    ):
        event = InboundEvent(
            channel=Channel.TELEGRAM,
            modality=Modality.VOICE,
            sender_id="eyal",
            chat_id="123",
            audio_bytes=b"ogg-bytes",
            audio_mime="audio/ogg",
        )
        result = await comms_spine.handle_inbound(event)
    eleven.speech_to_text.assert_awaited_once_with(b"ogg-bytes", mime_type="audio/ogg")
    agent.process_message.assert_awaited_once()
    assert event.text == "ship the demo by friday"
    assert event.raw_transcript == "ship the demo by friday"
    assert result["action"] == "quick_injection_confirm"


@pytest.mark.parametrize("transcript", [None, "", "uh", "ok", "hello"])
async def test_handle_inbound_voice_subfloor_is_stt_failed(transcript):
    """Sub-floor / empty transcript (review pt 6) -> stt_failed; brain never called."""
    eleven = MagicMock()
    eleven.speech_to_text = AsyncMock(return_value=transcript)
    agent = MagicMock()
    agent.process_message = AsyncMock()
    with patch("services.elevenlabs_client.elevenlabs_client", eleven), patch(
        "core.agent.gianluigi_agent", agent
    ):
        event = InboundEvent(
            channel=Channel.TELEGRAM,
            modality=Modality.VOICE,
            sender_id="eyal",
            chat_id="123",
            audio_bytes=b"noise",
        )
        result = await comms_spine.handle_inbound(event)
    assert result["action"] == "stt_failed"
    agent.process_message.assert_not_awaited()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ship the demo by friday", True),
        ("call paolo", True),
        ("uh", False),
        ("ok", False),
        ("hello", False),  # >=5 chars but only 1 word
        ("   ", False),
        ("", False),
        (None, False),
    ],
)
def test_usable_transcript_floor(value, expected):
    assert CommsSpine._usable_transcript(value) is expected


async def test_send_routes_telegram_text():
    bot = _mock_bot()
    bot.send_message = AsyncMock(return_value=True)
    with patch("services.telegram_bot.telegram_bot", bot):
        ok = await comms_spine.send(to="123", text="hi", parse_mode="HTML")
    bot.send_message.assert_awaited_once_with(
        "123", "hi", parse_mode="HTML", reply_markup=None
    )
    assert ok is True


async def test_send_raises_for_unimplemented_channel():
    with pytest.raises(NotImplementedError):
        await comms_spine.send(to="123", text="hi", channel="whatsapp")


async def test_send_raises_for_voice_out_modality():
    with pytest.raises(NotImplementedError):
        await comms_spine.send(to="123", text="hi", modality=Modality.VOICE)


# ---------------------------------------------------------------------------
# PR C — the inbound-text routing seam in TelegramBot._route_inbound_text.
# ---------------------------------------------------------------------------
async def test_route_inbound_text_uses_spine_when_flag_on():
    from services.telegram_bot import telegram_bot
    from config.settings import settings

    fake = {"action": "none", "response": "ok"}
    spine = MagicMock()
    spine.handle_inbound = AsyncMock(return_value=fake)
    history = [{"role": "user", "content": "earlier"}]

    with patch.object(settings, "ORCHESTRATION_SPINE_ENABLED", True), patch(
        "services.orchestrator.spine.comms_spine", spine
    ):
        result = await telegram_bot._route_inbound_text(
            message_text="what's overdue?",
            user_id="eyal",
            history=history,
            chat_id="123",
            message_id="42",
        )

    assert result == fake
    spine.handle_inbound.assert_awaited_once()
    event = spine.handle_inbound.call_args[0][0]
    assert event.channel == Channel.TELEGRAM
    assert event.modality == Modality.TEXT
    assert event.text == "what's overdue?"
    assert event.sender_id == "eyal"
    assert event.chat_id == "123"
    assert event.message_id == "42"
    assert event.conversation_history == history


async def test_route_inbound_text_uses_agent_when_flag_off():
    from services.telegram_bot import telegram_bot
    from config.settings import settings

    fake = {"action": "none", "response": "ok"}
    agent = MagicMock()
    agent.process_message = AsyncMock(return_value=fake)
    history = [{"role": "user", "content": "earlier"}]

    with patch.object(settings, "ORCHESTRATION_SPINE_ENABLED", False), patch(
        "core.agent.gianluigi_agent", agent
    ):
        result = await telegram_bot._route_inbound_text(
            message_text="what's overdue?",
            user_id="eyal",
            history=history,
            chat_id="123",
            message_id="42",
        )

    assert result == fake
    agent.process_message.assert_awaited_once_with(
        user_message="what's overdue?", user_id="eyal", conversation_history=history,
        allow_writes=True, max_sensitivity_level=4
    )
