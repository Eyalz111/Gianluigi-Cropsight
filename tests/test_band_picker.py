"""Approval-card distribution band picker (CEO/Founders/Company).

The picker sets the meeting's sensitivity directly (CEO->ceo, Founders->founders,
Company->team) via callback_data 'sens_set:{band}:{meeting_id}', marking the
current band. [distribution-groups 2026-07-05]
"""

from services.telegram_bot import TelegramBot


def _bare_bot():
    # Skip __init__ (needs a bot token) — the picker helpers only touch class attrs.
    return TelegramBot.__new__(TelegramBot)


def test_band_row_callbacks_and_marks_current_founders():
    kb = _bare_bot()._build_approval_keyboard("m123", "founders")
    band_row = kb.inline_keyboard[-1]
    assert [b.callback_data for b in band_row] == [
        "sens_set:ceo:m123",
        "sens_set:founders:m123",
        "sens_set:company:m123",
    ]
    assert [b.text for b in band_row] == ["CEO-only", "● Founders", "Company"]


def test_team_sensitivity_marks_company():
    kb = _bare_bot()._build_approval_keyboard("m1", "team")
    assert [b.text for b in kb.inline_keyboard[-1]] == ["CEO-only", "Founders", "● Company"]


def test_ceo_sensitivity_marks_ceo():
    kb = _bare_bot()._build_approval_keyboard("m1", "ceo")
    assert [b.text for b in kb.inline_keyboard[-1]] == ["● CEO-only", "Founders", "Company"]


def test_approve_edit_reject_rows_intact():
    kb = _bare_bot()._build_approval_keyboard("m9", "founders")
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["approve:m9", "edit:m9"]
    assert [b.callback_data for b in kb.inline_keyboard[1]] == ["reject:m9"]


def test_sensitivity_for_band_mapping():
    assert TelegramBot._sensitivity_for_band("company") == "team"
    assert TelegramBot._sensitivity_for_band("founders") == "founders"
    assert TelegramBot._sensitivity_for_band("ceo") == "ceo"
    assert TelegramBot._sensitivity_for_band("garbage") == "founders"  # safe default
