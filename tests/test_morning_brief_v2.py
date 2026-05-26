"""
Tests for the PR2 v2 morning-brief rework: knowledge-aware foresight flags,
decision-first assembly, exception-only system line, ranked overflow, the thin
Haiku headline (with deterministic fallback), and the loose-ends line.

Patches attributes on the real settings singleton / module globals — never
replaces the settings object.
"""

from unittest.mock import MagicMock, patch

import processors.morning_brief as mb


def _mock_topic_rows(rows):
    """Build a supabase_client mock whose topic_threads query returns `rows`."""
    m = MagicMock()
    (m.client.table.return_value
        .select.return_value
        .not_.is_.return_value
        .limit.return_value
        .execute.return_value).data = rows
    return m


# =========================================================================
# _gather_knowledge_flags
# =========================================================================

class TestGatherKnowledgeFlags:
    def test_blocked_and_idle_with_citation_and_tier(self):
        rows = [
            {"id": "t1", "topic_name": "Moldova pilot",
             "brief_json": {"current_status": "blocked", "risks": ["waiting on sign-off"],
                            "narrative": "n", "sensitivity": "founders"}},
            {"id": "t2", "topic_name": "Hiring",
             "brief_json": {"current_status": "stale", "narrative": "no movement",
                            "sensitivity": "team"}},
            {"id": "t3", "topic_name": "Closed thing",
             "brief_json": {"current_status": "closed"}},
        ]
        with patch.object(mb, "supabase_client", _mock_topic_rows(rows)):
            flags = mb._gather_knowledge_flags()
        kinds = {f["topic_name"]: f["kind"] for f in flags}
        assert kinds == {"Moldova pilot": "blocked", "Hiring": "idle"}  # closed excluded
        blocked = next(f for f in flags if f["kind"] == "blocked")
        assert blocked["detail"] == "waiting on sign-off"   # first risk
        assert blocked["citation"] == "t1"                  # source carried
        assert blocked["sensitivity"] == "founders"         # tier carried

    def test_empty_when_no_briefs(self):
        with patch.object(mb, "supabase_client", _mock_topic_rows([])):
            assert mb._gather_knowledge_flags() == []


# =========================================================================
# _assemble_v2_groups / _render_v2
# =========================================================================

class TestAssembleAndRender:
    def test_blocked_to_attention_idle_excluded(self):
        sections = [{"type": "knowledge_flags", "items": [
            {"topic_name": "Moldova", "kind": "blocked", "detail": "sign-off"},
            {"topic_name": "Hiring", "kind": "idle", "detail": "quiet"},
        ]}]
        groups = mb._assemble_v2_groups(sections)
        joined = " ".join(groups["attention"])
        assert "Moldova" in joined
        assert "Hiring" not in joined  # idle handled by loose-ends, not attention

    def test_system_line_silent_when_healthy(self):
        groups = mb._assemble_v2_groups([
            {"type": "system_state", "watcher_status": "ok", "rejected_count": 0,
             "errors_24h": 0, "pending_queue": 0},
        ])
        assert groups["system_line"] == ""

    def test_system_line_surfaces_problems(self):
        with patch.object(mb.settings, "BRIEF_ERROR_THRESHOLD", 3):
            groups = mb._assemble_v2_groups([
                {"type": "system_state", "watcher_status": "stale", "rejected_count": 0,
                 "errors_24h": 5, "pending_queue": 0},
            ])
        assert "watcher stale" in groups["system_line"]
        assert "5 errors" in groups["system_line"]

    def test_error_below_threshold_stays_silent(self):
        with patch.object(mb.settings, "BRIEF_ERROR_THRESHOLD", 3):
            groups = mb._assemble_v2_groups([
                {"type": "system_state", "watcher_status": "ok", "rejected_count": 0,
                 "errors_24h": 1, "pending_queue": 0},
            ])
        assert groups["system_line"] == ""  # 1 < 3, no spurious surfacing

    def test_decision_first_order_and_overflow(self):
        # 8 attention items -> capped at 6, overflow recorded + "+N more"
        att_items = [{"topic_name": f"T{i}", "kind": "blocked", "detail": "x"} for i in range(8)]
        sections = [
            {"type": "calendar", "events": [{"time": "10:00", "title": "Sync"}]},
            {"type": "knowledge_flags", "items": att_items},
        ]
        groups = mb._assemble_v2_groups(sections)
        text, overflow = mb._render_v2(groups, "lead line")
        assert text.index("Needs attention") > text.index("Today")  # decision-first
        assert any(o["section"] == "attention" and o["hidden"] == 2 for o in overflow)
        assert "+2 more" in text

    def test_html_escaped(self):
        sections = [{"type": "knowledge_flags", "items": [
            {"topic_name": "A & B <co>", "kind": "blocked", "detail": "x < y"},
        ]}]
        groups = mb._assemble_v2_groups(sections)
        assert "&amp;" in groups["attention"][0] and "&lt;" in groups["attention"][0]


# =========================================================================
# _compose_lead (thin Haiku headline + deterministic fallback)
# =========================================================================

class TestComposeLead:
    async def test_fallback_on_llm_failure_logs_status(self):
        groups = mb._assemble_v2_groups([{"type": "knowledge_flags", "items": [
            {"topic_name": "Moldova", "kind": "blocked", "detail": "x"},
        ]}])
        with patch("core.llm.call_llm", side_effect=RuntimeError("boom")), \
             patch.object(mb, "_log_headline_status") as mock_log:
            lead = await mb._compose_lead(groups)
        assert lead == mb._deterministic_lead(groups)  # fell back deterministically
        assert mock_log.call_args[0][0].startswith("fallback")

    async def test_uses_llm_output_when_available(self):
        groups = mb._assemble_v2_groups([{"type": "knowledge_flags", "items": [
            {"topic_name": "Moldova", "kind": "blocked", "detail": "x"},
        ]}])
        with patch("core.llm.call_llm", return_value=("Moldova pilot is blocked on sign-off", {})), \
             patch.object(mb, "_log_headline_status") as mock_log:
            lead = await mb._compose_lead(groups)
        assert "Moldova" in lead
        assert mock_log.call_args[0][0] == "success"


# =========================================================================
# _gather_loose_ends
# =========================================================================

class TestLooseEnds:
    def test_none_when_clean(self):
        with patch("processors.deal_intelligence.generate_commitments_due", return_value=[]):
            assert mb._gather_loose_ends(knowledge_flags=[]) is None

    def test_aggregates_commitments_and_idle(self):
        flags = [{"kind": "idle"}, {"kind": "idle"}, {"kind": "blocked"}]
        with patch("processors.deal_intelligence.generate_commitments_due",
                   return_value=[{"x": 1}, {"x": 2}]):
            line = mb._gather_loose_ends(knowledge_flags=flags)
        assert "2 overdue commitments" in line
        assert "2 idle topics" in line
        assert "blocked" not in line  # blocked surfaced individually, not counted here
