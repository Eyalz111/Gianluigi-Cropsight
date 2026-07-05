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
    band_row = kb.inline_keyboard[-1][:3]  # 3 band buttons (Custom… is the 4th)
    assert [b.callback_data for b in band_row] == [
        "sens_set:ceo:m123",
        "sens_set:founders:m123",
        "sens_set:company:m123",
    ]
    assert [b.text for b in band_row] == ["CEO-only", "● Founders", "Company"]


def test_team_sensitivity_marks_company():
    kb = _bare_bot()._build_approval_keyboard("m1", "team")
    assert [b.text for b in kb.inline_keyboard[-1][:3]] == ["CEO-only", "Founders", "● Company"]


def test_ceo_sensitivity_marks_ceo():
    kb = _bare_bot()._build_approval_keyboard("m1", "ceo")
    assert [b.text for b in kb.inline_keyboard[-1][:3]] == ["● CEO-only", "Founders", "Company"]


def test_approve_edit_reject_rows_intact():
    kb = _bare_bot()._build_approval_keyboard("m9", "founders")
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["approve:m9", "edit:m9"]
    assert [b.callback_data for b in kb.inline_keyboard[1]] == ["reject:m9"]


def test_sensitivity_for_band_mapping():
    assert TelegramBot._sensitivity_for_band("company") == "team"
    assert TelegramBot._sensitivity_for_band("founders") == "founders"
    assert TelegramBot._sensitivity_for_band("ceo") == "ceo"
    assert TelegramBot._sensitivity_for_band("garbage") == "founders"  # safe default


def test_band_row_includes_custom_button():
    kb = _bare_bot()._build_approval_keyboard("m1", "founders")
    assert kb.inline_keyboard[-1][-1].callback_data == "dcust:m1"
    assert kb.inline_keyboard[-1][-1].text == "Custom…"


# ── Custom picker checklist ──────────────────────────────────────────────────
_ROSTER = {
    "eyal": {"name": "Eyal Zror", "tier": "ceo", "status": "active", "email": "e@x.com"},
    "matti": {"name": "Matti Sevitt", "tier": "founders", "status": "active", "email": "m@x.com"},
    "marco": {"name": "Marco Sutter", "tier": "team", "status": "active", "email": "mc@x.com"},
    "gone": {"name": "Gone", "tier": "team", "status": "inactive", "email": "g@x.com"},
}


def test_custom_checklist_marks_selected_and_skips_inactive(monkeypatch):
    import config.team
    monkeypatch.setattr(config.team, "TEAM_MEMBERS", _ROSTER, raising=False)
    kb = _bare_bot()._build_custom_keyboard("m9", selected={"marco"}, override=False)
    # Member toggle buttons = active members only (eyal, matti, marco), not 'gone'.
    toggles = [b for row in kb.inline_keyboard for b in row if b.callback_data.startswith("dtog:")]
    assert {b.callback_data for b in toggles} == {"dtog:eyal:m9", "dtog:matti:m9", "dtog:marco:m9"}
    marco_btn = next(b for b in toggles if b.callback_data == "dtog:marco:m9")
    eyal_btn = next(b for b in toggles if b.callback_data == "dtog:eyal:m9")
    assert marco_btn.text.startswith("✅")  # selected
    assert eyal_btn.text.startswith("☐")    # not selected


def test_custom_checklist_controls(monkeypatch):
    import config.team
    monkeypatch.setattr(config.team, "TEAM_MEMBERS", _ROSTER, raising=False)
    kb = _bare_bot()._build_custom_keyboard("m9", selected={"marco", "matti"}, override=True)
    flat = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    assert flat["dqadd:founders:m9"] == "+ Founders"
    assert flat["dqadd:company:m9"] == "+ Company"
    assert flat["dqadd:clear:m9"] == "Clear"
    assert flat["dovr:m9"].startswith("⚠️ Full detail: ON")  # override on
    assert flat["dsend:m9"] == "✅ Send to 2"
    assert flat["dback:m9"] == "‹ Back"
