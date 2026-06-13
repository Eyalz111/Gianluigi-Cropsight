"""
Tests for the remaining silent-failure / crash cleanups (audit PR-G leftovers):
  P2-10 — nightly reconcile defaults a Haiku-dropped fact tier (conservatively,
          never downgrading) instead of ValidationError → None every night.
  P2-11 — operational_snapshot returns content=None on failure, not the error
          string (which would render to Eyal AS the ops brief).
  P2-08 sibling — generate_commitments_due tolerates a bad deadline.
"""

from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# P2-10 — reconcile defaults a missing fact tier conservatively
# =============================================================================

class TestNightlyReconcileTierDefault:
    def _brief(self):
        return {
            "sensitivity": "ceo",
            "narrative": "old",
            "current_status": "active",
            "facts": [{"text": "f1", "sensitivity": "ceo", "citation": None}],
            "version": 1,
        }

    def test_missing_fact_sensitivity_not_downgraded(self):
        from processors import knowledge_consolidation as kc
        # Haiku reworded a fact but DROPPED sensitivity → would silently default to
        # FOUNDERS (a downgrade of a CEO fact). Must default to the brief's tier.
        llm_json = '{"narrative": "new", "current_status": "active", ' \
                   '"facts": [{"text": "reworded", "citation": null}]}'
        with patch("core.llm.call_llm", return_value=(llm_json, {})):
            out = kc._reconcile_brief("Topic X", self._brief())
        assert out is not None
        assert out["facts"][0]["sensitivity"] == "ceo"  # NOT downgraded to founders
        assert out["narrative"] == "new"

    def test_invalid_fact_sensitivity_does_not_no_op(self):
        from processors import knowledge_consolidation as kc
        # An INVALID tier would ValidationError → return None (the nightly no-op
        # that burns Haiku). It must be coerced to the brief's tier instead.
        llm_json = '{"narrative": "new2", "current_status": "active", ' \
                   '"facts": [{"text": "x", "sensitivity": "high", "citation": null}]}'
        with patch("core.llm.call_llm", return_value=(llm_json, {})):
            out = kc._reconcile_brief("Topic Y", self._brief())
        assert out is not None, "an invalid tier must not crash reconcile → None"
        assert out["facts"][0]["sensitivity"] == "ceo"


# =============================================================================
# P2-11 — operational_snapshot returns content=None on failure
# =============================================================================

class TestOperationalSnapshotFailure:
    @pytest.mark.asyncio
    async def test_failure_returns_none_content_not_error_string(self):
        from processors import operational_snapshot as osnap

        # supabase_client is module-level; call_llm is a local import → patch core.llm.
        with patch.object(osnap, "supabase_client", MagicMock()), \
             patch("core.llm.call_llm", side_effect=RuntimeError("LLM down")):
            result = await osnap.generate_operational_snapshot()

        assert result["content"] is None
        assert "LLM down" in result.get("error", "")


# =============================================================================
# P2-08 sibling — generate_commitments_due tolerates a bad deadline
# =============================================================================

class TestCommitmentsDueDateGuard:
    def test_time_component_deadline_parsed(self):
        from processors import deal_intelligence
        rows = [{"organization": "Acme", "commitment": "Send NDA",
                 "deadline": "2026-06-01T00:00:00", "promised_to": "Eyal"}]
        with patch.object(deal_intelligence.supabase_client,
                          "get_overdue_commitments", return_value=rows):
            items = deal_intelligence.generate_commitments_due()
        assert len(items) == 1
        assert items[0]["organization"] == "Acme"

    def test_unparseable_deadline_skipped(self):
        from processors import deal_intelligence
        rows = [
            {"organization": "Bad", "commitment": "x", "deadline": "nonsense"},
            {"organization": "Null", "commitment": "y", "deadline": None},
        ]
        with patch.object(deal_intelligence.supabase_client,
                          "get_overdue_commitments", return_value=rows):
            items = deal_intelligence.generate_commitments_due()  # must NOT raise
        assert items == []
