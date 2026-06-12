"""
Tests for intelligence-signal robustness:
  P3-17 — Perplexity search_batch records a FAILED result for a section whose task
          raised (instead of silently dropping it), so the success ratio is honest.
  P2-18 — the signal context's crop/region extraction reads approved-only tasks so
          unapproved (pending-extraction) task text can't influence an outbound query.
"""

from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# P3-17 — search_batch keeps failed sections
# =============================================================================

class TestSearchBatchFailedSection:
    @pytest.mark.asyncio
    async def test_failed_section_recorded_not_dropped(self):
        from services.perplexity_client import PerplexityClient, PerplexityResult

        client = PerplexityClient()

        async def fake_search(query, system_prompt=""):
            if "boom" in query:
                raise RuntimeError("api down")
            return PerplexityResult(query=query, content="ok", success=True)

        client.search = fake_search

        results = await client.search_batch(
            [{"section": "good", "query": "fine"},
             {"section": "bad", "query": "boom"}],
            max_concurrent=2,
        )

        assert set(results) == {"good", "bad"}           # nothing dropped
        assert results["good"].success is True
        assert results["bad"].success is False           # counts against the ratio
        assert "api down" in (results["bad"].error or "")


# =============================================================================
# P2-18 — crop/region extraction is approved-only
# =============================================================================

class TestSignalContextApprovedOnly:
    def _chain(self):
        chain = MagicMock()
        for m in ("table", "select", "eq", "gte", "limit"):
            getattr(chain, m).return_value = chain
        chain.execute.return_value = MagicMock(data=[])
        return chain

    def test_extract_active_crops_filters_approved(self):
        from processors import intelligence_signal_context as isc
        chain = self._chain()
        with patch.object(isc.supabase_client, "_client", chain):
            isc._extract_active_crops()
        assert ("approval_status", "approved") in [c.args for c in chain.eq.call_args_list]

    def test_extract_active_regions_filters_approved(self):
        from processors import intelligence_signal_context as isc
        chain = self._chain()
        with patch.object(isc.supabase_client, "_client", chain):
            isc._extract_active_regions()
        assert ("approval_status", "approved") in [c.args for c in chain.eq.call_args_list]
