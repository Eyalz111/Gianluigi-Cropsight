"""PR2 — DB-backed team roster.

Flag off ⇒ roster is the hardcoded literal (byte-identical helpers); flag on
(mocked) ⇒ same shape from DB + tier/telegram; reader error/empty ⇒ hardcoded
fallback (the roster can never come back empty).
"""
from unittest.mock import patch

import config.team as team


# ---------------------------------------------------------------- loader ------
class TestLoadTeamMembers:
    def test_flag_off_is_hardcoded(self):
        with patch.object(team.settings, "TEAM_ROSTER_DB_ENABLED", False):
            assert team._load_team_members() is team._HARDCODED_TEAM_MEMBERS

    def test_flag_on_loads_from_db(self):
        fake = [{
            "member_key": "newbie", "name": "New Hire", "role": "BD",
            "role_description": "x", "primary_email": "new@cropsight.io",
            "identities": ["new@cropsight.io"], "tier": "team",
            "telegram_id": 123, "is_admin": False,
        }]
        with patch.object(team.settings, "TEAM_ROSTER_DB_ENABLED", True), patch(
            "services.supabase_client.supabase_client"
        ) as sc:
            sc.list_team_members.return_value = fake
            out = team._load_team_members()
        assert "newbie" in out
        assert out["newbie"]["email"] == "new@cropsight.io"
        assert out["newbie"]["tier"] == "team"
        assert out["newbie"]["telegram_id"] == 123

    def test_flag_on_db_error_falls_back(self):
        with patch.object(team.settings, "TEAM_ROSTER_DB_ENABLED", True), patch(
            "services.supabase_client.supabase_client"
        ) as sc:
            sc.list_team_members.side_effect = RuntimeError("db down")
            assert team._load_team_members() is team._HARDCODED_TEAM_MEMBERS

    def test_flag_on_empty_falls_back(self):
        with patch.object(team.settings, "TEAM_ROSTER_DB_ENABLED", True), patch(
            "services.supabase_client.supabase_client"
        ) as sc:
            sc.list_team_members.return_value = []
            assert team._load_team_members() is team._HARDCODED_TEAM_MEMBERS


# ---------------------------------------------------------- flag-off parity ---
class TestFlagOffParity:
    def test_hardcoded_has_the_four(self):
        names = [m["name"] for m in team._HARDCODED_TEAM_MEMBERS.values()]
        # Honorific dropped 2026-07-22: the canonical assignee form is
        # first + last name, and "Prof. Yoram Weiss" was the one roster entry
        # that broke it — neither "Yoram" nor "Yoram Weiss" matched it.
        assert names == ["Eyal Zror", "Roye Tadmor", "Paolo Vailetti", "Yoram Weiss"]

    def test_team_emails_derive_in_order(self):
        # CROPSIGHT_TEAM_EMAILS derives from the roster, preserving order.
        assert team.CROPSIGHT_TEAM_EMAILS == [
            m.get("email", "") for m in team.TEAM_MEMBERS.values()
        ]

    def test_helpers_unchanged(self):
        assert team.get_team_member("eyal")["role"] == "CEO"
        assert "Roye Tadmor" in team.get_team_member_names()


# ------------------------------------------------------ telegram id builder ---
class TestTelegramIds:
    def test_flag_on_builds_from_members(self):
        members = {"newbie": {"name": "New Hire", "telegram_id": 999}}
        with patch.object(team.settings, "TEAM_ROSTER_DB_ENABLED", True):
            ids = team._build_telegram_ids(members)
        assert ids["newbie"] == 999
        assert ids["new hire"] == 999

    def test_flag_off_returns_dict(self):
        with patch.object(team.settings, "TEAM_ROSTER_DB_ENABLED", False):
            ids = team._build_telegram_ids(team._HARDCODED_TEAM_MEMBERS)
        assert isinstance(ids, dict)  # populated from settings env (may be empty in test)
