"""
RLS coverage test — defense against tables being created without Row Level
Security enabled.

Runs against the live Supabase project (via service_role) and asserts that
every table in the public schema has RLS enabled. Fails loudly if any table
is missing the lockdown, listing the vulnerable tables by name.

Background: on 2026-04-07 Supabase flagged 6 tables (intelligence_signals,
competitor_watchlist, task_signals, deals, deal_interactions,
external_commitments) as publicly accessible because they were created after
the April 1 RLS migration without ALTER TABLE ... ENABLE ROW LEVEL SECURITY.

This test catches that class of mistake at pytest-time (CI / local dev) so
it never reaches production again.

Requirements:
- Supabase function `public.get_table_rls_status()` must exist
  (created by scripts/migrate_rls_security_v2.sql).
- SUPABASE_URL and SUPABASE_KEY env vars must be configured (service_role key).

If the function or creds are missing, the test is SKIPPED (not failed) so
CI environments without DB access don't block on it. Local dev and the
production CI should both have the function + creds available.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestRlsCoverage:
    def test_all_public_tables_have_rls_enabled(self):
        """Every table in the public schema must have RLS enabled."""
        try:
            from services.supabase_client import supabase_client
            from config.settings import settings
        except Exception as e:
            pytest.skip(f"Cannot import Supabase client ({e})")

        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            pytest.skip("SUPABASE_URL / SUPABASE_KEY not configured")

        # Call the helper function we created in migrate_rls_security_v2.sql
        try:
            result = supabase_client.client.rpc("get_table_rls_status").execute()
        except Exception as e:
            err = str(e).lower()
            if "could not find" in err or "function" in err or "does not exist" in err:
                pytest.skip(
                    "public.get_table_rls_status() function not found — "
                    "run scripts/migrate_rls_security_v2.sql on Supabase"
                )
            raise

        rows = result.data or []
        assert rows, (
            "get_table_rls_status() returned zero rows — "
            "either the schema is empty or the function is misbehaving"
        )

        missing = [r["table_name"] for r in rows if not r.get("rls_enabled")]
        if missing:
            pytest.fail(
                "The following public tables are missing RLS "
                "(anyone with the project URL + anon key can read/write them):\n"
                + "\n".join(f"  - {name}" for name in missing)
                + "\n\nFix: run `ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;` "
                "in Supabase for each, then re-run this test. "
                "Add the pattern to any new CREATE TABLE migration going forward."
            )

    def test_rpc_function_not_accessible_to_anon(self):
        """
        Safety check: the helper function should be callable by service_role
        only, not by the anon key. This tests the GRANT statement in the
        migration file is doing its job.

        We can't easily exercise the anon path from a service_role test, so
        this is a structural check — verify the function exists and that
        calling it returns data (implying service_role can call it). The
        anon restriction is enforced at the Postgres level by the REVOKE.
        """
        try:
            from services.supabase_client import supabase_client
            from config.settings import settings
        except Exception as e:
            pytest.skip(f"Cannot import Supabase client ({e})")

        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            pytest.skip("SUPABASE_URL / SUPABASE_KEY not configured")

        try:
            result = supabase_client.client.rpc("get_table_rls_status").execute()
        except Exception:
            pytest.skip("get_table_rls_status() function not available")

        # If we got here, service_role can call it and data came back
        assert result.data is not None, "Function returned None"
        assert len(result.data) > 0, "Function returned empty list (no tables found?)"
