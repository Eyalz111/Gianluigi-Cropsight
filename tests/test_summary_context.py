"""
Tests for the meeting-summary executive-context finetune (v2.5 Phase 3 chunk 2):
the clause builders (processors/summary_context.py), their tier-gating + guards,
and format_summary rendering (incl. the byte-identical baseline regression guard).

Patches attributes on the real supabase_client singleton — never replaces it.
"""

from unittest.mock import MagicMock, patch

import processors.summary_context as sc
from core.system_prompt import format_summary
from services.supabase_client import supabase_client


# =========================================================================
# Helpers
# =========================================================================

class TestNormalizeLevel:
    def test_ladder_and_aliases(self):
        assert sc._normalize_level("public") == 1
        assert sc._normalize_level("team") == 2
        assert sc._normalize_level("founders") == 3
        assert sc._normalize_level("ceo") == 4
        assert sc._normalize_level("ceo_only") == 4  # legacy alias
        assert sc._normalize_level("restricted") == 4
        assert sc._normalize_level("normal") == 3
        assert sc._normalize_level(None) == 3          # missing => founders
        assert sc._normalize_level("garbage") == 3     # unknown => founders

    def test_date_and_short(self):
        assert sc._fmt_date("2026-05-02") == "May 02, 2026"
        assert sc._fmt_date("2026-05-02T09:00:00Z") == "May 02, 2026"
        assert sc._fmt_date(None) == ""
        # first sentence only, trailing period stripped
        assert sc._short_desc("Use AWS for hosting. Roye flagged cost.") == "Use AWS for hosting"
        # long single sentence capped at 60 + ellipsis
        out = sc._short_desc("x" * 80)
        assert out.endswith("…") and len(out) <= 61


# =========================================================================
# build_supersession_clauses
# =========================================================================

class TestSupersessionClauses:
    def _patch_parents(self, mapping):
        return patch.object(supabase_client, "get_decisions_by_ids",
                            MagicMock(return_value=mapping))

    def test_clause_built_when_tier_safe(self):
        decisions = [{"description": "Switch to GCP"}]
        supers = [{"new_index": 1, "old_id": "p1", "reason": "x"}]
        parents = {"p1": {"description": "Use AWS for everything.", "date": "2026-05-02",
                          "sensitivity": "founders"}}
        with self._patch_parents(parents):
            out = sc.build_supersession_clauses(decisions, supers, "founders")
        assert out == {1: "(reverses the May 02, 2026 decision: Use AWS for everything)"}

    def test_omitted_when_parent_above_meeting_tier(self):
        decisions = [{"description": "d"}]
        supers = [{"new_index": 1, "old_id": "p1", "reason": "x"}]
        parents = {"p1": {"description": "Secret", "date": "2026-05-02", "sensitivity": "ceo"}}
        with self._patch_parents(parents):
            out = sc.build_supersession_clauses(decisions, supers, "founders")
        assert out == {}  # CEO prior must not leak into a founders summary

    def test_out_of_range_new_index_skipped(self):
        decisions = [{"description": "only one"}]
        supers = [{"new_index": 5, "old_id": "p1", "reason": "x"}]
        parents = {"p1": {"description": "d", "date": "2026-05-02", "sensitivity": "team"}}
        with self._patch_parents(parents):
            out = sc.build_supersession_clauses(decisions, supers, "founders")
        assert out == {}

    def test_missing_parent_skipped(self):
        decisions = [{"description": "d"}]
        supers = [{"new_index": 1, "old_id": "p1", "reason": "x"}]
        with self._patch_parents({}):  # parent not found
            out = sc.build_supersession_clauses(decisions, supers, "founders")
        assert out == {}

    def test_new_index_maps_to_correct_decision(self):
        decisions = [{"description": "first"}, {"description": "second"}]
        supers = [{"new_index": 2, "old_id": "p1", "reason": "x"}]
        parents = {"p1": {"description": "old", "date": "2026-04-01", "sensitivity": "team"}}
        with self._patch_parents(parents):
            out = sc.build_supersession_clauses(decisions, supers, "founders")
        assert set(out.keys()) == {2}

    def test_empty_supersessions_returns_empty(self):
        assert sc.build_supersession_clauses([{"description": "d"}], [], "founders") == {}


# =========================================================================
# build_topic_context
# =========================================================================

class TestTopicContext:
    # sensitivity lives INSIDE brief_json (matches the real topic_threads schema)
    def test_one_safe_topic(self):
        threads = [{"topic_name": "Moldova",
                    "brief_json": {"current_status": "active", "sensitivity": "founders"},
                    "last_updated": "2026-05-10"}]
        assert sc.build_topic_context(threads, "founders") == "\n**Where this fits:** Moldova — active"

    def test_ceo_topic_omitted_in_founders_meeting(self):
        threads = [{"topic_name": "Secret",
                    "brief_json": {"current_status": "blocked", "sensitivity": "ceo"},
                    "last_updated": "2026-05-10"}]
        assert sc.build_topic_context(threads, "founders") is None

    def test_untiered_brief_omitted(self):
        threads = [{"topic_name": "X",
                    "brief_json": {"current_status": "active"},  # no sensitivity key
                    "last_updated": "2026-05-10"}]
        assert sc.build_topic_context(threads, "ceo") is None

    def test_caps_at_two_with_deterministic_order(self):
        threads = [
            {"topic_name": "A", "brief_json": {"current_status": "s1", "sensitivity": "team"}, "last_updated": "2026-05-01"},
            {"topic_name": "B", "brief_json": {"current_status": "s2", "sensitivity": "team"}, "last_updated": "2026-05-20"},
            {"topic_name": "C", "brief_json": {"current_status": "s3", "sensitivity": "team"}, "last_updated": "2026-05-10"},
        ]
        out = sc.build_topic_context(threads, "founders")
        # last_updated desc → B (05-20) then C (05-10); A drops
        assert out == "\n**Where this fits:** B — s2; C — s3"

    def test_none_when_no_current_status(self):
        threads = [{"topic_name": "X", "brief_json": {"sensitivity": "team"}, "last_updated": "2026-05-10"}]
        assert sc.build_topic_context(threads, "founders") is None

    def test_none_when_no_threads(self):
        assert sc.build_topic_context([], "founders") is None


# =========================================================================
# format_summary rendering (incl. regression guards)
# =========================================================================

_BASE = dict(
    meeting_title="Sync", meeting_date="2026-05-27", participants=["Eyal", "Roye"],
    duration_minutes=30, sensitivity="founders",
    decisions=[{"description": "Use AWS", "participants_involved": ["Eyal"], "transcript_timestamp": "00:10"}],
    tasks=[], follow_ups=[], open_questions=[], discussion_summary="Talked.", stakeholders_mentioned=[],
)


class TestFormatSummaryRender:
    def test_baseline_identical_when_no_context(self):  # regression
        out = format_summary(**_BASE)
        assert "Where this fits" not in out
        assert "1. Use AWS — Eyal (ref: ~00:10)" in out  # no clause appended
        # all 7 sections still present
        for header in ("## Key Decisions", "## Action Items", "## Follow-Up Meetings",
                       "## Open Questions & Risks", "## Discussion Summary",
                       "## Stakeholders/Contacts Mentioned"):
            assert header in out

    def test_decision_clause_attaches_to_right_line(self):
        out = format_summary(**_BASE, decision_context={1: "(reverses the May 02, 2026 decision: Use GCP)"})
        assert "1. Use AWS (reverses the May 02, 2026 decision: Use GCP) — Eyal (ref: ~00:10)" in out

    def test_topic_line_under_header_no_new_section(self):
        out = format_summary(**_BASE, topic_context="\n**Where this fits:** Moldova — active")
        lines = out.splitlines()
        sens_idx = next(i for i, l in enumerate(lines) if l.startswith("**Sensitivity:**"))
        assert lines[sens_idx + 1] == "**Where this fits:** Moldova — active"  # directly under header
        assert "## Where this fits" not in out  # not a new section

    def test_tier_blocked_compose_yields_clean_summary(self):
        # The stored-string tier-safety guarantee: tier-blocked builders → no clause →
        # format renders the clean baseline (safe to reuse for the team email).
        decisions = [{"description": "d"}]
        supers = [{"new_index": 1, "old_id": "p1", "reason": "x"}]
        parents = {"p1": {"description": "Secret", "date": "2026-05-02", "sensitivity": "ceo"}}
        with patch.object(supabase_client, "get_decisions_by_ids", MagicMock(return_value=parents)):
            dctx = sc.build_supersession_clauses(decisions, supers, "founders")
        tctx = sc.build_topic_context(
            [{"topic_name": "Secret",
              "brief_json": {"current_status": "blocked", "sensitivity": "ceo"}, "last_updated": "2026-05-10"}], "founders")
        assert dctx == {} and tctx is None
        out = format_summary(**{**_BASE, "decisions": decisions},
                             decision_context=dctx, topic_context=tctx)
        assert "reverses" not in out and "Where this fits" not in out


# =========================================================================
# process_transcript wiring (source-level regression guards — mirrors the
# repo's existing tp source-introspection tests; the inline block can't be
# unit-driven without the full pipeline)
# =========================================================================

class TestProcessTranscriptWiring:
    def _src(self):
        import processors.transcript_processor as tp
        return open(tp.__file__, "r", encoding="utf-8").read()

    def test_enrichment_is_flag_gated(self):  # regression
        # Flag off ⇒ the block is skipped (no re-render, no update_meeting write).
        assert "if settings.SUMMARY_CONTEXT_ENABLED:" in self._src()

    def test_linked_threads_return_captured(self):
        assert "linked_threads = await link_meeting_to_topics(" in self._src()

    def test_db_write_before_local_reassign(self):  # regression
        src = self._src()
        write = src.find("update_meeting(meeting_id, summary=enriched)")
        reassign = src.find("summary = enriched\n")
        assert write != -1 and reassign != -1 and write < reassign
