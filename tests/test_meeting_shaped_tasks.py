"""Tasks that are really meetings. [2026-08-09]

"Coordinate in-person meeting with Calabria region president" was sitting in the
open-task pool the morning this shipped. Nothing was wrong with the extraction —
until the meetings pool moved into the Project Status workbook, a meeting-shaped
commitment had nowhere else to land.

The load-bearing property is that this NEVER MOVES ANYTHING. It raises a
proposal and stops, because "Chase Roye about scheduling the architecture
review" reads exactly like a meeting while being a genuine chase.
"""

from unittest.mock import MagicMock, patch

import pytest

from processors import meeting_shaped_tasks as mst
from processors.meeting_shaped_tasks import looks_like_a_meeting as looks


class TestTheDetector:
    """Both halves must be present — a verb about ARRANGING and a noun meaning a
    MEETING. Requiring both is what keeps "Book the flights" out."""

    @pytest.mark.parametrize("title", [
        "Coordinate a meeting for Yoram's arrival",
        "Schedule a call with Brian Keane",
        "Book a coffee with Marco",
        "Arrange kick-off with NCPB",
        "Organise a demo for the insurance vertical",
        "Line up an interview with the ML candidate",
        "Convene a session on the pricing model",
    ])
    def test_meeting_shaped(self, title):
        assert looks(title) is True

    @pytest.mark.parametrize("title", [
        "Push the NDVI code into github",
        "Book the flights to Milan",            # arranging, but not a meeting
        "Chase Roye about the pricing model",
        "Send the WFP contact an update",
        "",
        "   ",
    ])
    def test_not_meeting_shaped(self, title):
        assert looks(title) is False

    @pytest.mark.parametrize("title", [
        "Prepare the agenda for the board meeting",
        "Send Paolo the deck before the call",
        "Write up notes from the meeting",
        "Present the model accuracy analysis at the review meeting",
        "Prepare a summary and schedule the follow-up call",
    ])
    def test_work_that_merely_happens_around_a_meeting(self, title):
        """Moving these would lose the actual work."""
        assert looks(title) is False

    def test_position_decides_the_exclusion_not_presence(self):
        """"review" appears in the NAME of the meeting here, not as the work.
        Matching the exclusion anywhere killed this real title — the exact kind
        of false negative nobody notices, because it looks like the detector
        simply found nothing."""
        assert looks("Set up the architecture review session with Eyal Zamir") is True
        assert looks("Review the deck before we set up the call") is False


class TestLongTitlesAreJudgedOnTheirHead:
    """Extraction appends context after an em-dash or in parentheses. That
    trailer is elaboration, not additional work — a real title ran 28 words
    while its actual commitment was six."""

    @pytest.mark.parametrize("title", [
        "Schedule introductory call with Avi Perl (retired grape breeder, Big "
        "Perl variety creator) — discuss grape variety data and yield history",
        "Coordinate in-person meeting with Calabria region president — "
        "determine attendees and language support needed for the session",
        "Schedule an introductory call with Roberta Garibaldi (Professor of "
        "Tourism Business Management, University of Bergamo)",
    ])
    def test_a_long_title_with_a_short_commitment_is_caught(self, title):
        assert looks(title) is True

    def test_a_genuinely_sprawling_head_is_not(self):
        """No trailer to strip — this really is a body of work."""
        assert looks(
            "Schedule a meeting to discuss the full commercial rollout plan "
            "across Italy, Kenya, Moldova and every remaining target market"
        ) is False


class TestItOnlyEverProposes:
    def _sb(self, tasks, pending=None):
        sb = MagicMock()
        sb.get_tasks.side_effect = lambda **kw: (
            tasks if kw.get("status") == "pending" else [])
        sb.get_pending_approvals_by_status.return_value = pending or []
        return sb

    def test_a_meeting_shaped_task_becomes_a_proposal(self):
        sb = self._sb([{"id": "t1", "title": "Schedule a call with Brian",
                        "assignee": "Eyal Zror", "deadline": "2026-08-20"}])
        with patch.object(mst, "supabase_client", sb):
            out = mst.propose_meeting_shaped_tasks()
        assert out["proposed"] == 1
        kw = sb.create_pending_approval.call_args.kwargs
        assert kw["content_type"] == "task_is_a_meeting"
        assert kw["content"]["task_id"] == "t1"

    def test_nothing_is_moved_closed_or_created(self):
        """The whole point. A keyword match must not remove work from the
        review with no human in the loop."""
        sb = self._sb([{"id": "t1", "title": "Schedule a call with Brian"}])
        with patch.object(mst, "supabase_client", sb):
            mst.propose_meeting_shaped_tasks()
        sb.update_task.assert_not_called()
        sb.create_follow_up_meeting_manual.assert_not_called()

    def test_an_ordinary_task_is_left_alone(self):
        sb = self._sb([{"id": "t1", "title": "Push the NDVI code into github"}])
        with patch.object(mst, "supabase_client", sb):
            out = mst.propose_meeting_shaped_tasks()
        assert out["proposed"] == 0
        sb.create_pending_approval.assert_not_called()

    def test_it_never_proposes_the_same_task_twice(self):
        sb = self._sb(
            [{"id": "t1", "title": "Schedule a call with Brian"}],
            pending=[{"content_type": "task_is_a_meeting",
                      "content": {"task_id": "t1"}}])
        with patch.object(mst, "supabase_client", sb):
            out = mst.propose_meeting_shaped_tasks()
        assert out["proposed"] == 0

    def test_it_is_capped_and_says_what_it_left(self):
        tasks = [{"id": f"t{i}", "title": f"Schedule call {i} with Brian"}
                 for i in range(9)]
        sb = self._sb(tasks)
        with patch.object(mst, "supabase_client", sb):
            out = mst.propose_meeting_shaped_tasks(max_proposals=3)
        assert out["proposed"] == 3 and out["candidates"] == 9

    def test_a_read_failure_is_not_an_exception(self):
        """Scheduler-safe: it runs unattended."""
        sb = MagicMock()
        sb.get_tasks.side_effect = RuntimeError("supabase down")
        with patch.object(mst, "supabase_client", sb):
            out = mst.propose_meeting_shaped_tasks()
        assert out["proposed"] == 0


class TestApprovingOne:
    _DEFAULT = object()

    def _sb(self, meeting=_DEFAULT):
        sb = MagicMock()
        sb.create_follow_up_meeting_manual.return_value = (
            {"id": "m-new"} if meeting is self._DEFAULT else meeting)
        return sb

    def _content(self, **kw):
        base = {"task_id": "t1", "title": "Schedule a call with Brian",
                "assignee": "Eyal Zror", "deadline": "2026-08-20",
                "label": "Fundraising", "meeting_id": "src-1"}
        base.update(kw)
        return base

    def test_it_creates_the_meeting_and_closes_the_task(self):
        sb = self._sb()
        with patch.object(mst, "supabase_client", sb):
            out = mst.apply_meeting_shaped_proposal(self._content())
        assert out["ok"] and out["meeting_id"] == "m-new"
        assert sb.update_task.call_args.kwargs["status"] == "cancelled"

    def test_the_task_is_cancelled_never_deleted(self):
        """It carries a source meeting_id, and the citation trail is the point
        of every extracted item."""
        sb = self._sb()
        with patch.object(mst, "supabase_client", sb):
            mst.apply_meeting_shaped_proposal(self._content())
        assert "m-new"[:8] in sb.update_task.call_args.kwargs["status_reason"]

    def test_the_meeting_is_created_before_the_task_is_closed(self):
        """A failure between the two must leave a duplicate, not lose the
        commitment entirely."""
        sb = self._sb()
        sb.update_task.side_effect = RuntimeError("boom")
        with patch.object(mst, "supabase_client", sb):
            out = mst.apply_meeting_shaped_proposal(self._content())
        assert out["ok"] and out["warning"] == "task still open"

    def test_a_failed_create_closes_nothing(self):
        sb = self._sb(meeting=None)
        with patch.object(mst, "supabase_client", sb):
            out = mst.apply_meeting_shaped_proposal(self._content())
        assert out["ok"] is False
        sb.update_task.assert_not_called()

    def test_an_empty_proposal_is_refused(self):
        sb = self._sb()
        with patch.object(mst, "supabase_client", sb):
            assert mst.apply_meeting_shaped_proposal({})["ok"] is False
            assert mst.apply_meeting_shaped_proposal(None)["ok"] is False


class TestItIsReachableFromDecideProposal:
    def test_the_proposal_type_is_wired_in(self):
        """A proposal nothing can action is worse than no proposal."""
        import pathlib
        src = pathlib.Path("services/mcp_server.py").read_text(encoding="utf-8")
        assert "task_is_a_meeting" in src
        assert "apply_meeting_shaped_proposal" in src
