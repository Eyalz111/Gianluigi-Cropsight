"""PR7 — forward-facing rich meeting summary + executive TL;DR.

Behind SUMMARY_RICH_ENABLED. The renderer (`format_summary`) stays stateless;
the gather lives in `processors/summary_rich.py`. Tests pair the flag-OFF
(rich=False → legacy, byte-for-byte) path with the flag-ON enrichment, and
nail the two non-negotiables: tier-safety (above-tier facts never render) and
no-invented-dates (the TL;DR falls back to fact-only on any LLM failure).
"""
from unittest.mock import patch

import pytest

from core.system_prompt import format_summary
from processors import summary_rich as sr


def _base_kwargs(**over):
    kw = dict(
        meeting_title="Arch Sync",
        meeting_date="2026-06-10",
        participants=["Eyal", "Roye"],
        duration_minutes=45,
        sensitivity="founders",
        decisions=[{"description": "Adopt Postgres", "participants_involved": ["Roye"]}],
        tasks=[{"title": "Ship pilot", "assignee": "Roye", "deadline": "2026-06-20",
                "priority": "H", "urgency": "H", "category": "PRODUCT & TECHNOLOGY"}],
        follow_ups=[],
        open_questions=[],
        discussion_summary="We discussed the stack.",
        stakeholders_mentioned=[],
    )
    kw.update(over)
    return kw


# ---------------------------------------------------------------------------
# format_summary — legacy (rich=False) vs rich=True
# ---------------------------------------------------------------------------
class TestRenderer:
    def test_legacy_is_unchanged(self):
        out = format_summary(**_base_kwargs())
        # legacy Action Items header (the live registry template), NOT the
        # rich 8-column header, and no rich sections.
        assert "| # | Task | Category | Assignee | Deadline | Priority | Ref |" in out
        assert "| # | Task | Area | Owner | Deadline | Priority | Urgency | Ref |" not in out
        assert "TL;DR" not in out
        assert "Decision Intelligence" not in out
        assert "Per-Category Focus" not in out

    def test_legacy_ignores_rich_kwargs(self):
        # passing rich blocks with rich=False must be inert (off-path safety)
        plain = format_summary(**_base_kwargs())
        with_kwargs = format_summary(
            **_base_kwargs(), tl_dr="\n\n> X", decision_intelligence="\n## DI\n",
            area_rollup="\n## PA\n", risks_text="\n## RB\n", changed_since="\n## WC\n",
        )
        assert plain == with_kwargs

    def test_rich_adds_urgency_area_columns(self):
        out = format_summary(**_base_kwargs(), rich=True)
        assert "| # | Task | Area | Owner | Deadline | Priority | Urgency | Ref |" in out
        # the H/categorized task renders its category (Area cell) + urgency cells
        assert "| PRODUCT & TECHNOLOGY |" in out
        assert "| H |" in out

    def test_rich_renders_supplied_blocks(self):
        out = format_summary(
            **_base_kwargs(), rich=True,
            tl_dr="\n\n> **🎯 TL;DR**\n> Postgres chosen",
            decision_intelligence="\n\n## Decision Intelligence\nstuff\n",
            area_rollup="\n\n## Per-Category Focus\n- x\n",
            risks_text="\n\n## Risks & Blockers\n- y\n",
            changed_since="\n\n## What Changed Since Last Time\nz\n",
        )
        assert "🎯 TL;DR" in out
        assert "## Decision Intelligence" in out
        assert "## Per-Category Focus" in out
        assert "## Risks & Blockers" in out
        assert "## What Changed Since Last Time" in out

    def test_rich_empty_blocks_collapse(self):
        # default ""/None blocks → no empty headers leak into the summary
        out = format_summary(**_base_kwargs(), rich=True)
        assert "Decision Intelligence" not in out
        assert "Per-Category Focus" not in out
        assert "Risks & Blockers" not in out

    def test_rich_empty_action_items(self):
        out = format_summary(**_base_kwargs(tasks=[]), rich=True)
        assert "*No action items recorded*" in out


# ---------------------------------------------------------------------------
# Decision Intelligence — tier-safe, only when there's intelligence
# ---------------------------------------------------------------------------
class TestDecisionIntelligence:
    def test_renders_rationale_options_confidence(self):
        out = sr.build_decision_intelligence(
            [{"description": "Adopt Postgres", "rationale": "scales better",
              "options_considered": ["Mongo", "Postgres"], "confidence": 4}],
            {}, "founders",
        )
        assert "## Decision Intelligence" in out
        assert "Rationale: scales better" in out
        assert "Options weighed: Mongo, Postgres" in out
        assert "Confidence: high" in out

    def test_supersession_clause_surfaces(self):
        out = sr.build_decision_intelligence(
            [{"description": "Switch to GCP"}],
            {1: "(reverses the Jan 1, 2026 decision: use AWS)"}, "founders",
        )
        assert "reverses the Jan 1, 2026 decision" in out

    def test_no_intelligence_is_empty(self):
        out = sr.build_decision_intelligence(
            [{"description": "bare decision"}], {}, "founders",
        )
        assert out == ""

    def test_above_tier_decision_dropped(self):
        # a CEO-tier decision must not render into a FOUNDERS-tier summary
        out = sr.build_decision_intelligence(
            [{"description": "secret", "rationale": "hush", "sensitivity": "ceo"}],
            {}, "founders",
        )
        assert out == ""


# ---------------------------------------------------------------------------
# Per-Category Focus (groups by task.category since the 2026-06 realignment)
# ---------------------------------------------------------------------------
class TestAreaRollup:
    def test_groups_and_flags_urgent(self):
        out = sr.build_area_rollup([
            {"category": "PRODUCT & TECHNOLOGY", "urgency": "H"},
            {"category": "PRODUCT & TECHNOLOGY", "urgency": "L"},
            {"category": "SALES & BUSINESS DEVELOPMENT", "urgency": "M"},
        ])
        assert "## Per-Category Focus" in out
        assert "**PRODUCT & TECHNOLOGY**: 2 action items (1 urgent)" in out
        assert "**SALES & BUSINESS DEVELOPMENT**: 1 action item" in out
        # busiest category first
        assert out.index("PRODUCT & TECHNOLOGY") < out.index("SALES & BUSINESS DEVELOPMENT")

    def test_empty(self):
        assert sr.build_area_rollup([]) == ""


# ---------------------------------------------------------------------------
# Risks & Blockers — tier-safe, deduped
# ---------------------------------------------------------------------------
class TestRisks:
    def test_pulls_risks_and_blockers(self):
        threads = [{
            "topic_name": "Pilot",
            "brief_json": {
                "sensitivity": "founders",
                "current_status": "blocked",
                "risks": ["data licence unclear"],
                "open_items": [{"kind": "blocker", "text": "waiting on Roye"}],
            },
        }]
        out = sr.build_risks_blockers(threads, "founders")
        assert "data licence unclear" in out
        assert "blocked" in out
        assert "waiting on Roye" in out

    def test_above_tier_brief_dropped(self):
        threads = [{"topic_name": "Secret", "brief_json": {
            "sensitivity": "ceo", "risks": ["leak risk"]}}]
        assert sr.build_risks_blockers(threads, "founders") == ""

    def test_empty(self):
        assert sr.build_risks_blockers([], "founders") == ""


# ---------------------------------------------------------------------------
# Executive TL;DR — LLM path + deterministic, fact-only fallback
# ---------------------------------------------------------------------------
class TestTlDr:
    def test_deterministic_fallback_picks_high_urgency(self):
        extracted = {"decisions": [{"description": "x"}],
                     "tasks": [{"title": "Low task", "assignee": "A", "urgency": "L"},
                               {"title": "Hot task", "assignee": "Roye", "urgency": "H"}]}
        out = sr._deterministic_tl_dr(extracted)
        assert "TL;DR" in out
        assert "1 decision" in out
        assert "Hot task" in out         # the H task is the next action
        assert "Low task" not in out

    def test_empty_when_nothing(self):
        assert sr._deterministic_tl_dr({"decisions": [], "tasks": []}) == ""

    async def test_llm_path(self):
        extracted = {"decisions": [{"description": "Use Postgres"}],
                     "tasks": [{"title": "Ship", "assignee": "Roye", "urgency": "H"}]}
        with patch("core.llm.call_llm", return_value=("Postgres chosen.\nRoye ships pilot.", {})):
            out = await sr.build_tl_dr("Arch", extracted)
        assert "🎯 TL;DR" in out
        assert "Roye ships pilot" in out

    async def test_llm_failure_falls_back(self):
        extracted = {"decisions": [{"description": "x"}],
                     "tasks": [{"title": "Ship pilot", "assignee": "Roye", "urgency": "H"}]}
        with patch("core.llm.call_llm", side_effect=RuntimeError("no api")):
            out = await sr.build_tl_dr("Arch", extracted)
        assert "Ship pilot" in out       # deterministic fallback, no invention
        assert "1 decision" in out


# ---------------------------------------------------------------------------
# Orchestrator — end-to-end rich render (hermetic: external calls mocked)
# ---------------------------------------------------------------------------
class TestBuildRichSummary:
    async def test_renders_full_rich_summary(self):
        extracted = {
            "decisions": [{"description": "Adopt Postgres", "rationale": "scales",
                           "confidence": 4}],
            "tasks": [{"title": "Ship pilot", "assignee": "Roye", "deadline": "2026-06-20",
                       "priority": "H", "urgency": "H", "category": "PRODUCT & TECHNOLOGY"}],
            "follow_ups": [], "open_questions": [],
            "discussion_summary": "stack talk", "stakeholders": [],
        }
        threads = [{"topic_name": "Pilot", "brief_json": {
            "sensitivity": "founders", "risks": ["licence unclear"]}}]
        with patch("core.llm.call_llm", side_effect=RuntimeError("offline")), \
             patch("processors.summary_context.build_supersession_clauses", return_value={}), \
             patch("processors.summary_context.build_topic_context", return_value=None), \
             patch("processors.meeting_continuity.build_meeting_continuity_context", return_value=None):
            out = await sr.build_rich_summary(
                meeting_title="Arch", meeting_date="2026-06-10",
                participants=["Eyal", "Roye"], duration_minutes=45,
                sensitivity="founders", extracted=extracted, meeting_id="m1",
                supersessions=[], linked_threads=threads,
            )
        assert out is not None
        # rich table + the enrichment sections all present
        assert "| # | Task | Area | Owner | Deadline | Priority | Urgency | Ref |" in out
        assert "## Decision Intelligence" in out
        assert "## Per-Category Focus" in out
        assert "## Risks & Blockers" in out
        assert "licence unclear" in out
        # TL;DR fell back to deterministic (no invented content)
        assert "🎯 TL;DR" in out
