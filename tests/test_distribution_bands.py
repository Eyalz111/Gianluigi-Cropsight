"""Distribution bands (CEO ⊂ Founders ⊂ Company) — recipients + tier capping.

Proves: (1) BEFORE the roster gains members above the founding four, the new
roster+tier logic is byte-identical to the old hardcoded behavior; (2) AFTER the
seed, each band resolves to the right nested recipient set; (3) content is capped
so a Company send can never carry Founders/CEO items. [distribution-groups 2026-07-05]
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from guardrails import distribution as D
from guardrails.sensitivity_classifier import get_distribution_list

EYAL = "eyal@x.com"
ROYE = "roye@x.com"
PAOLO = "paolo@x.com"
YORAM = "yoram@x.com"
MATTI = "matti@x.com"
MARCO = "marco@x.com"
HADAR = "hadar@x.com"
IDO = "ido@x.com"

PRE_SEED = {
    "eyal": {"email": EYAL, "tier": "ceo", "status": "active"},
    "roye": {"email": ROYE, "tier": "founders", "status": "active"},
    "paolo": {"email": PAOLO, "tier": "founders", "status": "active"},
    "yoram": {"email": YORAM, "tier": "founders", "status": "active"},
}
POST_SEED = {
    **PRE_SEED,
    "matti": {"email": MATTI, "tier": "founders", "status": "active"},
    "marco": {"email": MARCO, "tier": "team", "status": "active"},
    "hadar": {"email": HADAR, "tier": "team", "status": "active"},
    "ido": {"email": IDO, "tier": "team", "status": "active"},
}


@pytest.fixture
def prod(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "ENVIRONMENT", "production", raising=False)
    monkeypatch.setattr(settings, "EYAL_EMAIL", EYAL, raising=False)


def _roster(monkeypatch, roster):
    import config.team
    monkeypatch.setattr(config.team, "TEAM_MEMBERS", roster, raising=False)


def test_preseed_identical_to_old_behavior(prod, monkeypatch):
    _roster(monkeypatch, PRE_SEED)
    assert get_distribution_list("ceo") == [EYAL]                       # ceo -> Eyal only
    assert set(get_distribution_list("founders")) == {EYAL, ROYE, PAOLO, YORAM}
    # 'team'/'public' historically went to the full 4-person team too:
    assert set(get_distribution_list("team")) == {EYAL, ROYE, PAOLO, YORAM}
    assert set(get_distribution_list("public")) == {EYAL, ROYE, PAOLO, YORAM}


def test_postseed_nested_bands(prod, monkeypatch):
    _roster(monkeypatch, POST_SEED)
    assert D.recipients_for_band("ceo") == [EYAL]
    assert set(D.recipients_for_band("founders")) == {EYAL, ROYE, PAOLO, YORAM, MATTI}
    assert set(D.recipients_for_band("company")) == {EYAL, ROYE, PAOLO, YORAM, MATTI, MARCO, HADAR, IDO}
    # Company members are NOT in a Founders send (no leak of audience):
    for company_only in (MARCO, HADAR, IDO):
        assert company_only not in D.recipients_for_band("founders")
    # team-facing package excludes Eyal but includes the new founder:
    assert set(D.recipients_for_band("founders", exclude_eyal=True)) == {ROYE, PAOLO, YORAM, MATTI}


def test_content_cap_prevents_leak():
    items = [
        {"id": "p", "sensitivity": "public"},
        {"id": "t", "sensitivity": "team"},
        {"id": "f", "sensitivity": "founders"},
        {"id": "c", "sensitivity": "ceo"},
        {"id": "d"},  # no sensitivity -> defaults to founders(3)
    ]
    # Founders band (level 3): keep <=founders, strip CEO.
    fnd = {i["id"] for i in D.cap_items_for_band(items, "founders")}
    assert fnd == {"p", "t", "f", "d"}
    # Company band (level 2): keep only public/team, strip founders(+default)+ceo.
    comp = {i["id"] for i in D.cap_items_for_band(items, "company")}
    assert comp == {"p", "t"}
    # CEO band (level 4): keep everything.
    assert len(D.cap_items_for_band(items, "ceo")) == len(items)


def test_band_and_level_mapping():
    assert D.band_for_sensitivity("ceo") == "ceo"
    assert D.band_for_sensitivity("sensitive") == "ceo"  # legacy
    assert D.band_for_sensitivity("founders") == "founders"
    assert D.band_for_sensitivity("normal") == "founders"  # legacy
    assert D.band_for_sensitivity("team") == "company"
    assert D.band_for_sensitivity("public") == "company"
    assert D.band_for_sensitivity(None) == "founders"
    assert D.level_for_sensitivity("ceo") == 4
    assert D.level_for_sensitivity("founders") == 3
    assert D.level_for_sensitivity("team") == 2


def test_dev_mode_is_eyal_only(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "ENVIRONMENT", "development", raising=False)
    monkeypatch.setattr(settings, "EYAL_EMAIL", EYAL, raising=False)
    _roster(monkeypatch, POST_SEED)
    assert D.recipients_for_band("company") == [EYAL]
    assert D.recipients_for_band("founders", exclude_eyal=True) == []


def test_custom_recipients_leak_safe_cap(prod, monkeypatch):
    _roster(monkeypatch, POST_SEED)
    # Mixed-tier pick (paolo=founders3 + marco=team2) -> cap to the LOWEST (2).
    emails, cap = D.resolve_custom_recipients(["paolo", "marco"])
    assert set(emails) == {PAOLO, MARCO}
    assert cap == 2
    # All-founders pick -> cap 3.
    _, cap_f = D.resolve_custom_recipients(["roye", "matti"])
    assert cap_f == 3


def test_custom_recipients_override_lifts_cap(prod, monkeypatch):
    _roster(monkeypatch, POST_SEED)
    emails, cap = D.resolve_custom_recipients(["paolo", "marco"], override=True)
    assert set(emails) == {PAOLO, MARCO}
    assert cap == 4  # full detail to everyone selected


def test_custom_recipients_unknown_keys_ignored(prod, monkeypatch):
    _roster(monkeypatch, POST_SEED)
    emails, _ = D.resolve_custom_recipients(["marco", "ghost", ""])
    assert emails == [MARCO]


def test_custom_recipients_dev_is_eyal_only(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "ENVIRONMENT", "development", raising=False)
    monkeypatch.setattr(settings, "EYAL_EMAIL", EYAL, raising=False)
    _roster(monkeypatch, POST_SEED)
    emails, _ = D.resolve_custom_recipients(["marco", "hadar", "ido"])
    assert emails == [EYAL]


@pytest.mark.asyncio
async def test_distribute_honors_custom_selection_and_caps():
    """End-to-end: content['__distribution'] overrides recipients AND caps items
    to the lowest selected tier — a Company-tier custom recipient must not receive
    Founders items. [distribution-groups custom]"""
    with patch("guardrails.approval_flow.supabase_client") as mock_db, \
         patch("guardrails.approval_flow.drive_service") as mock_drive, \
         patch("guardrails.approval_flow.sheets_service") as mock_sheets, \
         patch("guardrails.approval_flow.gmail_service") as mock_gmail, \
         patch("guardrails.approval_flow.comms_spine") as mock_tg, \
         patch("guardrails.approval_flow.settings") as mock_settings, \
         patch("guardrails.distribution.resolve_custom_recipients", return_value=(["marco@x.com"], 2)), \
         patch("services.word_generator.generate_summary_docx", return_value=b"docx"):
        mock_settings.ENVIRONMENT = "production"
        mock_db.get_meeting = MagicMock(return_value={"participants": [], "duration_minutes": 30, "summary": "x"})
        mock_db.get_tasks = MagicMock(return_value=[])
        mock_db.log_action = MagicMock(return_value={"id": "l"})
        mock_drive.save_meeting_summary = AsyncMock(return_value={"id": "d", "webViewLink": "http://x"})
        mock_drive.save_meeting_summary_docx = AsyncMock(return_value={"id": "d2", "webViewLink": "http://y"})
        mock_sheets.add_task = AsyncMock(return_value=True)
        mock_gmail.send_meeting_summary = AsyncMock(return_value=True)
        mock_tg.send_to_group = AsyncMock(return_value=True)
        mock_tg.send_to_eyal = AsyncMock(return_value=True)
        mock_tg.send_meeting_summary = AsyncMock(return_value=True)

        content = {
            "title": "BD Sync", "date": "2026-06-12", "summary": "s",
            "executive_summary": "e", "discussion_summary": "d",
            "decisions": [
                {"description": "founders decision", "sensitivity": "founders"},
                {"description": "team decision", "sensitivity": "team"},
            ],
            "tasks": [
                {"title": "founders task", "sensitivity": "founders"},
                {"title": "team task", "sensitivity": "team"},
            ],
            "open_questions": [], "follow_ups": [], "stakeholders": [],
            "__distribution": {"recipients": ["marco"], "override": False},
        }
        from guardrails.approval_flow import distribute_approved_content
        result = await distribute_approved_content("m-2", content, sensitivity="founders")

    assert result["emails_to"] == ["marco@x.com"]  # custom recipient, not the Founders band
    kw = mock_gmail.send_meeting_summary.call_args.kwargs
    titles = [t.get("title") for t in kw.get("tasks", [])]
    assert "founders task" not in titles  # capped to level 2 (Company)
    assert "team task" in titles
