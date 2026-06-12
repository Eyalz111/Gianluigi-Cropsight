"""
Tests for the cross-tier leak fixes (audit P2-01 / P2-02 / P2-03).

Invariant I3: any team-facing output must drop CEO-tier (4) content; the team cap
is FOUNDERS (3). These tests assert CEO-tagged items never reach the team digest,
the team meeting-summary email (body OR .docx), or the weekly-review team digest —
while founders/team items still go through, and Eyal's own paths are unaffected.

Hermetic: all I/O collaborators patched; no live Google/Telegram/DB.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_SECRET = "SUPERSECRET-acquisition-counter"


# =============================================================================
# P2-01 — weekly digest
# =============================================================================

class TestWeeklyDigestTierCap:
    @pytest.mark.asyncio
    async def test_get_task_summary_tier_cap_drops_ceo_from_lists_and_rollup(self):
        import processors.weekly_digest as wd

        def _get_tasks(status=None, **kw):
            if status == "done":
                return [{"title": "ship docs", "sensitivity": "founders", "category": "A", "urgency": "M"}]
            if status == "overdue":
                return [
                    {"title": _SECRET, "sensitivity": "ceo", "category": "A", "urgency": "H"},
                    {"title": "team task", "sensitivity": "team", "category": "B", "urgency": "L"},
                ]
            return []  # pending

        with patch.object(wd, "supabase_client") as mock_sc, \
             patch.object(wd, "settings") as mock_settings:
            mock_settings.OUTPUTS_PRIORITY_URGENCY_AREA_ENABLED = True
            mock_sc.get_tasks.side_effect = _get_tasks

            capped = await wd.get_task_summary(tier_cap=3)
            uncapped = await wd.get_task_summary()

        capped_overdue = [t["title"] for t in capped["overdue"]]
        assert _SECRET not in capped_overdue
        assert "team task" in capped_overdue
        # the CEO task was the only High-urgency one → cap zeroes that rollup bucket
        assert capped["by_urgency"]["H"] == 0
        # Eyal-only (no cap) still sees it, in the list AND the rollup
        assert any(t["title"] == _SECRET for t in uncapped["overdue"])
        assert uncapped["by_urgency"]["H"] == 1

    @pytest.mark.asyncio
    async def test_generate_weekly_digest_excludes_ceo_decisions_and_questions(self):
        import processors.weekly_digest as wd

        with patch.object(wd, "get_meetings_for_week", AsyncMock(return_value=[])), \
             patch.object(wd, "get_decisions_for_week", AsyncMock(return_value=[
                 {"description": _SECRET, "sensitivity": "ceo"},
                 {"description": "Move the demo to next week", "sensitivity": "founders"},
             ])), \
             patch.object(wd, "get_task_summary", AsyncMock(return_value={
                 "completed_this_week": [], "overdue": [], "due_next_week": []})), \
             patch.object(wd, "get_open_questions_summary", AsyncMock(return_value=[
                 {"question": _SECRET, "sensitivity": "ceo"},
                 {"question": "When is the demo?", "sensitivity": "founders"},
             ])), \
             patch.object(wd, "get_upcoming_meetings", AsyncMock(return_value=[])), \
             patch.object(wd, "get_cross_reference_summary", AsyncMock(return_value={})), \
             patch.object(wd, "get_commitment_scorecard", AsyncMock(return_value={})), \
             patch("processors.proactive_alerts.generate_alerts", return_value=[]), \
             patch("processors.entity_extraction.review_entity_health", return_value={}):
            digest = await wd.generate_weekly_digest()

        doc = digest["digest_document"]
        assert _SECRET not in doc
        assert "Move the demo to next week" in doc or "When is the demo?" in doc


# =============================================================================
# P2-02 — meeting-summary email (body + .docx)
# =============================================================================

class TestTeamSafeSummaryHelper:
    def test_render_team_safe_summary_uses_only_structured_filtered_content(self):
        from guardrails.approval_flow import _render_team_safe_summary

        # team_content is ALREADY filtered (no CEO items); the helper must ignore
        # any free-text prose entirely and emit only the structured items.
        team_content = {
            "decisions": [{"description": "Ship the demo"}],
            "tasks": [{"title": "Prep slides", "assignee": "Paolo"}],
            "open_questions": [{"question": "What date?"}],
        }
        out = _render_team_safe_summary("BD Sync", "2026-06-12", team_content)
        assert "Ship the demo" in out
        assert "Prep slides" in out and "Paolo" in out
        assert "What date?" in out
        assert _SECRET not in out  # nothing prose-derived can sneak in


class TestDistributeApprovedContentTierFilter:
    @pytest.mark.asyncio
    async def test_team_email_drops_ceo_prose_and_items(self):
        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.drive_service") as mock_drive, \
             patch("guardrails.approval_flow.sheets_service") as mock_sheets, \
             patch("guardrails.approval_flow.gmail_service") as mock_gmail, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow.get_distribution_list", return_value=["roye@cropsight.com"]), \
             patch("guardrails.approval_flow.settings") as mock_settings, \
             patch("services.word_generator.generate_summary_docx", return_value=b"docx"):
            mock_settings.ENVIRONMENT = "production"
            mock_db.get_meeting = MagicMock(return_value={"participants": [], "duration_minutes": 30, "summary": "x"})
            mock_db.get_tasks = MagicMock(return_value=[])
            mock_db.log_action = MagicMock(return_value={"id": "l"})
            mock_drive.save_meeting_summary = AsyncMock(return_value={"id": "d", "webViewLink": "http://drive/x"})
            mock_drive.save_meeting_summary_docx = AsyncMock(return_value={"id": "d2", "webViewLink": "http://drive/y"})
            mock_sheets.add_task = AsyncMock(return_value=True)
            mock_gmail.send_meeting_summary = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)
            mock_tg.send_meeting_summary = AsyncMock(return_value=True)

            content = {
                "title": "BD Sync",
                "date": "2026-06-12",
                "summary": f"Pipeline talk. {_SECRET} at $50M.",
                "executive_summary": f"{_SECRET} exec",
                "discussion_summary": f"Long discussion. {_SECRET}.",
                "decisions": [
                    {"description": "Move the demo to next week", "sensitivity": "founders"},
                    {"description": f"{_SECRET} decision", "sensitivity": "ceo"},
                ],
                "tasks": [
                    {"title": "Prep demo", "assignee": "Paolo", "sensitivity": "founders"},
                    {"title": f"{_SECRET} task", "assignee": "Eyal", "sensitivity": "ceo"},
                ],
                "open_questions": [
                    {"question": "When is the demo?", "sensitivity": "founders"},
                    {"question": f"{_SECRET} question", "sensitivity": "ceo"},
                ],
                "follow_ups": [],
                "stakeholders": [],
            }

            from guardrails.approval_flow import distribute_approved_content
            await distribute_approved_content("m-1", content, sensitivity="founders")

        mock_gmail.send_meeting_summary.assert_awaited_once()
        kw = mock_gmail.send_meeting_summary.call_args.kwargs
        # No CEO content in the prose body, discussion, exec, or structured tasks
        assert _SECRET not in kw["summary_content"]
        assert _SECRET not in kw.get("discussion_summary", "")
        assert _SECRET not in kw.get("executive_summary", "")
        assert all(_SECRET not in (t.get("title", "")) for t in kw.get("tasks", []))
        # Founders content still goes out
        assert "Move the demo to next week" in kw["summary_content"]


# =============================================================================
# P2-03 — weekly-review team digest (Drive doc)
# =============================================================================

class TestDistributeApprovedReviewTierFilter:
    @pytest.mark.asyncio
    async def test_review_digest_drops_ceo_decisions_and_tasks(self):
        captured = {}

        async def _save_weekly_digest(week_of, digest_content):
            captured["digest"] = digest_content
            return {"id": "wd", "link": "http://drive/wd"}

        with patch("guardrails.approval_flow.supabase_client") as mock_db, \
             patch("guardrails.approval_flow.drive_service") as mock_drive, \
             patch("guardrails.approval_flow.gmail_service") as mock_gmail, \
             patch("guardrails.approval_flow.comms_spine") as mock_tg, \
             patch("guardrails.approval_flow.settings") as mock_settings:
            mock_settings.ENVIRONMENT = "production"
            mock_settings.WEEKLY_DIGESTS_FOLDER_ID = "folder-1"
            mock_settings.team_emails = ["roye@cropsight.com"]
            mock_settings.EYAL_EMAIL = "eyal@cropsight.com"
            mock_drive.save_weekly_digest = AsyncMock(side_effect=_save_weekly_digest)
            mock_db.get_weekly_review_session = MagicMock(return_value={"report_id": "r1"})
            mock_db.update_weekly_report = MagicMock(return_value={})
            mock_gmail.send_weekly_digest = AsyncMock(return_value=True)
            mock_gmail.send_email = AsyncMock(return_value=True)
            mock_tg.send_to_group = AsyncMock(return_value=True)
            mock_tg.send_to_eyal = AsyncMock(return_value=True)

            agenda_data = {
                "week_in_review": {
                    "meetings_count": 3,
                    "decisions_count": 2,
                    "decisions": [
                        {"description": "Adopt the new pricing page", "sensitivity": "founders"},
                        {"description": f"{_SECRET} decision", "sensitivity": "ceo"},
                    ],
                    "task_summary": {
                        "completed_this_week": [
                            {"title": "Publish blog", "sensitivity": "founders"},
                            {"title": f"{_SECRET} task", "sensitivity": "ceo"},
                        ],
                        "overdue": [],
                    },
                },
            }

            from guardrails.approval_flow import distribute_approved_review
            await distribute_approved_review("s-1", agenda_data, week_number=24, year=2026)

        assert "digest" in captured, "weekly digest was not uploaded"
        assert _SECRET not in captured["digest"]
        assert "Adopt the new pricing page" in captured["digest"]
        assert "Publish blog" in captured["digest"]
