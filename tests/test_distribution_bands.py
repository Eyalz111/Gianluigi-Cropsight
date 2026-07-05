"""Distribution bands (CEO ⊂ Founders ⊂ Company) — recipients + tier capping.

Proves: (1) BEFORE the roster gains members above the founding four, the new
roster+tier logic is byte-identical to the old hardcoded behavior; (2) AFTER the
seed, each band resolves to the right nested recipient set; (3) content is capped
so a Company send can never carry Founders/CEO items. [distribution-groups 2026-07-05]
"""

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
