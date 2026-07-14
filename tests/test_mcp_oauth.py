"""Built-in MCP OAuth authorization server (audit P3-01 / OAuth).

Covers the single-owner PIN gate, the authorize->login->code flow, token issue/
load/expiry, refresh-token rotation, revoke, and DCR client storage. Persistence is
stubbed with an in-memory dict so the logic is tested without Supabase.
"""
import time
from unittest.mock import MagicMock

import pytest
from starlette.exceptions import HTTPException

import services.mcp_oauth as m
from services.mcp_oauth import GianluigiOAuthProvider, login_page_html
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def pin(monkeypatch):
    fake = MagicMock()
    fake.MCP_OAUTH_PIN = "1234"
    fake.MCP_OAUTH_SCOPE = "user"
    monkeypatch.setattr(m, "settings", fake)
    return fake


def _provider():
    p = GianluigiOAuthProvider("https://ex.run.app")
    store: dict = {}
    p._put = lambda kind, key, data, expires_at=None: store.__setitem__((kind, key), {"data": data, "expires_at": expires_at})
    p._get = lambda kind, key: store.get((kind, key))
    p._del = lambda kind, key: store.pop((kind, key), None)
    p._tbl = lambda: MagicMock()
    return p, store


def _client():
    return OAuthClientInformationFull(
        client_id="c1", redirect_uris=[REDIRECT], token_endpoint_auth_method="none"
    )


def _state(p):
    p._state["s1"] = {
        "redirect_uri": REDIRECT, "code_challenge": "cc",
        "redirect_uri_provided_explicitly": True, "client_id": "c1",
        "resource": "https://ex.run.app/mcp", "scopes": ["user"], "ts": time.time(),
    }


class TestAuthorizeAndLogin:
    async def test_authorize_stores_state_and_returns_login_url(self, pin):
        p, _ = _provider()
        params = AuthorizationParams(
            state="s1", scopes=["user"], code_challenge="cc", redirect_uri=REDIRECT,
            redirect_uri_provided_explicitly=True, resource="https://ex.run.app/mcp",
        )
        url = await p.authorize(_client(), params)
        assert url == "https://ex.run.app/login?state=s1"
        assert "s1" in p._state and p._state["s1"]["code_challenge"] == "cc"

    def test_correct_pin_redirects_with_code(self, pin):
        p, _ = _provider()
        _state(p)
        url = p.complete_login("1234", "s1")
        assert url.startswith(REDIRECT + "?")
        assert "code=mcp_" in url and "state=s1" in url
        assert "s1" not in p._state          # state consumed
        assert len(p._auth_codes) == 1

    def test_wrong_pin_401(self, pin):
        p, _ = _provider()
        _state(p)
        with pytest.raises(HTTPException) as ei:
            p.complete_login("wrong", "s1")
        assert ei.value.status_code == 401
        assert not p._auth_codes

    def test_no_pin_configured_fails_closed(self, monkeypatch):
        fake = MagicMock(); fake.MCP_OAUTH_PIN = ""; fake.MCP_OAUTH_SCOPE = "user"
        monkeypatch.setattr(m, "settings", fake)
        p, _ = _provider()
        _state(p)
        with pytest.raises(HTTPException) as ei:
            p.complete_login("anything", "s1")
        assert ei.value.status_code == 401

    def test_bad_state_400(self, pin):
        p, _ = _provider()
        with pytest.raises(HTTPException) as ei:
            p.complete_login("1234", "missing")
        assert ei.value.status_code == 400


class TestTokens:
    async def test_code_exchange_issues_and_persists_tokens(self, pin):
        p, _ = _provider()
        _state(p)
        p.complete_login("1234", "s1")
        code = next(iter(p._auth_codes.values()))
        tok = await p.exchange_authorization_code(_client(), code)
        assert tok.access_token.startswith("mcp_")
        assert tok.refresh_token.startswith("mcpr_")
        assert tok.token_type == "Bearer" and tok.expires_in == 3600
        loaded = await p.load_access_token(tok.access_token)
        assert loaded is not None and loaded.scopes == ["user"]
        assert loaded.resource == "https://ex.run.app/mcp"

    async def test_refresh_rotates_both_tokens(self, pin):
        p, _ = _provider()
        tok = p._issue_tokens("c1", ["user"], None)
        rt = await p.load_refresh_token(_client(), tok.refresh_token)
        assert rt is not None
        new = await p.exchange_refresh_token(_client(), rt, ["user"])
        assert new.access_token != tok.access_token
        assert new.refresh_token != tok.refresh_token
        assert await p.load_refresh_token(_client(), tok.refresh_token) is None  # old gone

    async def test_expired_access_token_not_returned(self, pin):
        p, _ = _provider()
        p._put("access", "old", {"client_id": "c1", "scopes": ["user"], "resource": None, "refresh": "r"}, int(time.time()) - 10)
        assert await p.load_access_token("old") is None

    async def test_revoke_access_drops_paired_refresh(self, pin):
        p, _ = _provider()
        tok = p._issue_tokens("c1", ["user"], None)
        acc = await p.load_access_token(tok.access_token)
        await p.revoke_token(acc)
        assert await p.load_access_token(tok.access_token) is None
        assert await p.load_refresh_token(_client(), tok.refresh_token) is None


class TestClients:
    async def test_register_and_get_client(self, pin):
        p, _ = _provider()
        await p.register_client(_client())
        got = await p.get_client("c1")
        assert got is not None and got.client_id == "c1"
        assert str(got.redirect_uris[0]).rstrip("/") == REDIRECT

    async def test_get_unknown_client_none(self, pin):
        p, _ = _provider()
        assert await p.get_client("nope") is None


def test_login_page_renders_pin_form():
    html = login_page_html("https://ex.run.app", "s1")
    assert 'name="pin"' in html
    assert 'name="state" value="s1"' in html
    assert 'action="https://ex.run.app/login/callback"' in html

def test_login_page_shows_error_and_escapes():
    html = login_page_html("https://ex.run.app", "s1", error="Incorrect PIN.")
    assert "Incorrect PIN." in html
