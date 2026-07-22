"""Built-in OAuth 2.1 authorization server for the MCP endpoint.

Makes the FastMCP server its own self-contained Authorization Server so Claude.ai
connects via OAuth (Dynamic Client Registration + PKCE) instead of authless — the
proper close of the June-2026 audit P3-01 hole. A single OWNER PIN gates the /login
consent page: only someone who knows the PIN can complete a connection, and Claude.ai
never sees the PIN (it just does the standard OAuth redirect).

Restart-safety (invariant I4): registered clients + issued access/refresh tokens are
persisted in the `mcp_oauth` Supabase table, so a Cloud Run cycle does NOT force a
re-login. Authorization codes and the brief authorize->login state live in memory
(5-minute window); a restart mid-handshake just makes Claude retry.

Adapted from the MCP Python SDK simple-auth example for single-user + persistence.
The installed SDK (1.27.x) AuthorizationCode/AccessToken have no `subject` field.
"""

import hmac
import logging
import secrets
import time

from pydantic import AnyHttpUrl
from starlette.exceptions import HTTPException

from config.settings import settings
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

_ACCESS_TTL = 3600                      # access token: 1 hour
_REFRESH_TTL = 60 * 60 * 24 * 30        # refresh token: 30 days
_CODE_TTL = 300                         # authorization code: 5 min


def _scope() -> str:
    return settings.MCP_OAUTH_SCOPE or "user"


class GianluigiOAuthProvider(OAuthAuthorizationServerProvider):
    """Single-owner OAuth authorization server. PIN-gated consent; DB-persisted tokens."""

    def __init__(self, public_url: str):
        self.public_url = public_url.rstrip("/")
        # Ephemeral, short-lived — safe to lose on a restart.
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._state: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Supabase persistence (sync client; the brief block is acceptable). #
    # ------------------------------------------------------------------ #
    def _tbl(self):
        from services.supabase_client import supabase_client
        return supabase_client.client.table("mcp_oauth")

    def _put(self, kind: str, key: str, data: dict, expires_at: int | None = None) -> None:
        self._tbl().upsert({
            "kind": kind, "key": key, "data": data, "expires_at": expires_at,
        }).execute()

    def _get(self, kind: str, key: str) -> dict | None:
        r = self._tbl().select("*").eq("kind", kind).eq("key", key).limit(1).execute()
        return r.data[0] if r.data else None

    def _del(self, kind: str, key: str) -> None:
        try:
            self._tbl().delete().eq("kind", kind).eq("key", key).execute()
        except Exception as e:
            logger.debug(f"oauth _del {kind}:{key} failed (non-fatal): {e}")

    # ------------------------------------------------------------------ #
    # Dynamic client registration (Claude.ai uses DCR).                  #
    # ------------------------------------------------------------------ #
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._get("client", client_id)
        if not row:
            return None
        try:
            return OAuthClientInformationFull(**row["data"])
        except Exception as e:
            logger.warning(f"oauth get_client parse failed: {e}")
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            return
        self._put("client", client_info.client_id, client_info.model_dump(mode="json"))

    # ------------------------------------------------------------------ #
    # authorize -> our PIN-gated /login page.                            #
    # ------------------------------------------------------------------ #
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = params.state or secrets.token_hex(16)
        self._state[state] = {
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,          # PKCE — SDK verifies at /token
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "client_id": client.client_id,
            "resource": params.resource,                       # RFC 8707
            "scopes": params.scopes or [_scope()],
            "ts": time.time(),
        }
        return f"{self.public_url}/login?state={state}"

    def complete_login(self, pin: str, state: str) -> str:
        """Validate the owner PIN and mint an authorization code. Called by /login/callback."""
        sd = self._state.get(state)
        if not sd or (time.time() - sd.get("ts", 0)) > _CODE_TTL:
            self._state.pop(state, None)
            raise HTTPException(400, "This sign-in link expired — reconnect from Claude and try again.")
        expected = settings.MCP_OAUTH_PIN or ""
        # Constant-time compare; reject when no PIN is configured (fail closed).
        if not expected or not hmac.compare_digest(str(pin), expected):
            raise HTTPException(401, "Incorrect PIN.")
        code = f"mcp_{secrets.token_hex(24)}"
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=sd["client_id"],
            redirect_uri=AnyHttpUrl(sd["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(sd["redirect_uri_provided_explicitly"]),
            expires_at=time.time() + _CODE_TTL,
            scopes=list(sd["scopes"]),
            code_challenge=sd["code_challenge"],
            resource=sd.get("resource"),
        )
        del self._state[state]
        # Redirect back to the client's registered redirect_uri (SDK already
        # validated it against the client) with the code + original state.
        return construct_redirect_uri(sd["redirect_uri"], code=code, state=state)

    async def load_authorization_code(self, client, authorization_code: str):
        ac = self._auth_codes.get(authorization_code)
        if ac and ac.expires_at < time.time():
            self._auth_codes.pop(authorization_code, None)
            return None
        return ac

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)
        return self._issue_tokens(
            authorization_code.client_id, authorization_code.scopes, authorization_code.resource
        )

    # ------------------------------------------------------------------ #
    # Refresh tokens (rotating).                                         #
    # ------------------------------------------------------------------ #
    async def load_refresh_token(self, client, refresh_token: str):
        row = self._get("refresh", refresh_token)
        if not row:
            return None
        if row.get("expires_at") and row["expires_at"] < time.time():
            self._del("refresh", refresh_token)
            return None
        d = row["data"]
        return RefreshToken(
            token=refresh_token, client_id=d["client_id"],
            scopes=d["scopes"], expires_at=row.get("expires_at"),
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        # OAuth 2.1: rotate both tokens — drop the presented refresh, issue a new pair.
        self._del("refresh", refresh_token.token)
        return self._issue_tokens(
            refresh_token.client_id, list(scopes) if scopes else refresh_token.scopes, None
        )

    async def load_access_token(self, token: str):
        row = self._get("access", token)
        if not row:
            return None
        if row.get("expires_at") and row["expires_at"] < time.time():
            self._del("access", token)
            return None
        d = row["data"]
        return AccessToken(
            token=token, client_id=d["client_id"], scopes=d["scopes"],
            expires_at=row.get("expires_at"), resource=d.get("resource"),
        )

    async def revoke_token(self, token) -> None:
        key = getattr(token, "token", None) or str(token)
        acc = self._get("access", key)
        if acc:
            paired = (acc.get("data") or {}).get("refresh")
            self._del("access", key)
            if paired:
                self._del("refresh", paired)
            return
        # A refresh token was presented — drop it and any access token bound to it.
        self._del("refresh", key)
        try:
            self._tbl().delete().eq("kind", "access").eq("data->>refresh", key).execute()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def _issue_tokens(self, client_id: str, scopes, resource) -> OAuthToken:
        now = int(time.time())
        access = f"mcp_{secrets.token_hex(32)}"
        refresh = f"mcpr_{secrets.token_hex(32)}"
        scopes = list(scopes)
        self._put("access", access, {
            "client_id": client_id, "scopes": scopes, "resource": resource, "refresh": refresh,
        }, now + _ACCESS_TTL)
        self._put("refresh", refresh, {
            "client_id": client_id, "scopes": scopes,
        }, now + _REFRESH_TTL)
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=_ACCESS_TTL,
            scope=" ".join(scopes), refresh_token=refresh,
        )


def login_page_html(public_url: str, state: str, error: str = "") -> str:
    """Minimal PIN-entry consent page posted to /login/callback."""
    from html import escape
    err = f'<p class="err">{escape(error)}</p>' if error else ""
    base = public_url.rstrip("/")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gianluigi — Sign in</title>
<style>
 body{{font-family:system-ui,-apple-system,Arial,sans-serif;max-width:420px;margin:64px auto;padding:0 20px;color:#111}}
 h2{{margin-bottom:2px}} .sub{{color:#666;margin-top:0}}
 .err{{color:#c0392b;font-size:14px}}
 input{{width:100%;padding:12px;font-size:16px;margin-top:8px;box-sizing:border-box;border:1px solid #ccc;border-radius:6px}}
 button{{margin-top:16px;width:100%;padding:12px;font-size:16px;background:#111;color:#fff;border:0;border-radius:6px;cursor:pointer}}
</style></head>
<body>
 <h2>Gianluigi</h2>
 <p class="sub">Enter the access PIN to connect this workspace.</p>
 {err}
 <form action="{escape(base)}/login/callback" method="post">
  <input type="hidden" name="state" value="{escape(state)}">
  <input type="password" name="pin" placeholder="Access PIN" autocomplete="off" autofocus required>
  <button type="submit">Connect</button>
 </form>
</body></html>"""
