"""
Group A — silent-failure & correctness tail (June 2026 audit).

  P1-08 — an unparseable extraction deadline lands NULL with deadline_confidence
          forced to NONE (not a contradictory NULL-but-EXPLICIT state), so it
          surfaces cleanly in the daily-QA deadline gap-fill.
  P1-14 — canonical-name fuzzy match strips generic tokens (esp. "cropsight")
          and requires ≥2 shared significant words + ratio>0.6, so two short
          labels sharing only a generic prefix no longer false-merge.
  P2-14 — a debrief whose injection failed is parked at injection_failed and the
          confirm guard reports it honestly instead of "already approved".
  P3-15 — Eyal-only read helpers add approval_status='approved' (gate in principle).
"""

from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# P1-14 — canonical-name fuzzy match no longer false-merges short labels
# =============================================================================

class TestCanonicalFuzzyMatch:
    def _run(self, label, projects):
        from processors import topic_threading as tt
        with patch.object(tt.supabase_client, "match_label_to_canonical", return_value=None), \
             patch.object(tt.supabase_client, "get_canonical_projects", return_value=projects):
            return tt._match_canonical_name(label)

    def test_shared_generic_prefix_does_not_merge(self):
        # "CropSight Investor Deck" vs "CropSight Investor Call": after stripping
        # the generic "cropsight", they share only {investor} → must NOT merge.
        out = self._run("CropSight Investor Deck",
                        [{"name": "CropSight Investor Call"}])
        assert out is None

    def test_two_significant_shared_words_merge(self):
        out = self._run("CropSight Investor Deck",
                        [{"name": "Investor Deck Prep"}])
        assert out == "Investor Deck Prep"

    def test_exact_substring_still_matches(self):
        out = self._run("Moldova Pilot",
                        [{"name": "Moldova Pilot Rollout"}])
        assert out == "Moldova Pilot Rollout"


# =============================================================================
# P1-08 — unparseable extraction deadline → NULL + confidence NONE
# =============================================================================

class TestUnparseableDeadlineConfidence:
    def _capture_insert(self):
        from services.supabase_client import supabase_client as sc
        captured = {}
        client = MagicMock()

        def _insert(rows):
            captured["rows"] = rows
            ex = MagicMock()
            ex.execute.return_value = MagicMock(data=rows)
            return ex
        client.table.return_value.insert.side_effect = _insert
        return sc, client, captured

    def test_vague_deadline_text_lands_null_none(self):
        from services.supabase_client import supabase_client as sc
        _, client, captured = self._capture_insert()
        with patch.object(sc, "_client", client), \
             patch.object(sc, "get_areas", return_value=[]), \
             patch.object(sc, "resolve_category", side_effect=lambda c, areas=None: c or "General"):
            sc.create_tasks_batch(
                "m1",
                [{"title": "Sign term sheet", "deadline": "end of July 2026",
                  "deadline_confidence": "EXPLICIT"}],
            )
        row = captured["rows"][0]
        assert row["deadline"] is None
        assert row["deadline_confidence"] == "NONE"   # not the contradictory EXPLICIT

    def test_parseable_deadline_kept(self):
        from services.supabase_client import supabase_client as sc
        _, client, captured = self._capture_insert()
        with patch.object(sc, "_client", client), \
             patch.object(sc, "get_areas", return_value=[]), \
             patch.object(sc, "resolve_category", side_effect=lambda c, areas=None: c or "General"):
            sc.create_tasks_batch(
                "m1",
                [{"title": "Ship", "deadline": "2026-07-20", "deadline_confidence": "EXPLICIT"}],
            )
        row = captured["rows"][0]
        assert str(row["deadline"]).startswith("2026-07-20")   # serializer expands to ISO datetime
        assert row["deadline_confidence"] == "EXPLICIT"


# =============================================================================
# P3-15 — Eyal-only read helpers filter approved-only
# =============================================================================

class TestReadHelperApprovalFilter:
    def _chain(self):
        chain = MagicMock()
        for m in ("table", "select", "eq", "in_", "or_", "is_", "lt", "lte",
                  "gte", "not_", "order", "limit", "ilike"):
            getattr(chain, m).return_value = chain
        chain.not_.is_.return_value = chain
        chain.execute.return_value = MagicMock(data=[])
        return chain

    def test_get_stale_tasks_filters_approved(self):
        from services.supabase_client import supabase_client as sc
        chain = self._chain()
        with patch.object(sc, "_client", chain):
            sc.get_stale_tasks()
        assert ("approval_status", "approved") in [c.args for c in chain.eq.call_args_list]

    def test_get_tasks_without_deadline_filters_approved(self):
        from services.supabase_client import supabase_client as sc
        chain = self._chain()
        with patch.object(sc, "_client", chain):
            sc.get_tasks_without_deadline()
        assert ("approval_status", "approved") in [c.args for c in chain.eq.call_args_list]

    def test_get_changes_since_filters_approved(self):
        from services.supabase_client import supabase_client as sc
        chain = self._chain()
        with patch.object(sc, "_client", chain):
            sc.get_changes_since("2026-01-01")
        assert ("approval_status", "approved") in [c.args for c in chain.eq.call_args_list]


# =============================================================================
# P2-14 — debrief injection-failed state is surfaced, not "already approved"
# =============================================================================

class TestDebriefInjectionFailedGuard:
    async def test_injection_failed_status_reports_honestly(self):
        from processors import debrief
        sess = {"id": "s1", "status": "injection_failed", "items_captured": []}
        with patch.object(debrief.supabase_client, "get_debrief_session", return_value=sess):
            out = await debrief.confirm_debrief("s1", approved=True)
        assert out["action"] == "error"
        assert "failed" in out["response"].lower()

    async def test_failed_injection_parks_session(self, monkeypatch):
        from processors import debrief
        sess = {"id": "s1", "status": "confirming", "items_captured": [{"x": 1}],
                "date": "2026-06-01"}
        updates = []
        claim = MagicMock()
        claim.update.return_value = claim
        claim.eq.return_value = claim
        claim.execute.return_value = MagicMock(data=[{"id": "s1"}])  # CAS claim succeeds

        monkeypatch.setattr(debrief.supabase_client, "get_debrief_session", lambda sid: sess)
        monkeypatch.setattr(debrief.supabase_client, "_client",
                            MagicMock(table=lambda *_: claim))
        monkeypatch.setattr(debrief.supabase_client, "update_debrief_session",
                            lambda sid, **k: updates.append(k) or {})

        async def _boom(*a, **k):
            raise RuntimeError("inject failed")
        monkeypatch.setattr(debrief, "_inject_debrief_items", _boom)

        out = await debrief.confirm_debrief("s1", approved=True)
        assert out["action"] == "error"
        assert {"status": "injection_failed"} in updates
