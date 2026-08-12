"""One action, extracted twice, filed under two projects.

From the Jonathan Greenwald meeting of 2026-08-12. The model emitted the same
task twice — identical for 130 characters, differing only in the trailing
rationale — and cross_reference only ever compared tasks ACROSS meetings. Both
landed, under DIFFERENT projects, so the same work would draw a bar on two rows
of the Gantt.

Decisions got this treatment on 2026-08-06 after the same failure. Tasks did not.
"""
from unittest.mock import MagicMock, patch

import pytest

from processors.cross_reference import dedupe_tasks_within_meeting

# The real pair, verbatim from the database.
A = ("Run the model separately on NASA Landsat and European Copernicus data sets "
     "and analyze where the two diverge — Jonathan requested this analysis to "
     "assess confidence in regional coverage")
B = ("Run the model separately on NASA Landsat and European Copernicus data sets "
     "and analyze where the two diverge — Eyal confirmed this is wanted but has "
     "not been done yet, and it affects confidence in regional coverage")


class TestTheRealPair:
    def test_the_two_collapse_to_one(self):
        out = dedupe_tasks_within_meeting([{"title": A}, {"title": B}])
        assert len(out) == 1

    def test_the_longer_phrasing_survives(self):
        """It carries more context, and the shorter one is usually the padded
        restatement rather than the other way round."""
        out = dedupe_tasks_within_meeting([{"title": A}, {"title": B}])
        assert out[0]["title"] == max(A, B, key=len)

    def test_plain_jaccard_alone_would_not_have_caught_it(self):
        """Documents WHY containment scoring is needed: each phrasing pads
        differently, so the union grows and Jaccard sinks below the threshold.
        If this ever starts passing on Jaccard alone the guard can be simplified
        — until then, removing containment silently reopens the bug."""
        from processors.cross_reference import _decision_tokens
        ta, tb = _decision_tokens(A), _decision_tokens(B)
        jaccard = len(ta & tb) / len(ta | tb)
        contain = len(ta & tb) / min(len(ta), len(tb))
        assert jaccard < 0.65, f"jaccard {jaccard:.2f} — containment now redundant"
        assert contain >= 0.65, f"containment {contain:.2f}"


class TestItDoesNotOverReach:
    def test_two_genuinely_different_tasks_both_survive(self):
        out = dedupe_tasks_within_meeting([
            {"title": "Send CropSight investment materials to Jonathan Greenwald"},
            {"title": "Assess whether soil carbon depletion can be detected from "
                      "satellite imagery across years"},
        ])
        assert len(out) == 2

    def test_a_terse_task_is_not_swallowed_by_a_longer_unrelated_one(self):
        """Containment is guarded to >=4 content words for exactly this: a short
        title appearing inside a longer one is not evidence of duplication."""
        out = dedupe_tasks_within_meeting([
            {"title": "Send the deck"},
            {"title": "Send the deck to Jonathan, then follow up with Paolo about "
                      "the Milan office lease and the Q4 hiring plan"},
        ])
        assert len(out) == 2

    def test_tasks_without_titles_are_left_alone(self):
        out = dedupe_tasks_within_meeting([{"title": ""}, {"title": ""}])
        assert len(out) == 2


class TestNothingIsLostInTheMerge:
    def test_the_duplicate_fills_gaps_in_the_survivor(self):
        """A shorter phrasing often carries the assignee or deadline the longer
        one omitted; dropping it outright would lose that."""
        out = dedupe_tasks_within_meeting([
            {"title": A, "assignee": "", "deadline": None, "priority": "H"},
            {"title": B, "assignee": "Roye Tadmor", "deadline": "2026-09-01"},
        ])
        assert len(out) == 1
        assert out[0]["assignee"] == "Roye Tadmor"
        assert out[0]["deadline"] == "2026-09-01"
        assert out[0]["priority"] == "H", "the survivor's own fields are kept"

    def test_the_input_is_not_mutated(self):
        rows = [{"title": A}, {"title": B}]
        dedupe_tasks_within_meeting(rows)
        assert len(rows) == 2


class TestItRunsAtTheChokePoint:
    def _batch(self, tasks, dedup_raises=False):
        from services.supabase_client import supabase_client

        inserted = {}
        table = MagicMock()

        def _insert(payload):
            inserted["rows"] = payload
            t = MagicMock()
            t.execute.return_value = MagicMock(
                data=[{"id": f"t{i}", **r} for i, r in enumerate(payload)])
            return t
        table.insert.side_effect = _insert
        table.select.return_value = table
        table.eq.return_value = table
        table.limit.return_value = table
        table.execute.return_value = MagicMock(data=[])
        client = MagicMock()
        client.table.return_value = table

        patches = [
            patch.object(type(supabase_client), "client",
                         property(lambda self: client)),
            patch.object(supabase_client, "get_areas", return_value=[]),
            patch.object(supabase_client, "list_team_members", return_value=[]),
            patch.object(supabase_client, "get_canonical_projects", return_value=[]),
            patch.object(supabase_client, "get_topic_project_links", return_value={}),
        ]
        if dedup_raises:
            patches.append(patch(
                "processors.cross_reference.dedupe_tasks_within_meeting",
                side_effect=RuntimeError("boom")))
        for p in patches:
            p.start()
        try:
            supabase_client.create_tasks_batch("meeting-1", tasks)
        finally:
            for p in reversed(patches):
                p.stop()
        return inserted.get("rows", [])

    def test_the_duplicate_never_reaches_the_database(self):
        rows = self._batch([{"title": A}, {"title": B}])
        assert len(rows) == 1, "both copies were inserted again"

    def test_a_dedup_failure_does_not_lose_the_meetings_tasks(self):
        """Dedup is an improvement, not a gate: losing it costs a duplicate row
        Eyal can delete, while aborting the batch would lose everything the
        meeting produced."""
        rows = self._batch([{"title": A}, {"title": B}], dedup_raises=True)
        assert len(rows) == 2, "the batch should still insert"


class TestOverReachTheSuiteCaught:
    """Both of these shipped briefly and two unrelated tests caught them. They
    are the reason this dedup is not just "compare and drop"."""

    def test_the_batch_order_is_preserved(self):
        """Longest-first decides who survives; it must not decide the ORDER.
        Emitting in length order silently reshuffles every batch — the rows
        arrive in the sequence the meeting discussed them, and the sheet shows
        them in insert order. Caught by test_task_categories, which asserts
        which category lands in row 0."""
        out = dedupe_tasks_within_meeting([
            {"title": "Build API", "category": "PRODUCT & TECHNOLOGY"},
            {"title": "Call investor at length about the round",
             "category": "SALES & BUSINESS DEVELOPMENT"},
        ])
        assert [t["category"] for t in out] == [
            "PRODUCT & TECHNOLOGY", "SALES & BUSINESS DEVELOPMENT"]

    def test_numbered_siblings_are_not_merged(self):
        """"T3.2 direct-delete task 1" and "...task 2" both reduce to
        {direct-delete, task} — the numeric suffix is below the tokeniser's
        length floor — so they scored a perfect 1.0 while naming two different
        jobs. Caught by test_tier3_cascade_fks, which counts rows."""
        out = dedupe_tasks_within_meeting([
            {"title": "T3.2 direct-delete task 1"},
            {"title": "T3.2 direct-delete task 2"},
        ])
        assert len(out) == 2

    def test_short_titles_never_merge_at_all(self):
        """Four content words is the floor for any comparison. Below it there is
        not enough evidence to call two things the same."""
        out = dedupe_tasks_within_meeting([
            {"title": "Send deck"}, {"title": "Send deck"},
        ])
        assert len(out) == 2, "identical but too short to be evidence"

    def test_the_real_duplicate_still_merges(self):
        """The guard must not have cost us the case it was built for."""
        assert len(dedupe_tasks_within_meeting([{"title": A}, {"title": B}])) == 1
