"""
Tests for the weekly cluster (v2.5 Phase 3, chunk 4):
the deterministic Eyal "Pulse" report, the tier-filtered team package (the
leak surface — one test per content type), the restart-safe scheduler, the
old-push suppression guards, and the Telegram callbacks/reply + TTS strip.

Patches MODULE-level attrs (never the global settings object).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import processors.weekly_pulse as pp
import processors.weekly_team_package as tp


# =========================================================================
# weekly_pulse — helpers + format (deterministic)
# =========================================================================

class TestPulseHelpers:
    def test_area_health_emoji(self):
        assert pp._area_health_emoji(["active", "blocked"]) == "\U0001f534"      # red
        assert pp._area_health_emoji(["active", "pending_decision"]) == "\U0001f7e1"  # yellow
        assert pp._area_health_emoji(["active", "active"]) == "\U0001f7e2"       # green
        assert pp._area_health_emoji([]) == "⚪"                              # white (no signal)

    def test_attention_detail_prefers_question_then_risk(self):
        assert pp._attention_detail({"open_items": [{"kind": "question", "description": "pick vendor"}]}) == "pick vendor"
        assert pp._attention_detail({"risks": ["waiting on X"]}) == "waiting on X"
        assert pp._attention_detail({"narrative": "some prose"}) == "some prose"

    def test_strategic_line_fallback(self):
        assert pp._strategic_line({"strategic_state": "MVP push"}) == "MVP push"
        assert pp._strategic_line({"narrative": "n"}) == "n"
        assert pp._strategic_line({}) == "—"


class TestFormatPulseText:
    def _data(self):
        areas = [
            {"emoji": "\U0001f7e2", "name": "Product", "strategic_state": "MVP push"},
            {"emoji": "\U0001f534", "name": "Fundraising", "strategic_state": "deck blocked"},
        ]
        attention = {
            "blocked": [{"name": "ML accuracy", "detail": "waiting on data"}],
            "pending_decision": [{"name": "Pricing", "detail": "pick a model"}],
            "stale_count": 67,
        }
        return areas, attention

    def test_all_sections_present(self):
        areas, attention = self._data()
        text = pp.format_pulse_text("2026-05-25", {"meetings": 12, "decisions": 8}, areas, attention, ["Moldova"])
        assert pp.PULSE_REPLY_MARKER in text          # header marker
        assert "12 meetings" in text and "8 decisions" in text
        assert "Product" in text and "Fundraising" in text          # all areas walked
        assert "NEEDS YOUR CALL" in text
        assert "ML accuracy" in text and "Pricing" in text
        assert "Reply to flag any of these for next review." in text
        assert "MOVED THIS WEEK" in text and "Moldova" in text
        assert "67 topics quiet 30+ days" in text                   # one-line housekeeping

    def test_quiet_week_and_no_attention(self):
        text = pp.format_pulse_text(
            "2026-05-25", {"meetings": 0, "decisions": 0}, [], {"blocked": [], "pending_decision": [], "stale_count": 0}, []
        )
        assert "Quiet week — no topics moved." in text
        assert "NEEDS YOUR CALL" not in text   # omitted when nothing to call


class TestPulseGather:
    def test_classify_attention_topics(self):
        rows = [
            {"topic_name": "A", "brief_json": {"current_status": "blocked", "risks": ["waiting on X"]}},
            {"topic_name": "B", "brief_json": {"current_status": "pending_decision",
                                               "open_items": [{"kind": "question", "description": "pick vendor"}]}},
            {"topic_name": "C", "brief_json": {"current_status": "stale"}},
            {"topic_name": "D", "brief_json": {"current_status": "active"}},
        ]
        mock_sc = MagicMock()
        mock_sc.client.table.return_value.select.return_value.not_.is_.return_value.execute.return_value.data = rows
        with patch.object(pp, "supabase_client", mock_sc):
            res = pp.classify_attention_topics()
        assert [t["name"] for t in res["blocked"]] == ["A"]
        assert res["blocked"][0]["detail"] == "waiting on X"
        assert [t["name"] for t in res["pending_decision"]] == ["B"]
        assert res["pending_decision"][0]["detail"] == "pick vendor"
        assert res["stale_count"] == 1

    def test_fetch_areas_with_health(self):
        mock_sc = MagicMock()
        mock_sc.get_areas.return_value = [
            {"id": "a1", "name": "Product", "brief_json": {"strategic_state": "MVP push", "sensitivity": "founders"}},
        ]
        mock_sc.client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"topic_name": "T1", "brief_json": {"current_status": "blocked", "sensitivity": "team"}},
        ]
        with patch.object(pp, "supabase_client", mock_sc):
            out = pp.fetch_areas_with_health()
        assert out[0]["emoji"] == "\U0001f534"            # blocked child → red
        assert out[0]["strategic_state"] == "MVP push"

    async def test_assemble_pulse_calls_no_llm(self):
        with patch.object(pp, "fetch_areas_with_health", return_value=[]), \
             patch.object(pp, "classify_attention_topics", return_value={"blocked": [], "pending_decision": [], "stale_count": 0}), \
             patch.object(pp, "fetch_moved_this_week", return_value=[]), \
             patch.object(pp, "recap_counts", AsyncMock(return_value={"meetings": 1, "decisions": 2})), \
             patch("core.llm.call_llm", side_effect=AssertionError("the Pulse must not call the LLM")):
            out = await pp.assemble_pulse(datetime(2026, 5, 25))
        assert pp.PULSE_REPLY_MARKER in out["text"]
        assert out["week_of"] == "2026-05-25"


# =========================================================================
# weekly_team_package — the leak surface (one test per content type)
# =========================================================================

class TestTeamPackage:
    def test_team_recipients_excludes_eyal(self):
        with patch.object(tp, "settings", SimpleNamespace(ROYE_EMAIL="r@x", PAOLO_EMAIL="p@x", YORAM_EMAIL="y@x")):
            assert tp._team_recipients() == ["r@x", "p@x", "y@x"]

    def test_area_team_lines_ceo_degradation(self):
        areas = [
            {"name": "Product", "strategic_state": "MVP", "brief": {"sensitivity": "founders"}, "children": []},
            {"name": "Fundraising", "strategic_state": "secret narrative", "brief": {"sensitivity": "ceo"},
             "children": [{"name": "Deck", "brief": {"sensitivity": "team"}},
                          {"name": "Investor terms", "brief": {"sensitivity": "ceo"}}]},
            {"name": "Legal", "strategic_state": "x", "brief": {"sensitivity": "ceo"},
             "children": [{"name": "NDA", "brief": {"sensitivity": "ceo"}}]},
        ]
        lines = tp._area_team_lines(areas)
        joined = " || ".join(lines)
        assert "Product" in joined and "MVP" in joined                 # founders area → headline
        assert "Fundraising" in joined and "Deck" in joined            # CEO area → safe child only
        assert "secret narrative" not in joined                        # contaminated narrative dropped
        assert "Investor terms" not in joined                          # CEO child dropped
        assert "Legal" not in joined                                   # all-CEO area omitted → variable length

    def test_signal_section_gates(self):
        fresh = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        assert tp._signal_section({"status": "distributed", "drive_doc_url": "u", "week_number": 21, "created_at": fresh})[0] is True
        assert tp._signal_section({"status": "pending_approval", "drive_doc_url": "u", "created_at": fresh})[0] is False
        assert tp._signal_section({"status": "distributed", "drive_doc_url": "u", "created_at": old})[0] is False
        assert tp._signal_section({"status": "distributed", "drive_doc_url": "", "created_at": fresh})[0] is False
        assert tp._signal_section(None)[0] is False

    async def _build(self, signal=None, areas=None):
        areas = areas if areas is not None else [
            {"name": "Product", "strategic_state": "MVP", "brief": {"sensitivity": "founders"}, "children": []}
        ]
        with patch("processors.weekly_digest.get_meetings_for_week", AsyncMock(return_value=[{}, {}, {}])), \
             patch("processors.weekly_digest.get_decisions_for_week",
                   AsyncMock(return_value=[{"description": "Pub", "sensitivity": "founders"},
                                           {"description": "Secret", "sensitivity": "ceo"}])), \
             patch("processors.weekly_digest.get_task_summary",
                   AsyncMock(return_value={"completed_this_week": [{"title": "Done1", "sensitivity": "team"},
                                                                    {"title": "CEOtask", "sensitivity": "ceo"}],
                                           "overdue": []})), \
             patch.object(tp, "fetch_areas_with_health", return_value=areas), \
             patch.object(tp, "supabase_client", MagicMock(get_latest_intelligence_signal=MagicMock(return_value=signal))), \
             patch.object(tp, "settings", SimpleNamespace(ROYE_EMAIL="r@x", PAOLO_EMAIL="p@x", YORAM_EMAIL="y@x", GANTT_SHEET_ID="SHEET")):
            return await tp.build_team_package(datetime(2026, 5, 25))

    async def test_ceo_decision_and_task_filtered_meetings_all(self):
        pkg = await self._build()
        body = pkg["html_body"]
        assert "Pub" in body and "Secret" not in body          # CEO decision stripped
        assert "3 meetings" in body                             # meeting COUNT counts all
        assert "1 decisions" in body                            # only founders decision counted
        assert "1 tasks done" in body                           # CEO task filtered out of count

    async def test_sections_3_and_4_absent_and_gantt_present(self):
        pkg = await self._build()
        assert "NEEDS YOUR CALL" not in pkg["html_body"]
        assert "MOVED THIS WEEK" not in pkg["html_body"]
        assert "docs.google.com/spreadsheets/d/SHEET" in pkg["html_body"]

    async def test_signal_included_when_distributed_fresh_else_omitted(self):
        fresh = datetime.now(timezone.utc).isoformat()
        sig = {"status": "distributed", "drive_doc_url": "http://doc", "week_number": 21, "created_at": fresh}
        pkg = await self._build(signal=sig)
        assert "Intelligence Signal (Week 21)" in " ".join(pkg["contents"])
        assert "http://doc" in pkg["html_body"]
        pkg2 = await self._build(signal={"status": "pending_approval", "drive_doc_url": "http://doc", "created_at": fresh})
        assert not any("Intelligence Signal" in c for c in pkg2["contents"])

    async def test_founders_topic_naming_investor_is_accepted(self):
        # We accept what the classifier accepts: a founders-tier area headline that
        # happens to name an investor still goes to the team copy.
        areas = [{"name": "Fundraising", "strategic_state": "Sequoia intro progressing",
                  "brief": {"sensitivity": "founders"}, "children": []}]
        pkg = await self._build(areas=areas)
        assert "Sequoia" in pkg["html_body"]

    async def test_confirm_contents_match_email_when_signal_absent(self):
        # team_package_contents derives from the same build → no drift.
        with patch.object(tp, "build_team_package",
                          AsyncMock(return_value={"contents": ["recap", "area status", "Gantt link"]})):
            contents = await tp.team_package_contents(datetime(2026, 5, 25))
        assert "recap" in contents and not any("Intelligence Signal" in c for c in contents)

    async def test_send_team_package_recipients(self):
        with patch.object(tp, "build_team_package",
                          AsyncMock(return_value={"subject": "s", "body": "b", "html_body": "h",
                                                  "contents": ["recap"], "recipients": ["r@x", "p@x", "y@x"]})), \
             patch("services.gmail.gmail_service.send_email", AsyncMock(return_value=True)) as send, \
             patch.object(tp, "supabase_client", MagicMock()):
            ok = await tp.send_team_package(datetime(2026, 5, 25))
        assert ok is True
        send.assert_awaited_once()
        assert send.call_args.kwargs["to"] == ["r@x", "p@x", "y@x"]


# =========================================================================
# scheduler — window / fire-once / reconstruct / send markup
# =========================================================================

import schedulers.weekly_pulse_scheduler as wmod


def _fixed_now(dt):
    class _FN:
        @staticmethod
        def now(tz=None):
            return dt
    return _FN


_SETTINGS = SimpleNamespace(
    WEEKLY_DIGEST_DAY=4, WEEKLY_PULSE_HOUR=15, WEEKLY_PULSE_WINDOW_HOURS=2, WEEKLY_PULSE_CHECK_INTERVAL=3600,
)


class TestPulseScheduler:
    async def test_fires_in_window(self):
        sched = wmod.WeeklyPulseScheduler()
        friday = datetime(2026, 5, 29, 15, 30, tzinfo=wmod._ISRAEL_TZ)
        with patch.object(wmod, "datetime", _fixed_now(friday)), \
             patch.object(wmod, "settings", _SETTINGS), \
             patch.object(sched, "_send_pulse", AsyncMock()) as sp:
            fired = await sched._check_and_send()
        assert fired is True
        sp.assert_awaited_once()

    async def test_skips_outside_hour_and_day(self):
        sched = wmod.WeeklyPulseScheduler()
        with patch.object(wmod, "settings", _SETTINGS), patch.object(sched, "_send_pulse", AsyncMock()) as sp:
            with patch.object(wmod, "datetime", _fixed_now(datetime(2026, 5, 29, 18, 30, tzinfo=wmod._ISRAEL_TZ))):
                assert await sched._check_and_send() is False   # past the window
            with patch.object(wmod, "datetime", _fixed_now(datetime(2026, 5, 28, 15, 30, tzinfo=wmod._ISRAEL_TZ))):
                assert await sched._check_and_send() is False   # Thursday
        sp.assert_not_awaited()

    async def test_fire_once_per_week(self):
        sched = wmod.WeeklyPulseScheduler()
        friday = datetime(2026, 5, 29, 15, 30, tzinfo=wmod._ISRAEL_TZ)
        sched._sent_weeks.add(sched._week_key(friday))
        with patch.object(wmod, "datetime", _fixed_now(friday)), \
             patch.object(wmod, "settings", _SETTINGS), \
             patch.object(sched, "_send_pulse", AsyncMock()) as sp:
            assert await sched._check_and_send() is False
        sp.assert_not_awaited()

    async def test_reconstruct_sent_weeks(self):
        sched = wmod.WeeklyPulseScheduler()
        rows = [{"created_at": datetime.now(timezone.utc).isoformat(),
                 "details": {"week_key": "weekly_pulse:2026-W22"}}]
        with patch.object(wmod.supabase_client, "get_audit_log", MagicMock(return_value=rows)):
            await sched.reconstruct_sent_weeks()
        assert "weekly_pulse:2026-W22" in sched._sent_weeks

    async def test_send_pulse_logs_before_send_and_has_buttons(self):
        sched = wmod.WeeklyPulseScheduler()
        order = []

        async def _send(*a, **k):
            order.append("send")
            return True

        with patch.object(wmod, "assemble_pulse",
                          AsyncMock(return_value={"week_of": "2026-05-25",
                                                  "text": f"{pp.PULSE_REPLY_MARKER} 2026-05-25", "stale_count": 5})), \
             patch.object(wmod.comms_spine, "send_to_eyal", side_effect=_send) as send, \
             patch.object(wmod.supabase_client, "log_action", MagicMock(side_effect=lambda **k: order.append("log"))):
            await sched._send_pulse(datetime(2026, 5, 25), "weekly_pulse:2026-W22")
        assert order and order[0] == "log" and "send" in order      # audit before send
        kb = send.call_args.kwargs["reply_markup"].inline_keyboard
        assert kb[0][0].callback_data.startswith("weekly_pkg:")
        assert kb[1][0].callback_data == "listen:1"
        assert "weekly_pulse:2026-W22" in sched._sent_weeks


# =========================================================================
# suppression guards
# =========================================================================

class TestSuppression:
    async def test_digest_autopush_suppressed_when_pulse_on(self):
        import schedulers.weekly_digest_scheduler as dmod
        d = dmod.WeeklyDigestScheduler()
        with patch.object(dmod, "settings", SimpleNamespace(
                WEEKLY_PULSE_ENABLED=True, WEEKLY_DIGEST_DAY=4, WEEKLY_DIGEST_HOUR=14,
                WEEKLY_DIGEST_WINDOW_HOURS=2, WEEKLY_DIGEST_CHECK_INTERVAL=3600)), \
             patch.object(d, "_generate_and_distribute", AsyncMock()) as gd:
            await d._check_and_generate()
        gd.assert_not_awaited()

    async def test_digest_autopush_proceeds_when_pulse_off(self):
        import schedulers.weekly_digest_scheduler as dmod
        d = dmod.WeeklyDigestScheduler()
        d._last_digest_week = None
        friday = datetime(2026, 5, 29, 14, 30, tzinfo=dmod._ISRAEL_TZ)
        with patch.object(dmod, "settings", SimpleNamespace(
                WEEKLY_PULSE_ENABLED=False, WEEKLY_DIGEST_DAY=4, WEEKLY_DIGEST_HOUR=14,
                WEEKLY_DIGEST_WINDOW_HOURS=2, WEEKLY_DIGEST_CHECK_INTERVAL=3600)), \
             patch.object(dmod, "datetime", _fixed_now(friday)), \
             patch.object(dmod.supabase_client, "get_active_weekly_review_session", MagicMock(return_value=None)), \
             patch.object(d, "_generate_and_distribute", AsyncMock()) as gd:
            await d._check_and_generate()
        gd.assert_awaited_once()

    async def test_review_session_nudge_suppressed_when_pulse_on(self):
        import schedulers.weekly_review_scheduler as rmod
        r = rmod.WeeklyReviewScheduler()
        with patch.object(rmod, "settings", SimpleNamespace(WEEKLY_PULSE_ENABLED=True, WEEKLY_REVIEW_SCHEDULER_INTERVAL=60)), \
             patch.object(r, "_find_review_event", AsyncMock()) as fre:
            await r._send_notification("evt")
        fre.assert_not_awaited()


# =========================================================================
# Telegram callbacks + pulse reply + TTS strip
# =========================================================================

class TestTelegram:
    def _bot(self):
        from services.telegram_bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.eyal_chat_id = "999"
        bot.send_message = AsyncMock()
        return bot

    async def test_weekly_pkg_eyal_gate(self):
        bot = self._bot()
        q = MagicMock()
        q.message.chat_id = 111  # not Eyal
        await bot._handle_weekly_pkg_callback(q, "2026-05-25")
        bot.send_message.assert_not_awaited()

    async def test_weekly_pkg_shows_confirm(self):
        bot = self._bot()
        q = MagicMock()
        q.message.chat_id = 999
        with patch("processors.weekly_team_package.team_package_contents",
                   AsyncMock(return_value=["recap", "area status", "Gantt link"])):
            await bot._handle_weekly_pkg_callback(q, "2026-05-25")
        bot.send_message.assert_awaited_once()
        assert "Roye, Paolo, Yoram" in bot.send_message.call_args.args[1]

    async def test_weekly_pkg_confirm_sends_cancel_does_not(self):
        bot = self._bot()
        q = MagicMock()
        q.message.chat_id = 999
        q.edit_message_text = AsyncMock()
        with patch("processors.weekly_team_package.send_team_package", AsyncMock(return_value=True)) as snd:
            await bot._handle_weekly_pkg_confirm(q, "2026-05-25")
        snd.assert_awaited_once()
        with patch("processors.weekly_team_package.send_team_package", AsyncMock()) as snd2:
            await bot._handle_weekly_pkg_cancel(MagicMock(edit_message_text=AsyncMock()))
        snd2.assert_not_awaited()

    async def test_pulse_reply_scoped_to_marker(self):
        bot = self._bot()
        upd = MagicMock()
        upd.message.reply_to_message.text = f"\U0001f4ca {pp.PULSE_REPLY_MARKER} 2026-05-25\n…"
        upd.message.text = "close the Lavazza pricing decision"
        upd.message.chat_id = 999
        with patch("services.supabase_client.supabase_client.log_action") as la:
            handled = await bot._handle_pulse_reply(upd)
        assert handled is True
        la.assert_called_once()
        bot.send_message.assert_awaited_once()

    async def test_non_pulse_reply_passes_through(self):
        bot = self._bot()
        upd = MagicMock()
        upd.message.reply_to_message.text = "Reminder: task X is due"
        upd.message.text = "done"
        assert await bot._handle_pulse_reply(upd) is False

    def test_strip_for_tts_removes_emoji_and_tags(self):
        from services.telegram_bot import _strip_for_tts
        out = _strip_for_tts("\U0001f514 <b>NEEDS</b> YOUR CALL \U0001f7e2")
        assert "NEEDS YOUR CALL" in out
        assert "<b>" not in out
        assert "\U0001f514" not in out and "\U0001f7e2" not in out
