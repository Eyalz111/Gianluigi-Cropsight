# MCP OAuth — activation runbook

The built-in OAuth authorization server is **built and deployed dark** (code live at
rev `gianluigi-00162-zp9`, `MCP_OAUTH_ENABLED=false`). This closes the June-2026 audit
**P3-01** (tokenless `/mcp`) properly: once on, Claude.ai connects via OAuth (DCR + PKCE)
gated by a single owner **PIN**, and authless `/mcp` becomes impossible.

Nothing changes until you flip the flag. When you do, the current authless connector
stops working and must be re-added once (a ~2-minute reconnect).

## Order matters — do these top to bottom

### 1. Run the migration (Supabase SQL editor) — Eyal
Paste and run **`scripts/migrate_mcp_oauth.sql`**. Creates the `mcp_oauth` table (stores
OAuth clients + tokens so a Cloud Run restart doesn't log you out) with RLS.

### 2. Set the PIN (a strong secret you choose) — Eyal
Pick a strong PIN/passphrase. Set it in **both** places:

- **Prod (Cloud Run):**
  ```
  gcloud run services update gianluigi --region=europe-west1 --update-env-vars MCP_OAUTH_PIN=YOUR_STRONG_PIN
  ```
- **Local (.env):** the `MCP_OAUTH_PIN=` line is already scaffolded (empty) — fill it in
  if you ever run locally.

Keep it secret. An empty PIN makes login **fail closed** (nobody can connect).

### 3. Enable OAuth (I can run this once you confirm 1 + 2 are done)
```
gcloud run services update gianluigi --region=europe-west1 --update-env-vars MCP_OAUTH_ENABLED=true
```
The moment this deploys, the existing authless Claude.ai connector will 401 — that's
expected. Go straight to step 4.

### 4. Re-add the connector in Claude.ai — Eyal
1. Settings → Connectors → **gianluigi** → **⋯** → **Remove**.
2. **Add custom connector** → URL: `https://gianluigi-378037201341.europe-west1.run.app/mcp`
3. Claude will start the OAuth flow and open the **Gianluigi sign-in page** → enter your
   **PIN** → it connects.
4. Confirm the **46 tools** appear and a test query works.

### 5. Done — P3-01 is closed
With OAuth on, `/mcp` requires a valid OAuth token (the SDK's `RequireAuthMiddleware`
enforces it); `MCP_ALLOW_AUTHLESS` is ignored. No further action needed. (You may set
`MCP_ALLOW_AUTHLESS=false` for tidiness, but it's already inert.)

## Rollback (instant, if anything goes wrong)
```
gcloud run services update gianluigi --region=europe-west1 --update-env-vars MCP_OAUTH_ENABLED=false
```
Reverts to the prior authless behavior (`MCP_ALLOW_AUTHLESS=true` is still set), then
re-add the connector the old way. ~1 minute.

## Notes
- **Redirect URI:** Claude.ai uses `https://claude.ai/api/mcp/auth_callback` — handled
  automatically by DCR; nothing to configure.
- **Token lifetimes:** access token 1h, refresh token 30d, rotating — Claude refreshes
  silently, so you won't be asked for the PIN again unless the refresh token expires or
  is revoked.
- **What the PIN protects:** it's the consent gate. Even if someone finds the `/mcp` URL,
  they can't connect without the PIN, and they can't reach any tool without a token.
