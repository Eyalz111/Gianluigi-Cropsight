-- MCP OAuth authorization-server storage (audit P3-01 / OAuth).
-- One table holds registered OAuth clients (DCR) + issued access/refresh tokens,
-- so a Cloud Run restart doesn't force Eyal to re-connect (restart-safety, I4).
--
-- Run this in the Supabase SQL editor BEFORE deploying with MCP_OAUTH_ENABLED=true.
BEGIN;

CREATE TABLE IF NOT EXISTS public.mcp_oauth (
    kind        TEXT   NOT NULL,               -- 'client' | 'access' | 'refresh'
    key         TEXT   NOT NULL,               -- client_id or opaque token string
    data        JSONB  NOT NULL,               -- serialized client info / token record
    expires_at  BIGINT,                        -- epoch seconds (NULL for clients)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key)
);

-- Fast expiry sweeps / lookups.
CREATE INDEX IF NOT EXISTS idx_mcp_oauth_expires ON public.mcp_oauth (expires_at);

-- MANDATORY (CLAUDE.md): RLS on every public table. The service-role key bypasses
-- RLS so there is zero functional impact — this just closes anon-key public access.
ALTER TABLE public.mcp_oauth ENABLE ROW LEVEL SECURITY;

COMMIT;
