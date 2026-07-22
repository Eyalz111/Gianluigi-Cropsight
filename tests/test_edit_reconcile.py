"""Holistic edit-reconcile: the fix for the 2026-07 apply_edits duplication.

Editing a pending summary re-runs an LLM over the whole content; the old
byte-exact matcher let a reworded/re-emitted item slip the match and get
inserted as a NEW row -> duplicates that reached the team (worst case: 43 task
rows for a 24-task meeting after two edits — meeting 4eb553c6). These tests pin
the layered fix: a precision-first matching cascade + idempotent output de-dup +
a deterministic self-healing backstop, all keeping byte-exact as the first tier
so genuinely-distinct items are NEVER merged.
"""
import json
from unittest.mock import patch, MagicMock, AsyncMock

import guardrails.approval_flow as af
from guardrails.edit_reconcile import (
    normalize, jaccard, char_ratio, is_near_dup,
    reconcile_children, dedup_within, find_duplicate_groups,
)


# ---------------------------------------------------------------------------
# Pure module — similarity primitives
# ---------------------------------------------------------------------------
class TestSimilarity:
    def test_normalize_collapses_punct_and_case(self):
        assert normalize("The MVP, delivered!") == normalize("the mvp delivered")

    def test_reworded_variant_is_near_dup(self):
        # The exact Shemer rewording that byte-exact matching missed.
        assert is_near_dup(
            "Connect with Bar Topper at a potential unicorn from Iron Source",
            "Connect with Bar Topper at potential unicorn from Iron Source",
        )

    def test_trivial_rewording_is_near_dup(self):
        assert is_near_dup(
            "Prioritize DevOps and production readiness over legal tasks for the MVP delivery",
            "Prioritize DevOps and production readiness over legal tasks for MVP delivery",
        )

    def test_distinct_but_overlapping_is_not_merged(self):
        # Shares "Follow up with Sara" but genuinely different -> must NOT merge.
        assert not is_near_dup(
            "Follow up with Sara at Banca Intesa about the term sheet",
            "Follow up with Sara's father, not Sara directly",
        )

    def test_identical_after_normalize(self):
        assert is_near_dup("Ship in Q3.", "ship in q3")

    def test_empty_never_matches(self):
        assert not is_near_dup("", "anything")
        assert not is_near_dup("anything", "")


# ---------------------------------------------------------------------------
# Pure module — reconcile_children cascade
# ---------------------------------------------------------------------------
def _tp(title, **kw):
    return {"title": title, **kw}


class TestReconcileChildren:
    def test_exact_index_updates_in_place(self):
        old = [{"id": "a", "title": "Alpha"}, {"id": "b", "title": "Beta"}]
        edited = [{"index": 1, "title": "Alpha v2"}, {"index": 2, "title": "Beta"}]
        plan = reconcile_children(old, edited, lambda t: t.get("title", ""))
        assert [u[0] for u in plan["updates"]] == ["a", "b"]
        assert plan["creates"] == [] and plan["deletes"] == []

    def test_reworded_kept_item_matches_fuzzy_not_created(self):
        # LLM dropped the index AND reworded — byte-exact would have created a dup.
        old = [{"id": "a", "title": "Review the Volcani Institute researcher directory online"}]
        edited = [{"title": "Review Volcani Institute researcher directory online"}]
        plan = reconcile_children(old, edited, lambda t: t.get("title", ""))
        assert [u[0] for u in plan["updates"]] == ["a"]
        assert plan["creates"] == [] and plan["deletes"] == []

    def test_reworded_repeat_collapses_to_one(self):
        # THE Shemer bug: same task emitted twice (once matching, once reworded).
        old = [{"id": "a", "title": "Schedule introductory call with Avi Perl"}]
        edited = [
            {"index": 1, "title": "Schedule introductory call with Avi Perl"},
            {"title": "Schedule an introductory call with Avi Perl"},   # reworded repeat
        ]
        plan = reconcile_children(old, edited, lambda t: t.get("title", ""))
        assert len(plan["updates"]) == 1 and plan["creates"] == [] and plan["deletes"] == []

    def test_genuinely_new_item_is_created(self):
        old = [{"id": "a", "title": "Alpha"}]
        edited = [{"index": 1, "title": "Alpha"}, {"title": "A totally unrelated new task"}]
        plan = reconcile_children(old, edited, lambda t: t.get("title", ""))
        assert len(plan["updates"]) == 1 and len(plan["creates"]) == 1

    def test_removed_item_is_deleted(self):
        old = [{"id": "a", "title": "Alpha"}, {"id": "b", "title": "Beta"}]
        edited = [{"index": 1, "title": "Alpha"}]
        plan = reconcile_children(old, edited, lambda t: t.get("title", ""))
        assert plan["deletes"] == ["b"]

    def test_same_title_different_assignee_not_merged(self):
        # Eyal + Roye both own "Review the directory" -> two legitimate rows.
        old = [
            {"id": "a", "title": "Review the directory", "assignee": "Eyal"},
            {"id": "b", "title": "Review the directory", "assignee": "Roye"},
        ]
        edited = [
            {"index": 1, "title": "Review the directory", "assignee": "Eyal"},
            {"index": 2, "title": "Review the directory", "assignee": "Roye"},
        ]
        plan = reconcile_children(
            old, edited, lambda t: t.get("title", ""),
            secondary_of=lambda t: t.get("assignee", ""),
        )
        assert {u[0] for u in plan["updates"]} == {"a", "b"}
        assert plan["creates"] == [] and plan["deletes"] == []


# ---------------------------------------------------------------------------
# Pure module — dedup_within (extraction-time) + find_duplicate_groups (backstop)
# ---------------------------------------------------------------------------
class TestDedupWithin:
    def test_collapses_reworded_decisions(self):
        items = [
            {"description": "Focus on large-scale agricultural areas, not small farms"},
            {"description": "Focus on large scale agricultural areas not small farms"},
        ]
        kept = dedup_within(items, lambda d: d.get("description", ""))
        assert len(kept) == 1

    def test_keeps_distinct_decisions(self):
        items = [
            {"description": "Do not pursue software patents"},
            {"description": "Focus DevOps on production readiness"},
        ]
        assert len(dedup_within(items, lambda d: d.get("description", ""))) == 2

    def test_task_assignee_guard_keeps_split(self):
        items = [
            {"title": "Review the directory", "assignee": "Eyal"},
            {"title": "Review the directory", "assignee": "Roye"},
        ]
        kept = dedup_within(
            items, lambda t: t.get("title", ""), secondary_of=lambda t: t.get("assignee", "")
        )
        assert len(kept) == 2


class TestFindDuplicateGroups:
    def test_groups_near_dupes_oldest_first(self):
        rows = [
            {"id": "old", "title": "Compile the contact list", "created_at": "2026-07-16T20:29"},
            {"id": "new", "title": "Compile contact list", "created_at": "2026-07-16T20:38"},
            {"id": "solo", "title": "Book the flights", "created_at": "2026-07-16T20:29"},
        ]
        groups = find_duplicate_groups(rows, lambda r: r.get("title", ""))
        assert len(groups) == 1
        assert [r["id"] for r in groups[0]] == ["old", "new"]  # input order preserved


# ---------------------------------------------------------------------------
# Integration — apply_edits no longer duplicates, backstop self-heals
# ---------------------------------------------------------------------------
def _sc(tasks=None, decisions=None, questions=None, follow_ups=None):
    sc = MagicMock()
    sc.get_meeting.return_value = {"summary": "S", "title": "M", "date": "2026-07-16"}
    sc.get_tasks.return_value = tasks or []
    sc.list_decisions.return_value = decisions or []
    sc.list_follow_up_meetings.return_value = follow_ups or []
    sc.get_open_questions.return_value = questions or []
    sc.update_meeting.return_value = None
    sc._serialize_datetime.side_effect = lambda d: (d or None)
    return sc


async def _run(sc, payload):
    with patch.object(af, "supabase_client", sc), \
         patch.object(af, "call_llm", return_value=(json.dumps(payload), None)):
        return await af.apply_edits("m1", [{"op": "rename"}])


class TestApplyEditsNoDuplicate:
    async def test_reworded_repeat_edit_creates_no_duplicate(self):
        """The regression: an edit that re-emits a reworded copy must NOT create
        a second row."""
        sc = _sc(tasks=[{"id": "uuid-a", "meeting_id": "m1",
                         "title": "Schedule introductory call with Avi Perl",
                         "assignee": "Eyal", "priority": "M", "deadline": None,
                         "category": "", "status": "pending"}])
        await _run(sc, {
            "summary": "S", "tasks": [
                {"index": 1, "title": "Schedule introductory call with Avi Perl",
                 "assignee": "Eyal", "priority": "M", "deadline": None,
                 "category": "", "status": "pending"},
                {"title": "Schedule an introductory call with Avi Perl",
                 "assignee": "Eyal", "priority": "M", "deadline": None,
                 "category": "", "status": "pending"},  # reworded repeat
            ], "decisions": [], "follow_ups": [], "open_questions": [],
        })
        # updated in place once; NOTHING recreated (pre-fix this made a dup row)
        assert sc.update_task.call_count == 1
        sc.create_tasks_batch.assert_not_called()

    async def test_kept_question_updated_in_place_not_recreated(self):
        """Questions used to be delete-all+recreate (UUID churn). Now in place."""
        sc = _sc(questions=[{"id": "q-1", "meeting_id": "m1",
                             "question": "Does CropSight need a PhD agronomist?",
                             "raised_by": "Eyal"}])
        await _run(sc, {
            "summary": "S", "tasks": [], "decisions": [], "follow_ups": [],
            "open_questions": [
                {"index": 1, "question": "Does CropSight need a PhD-level agronomist?",
                 "raised_by": "Eyal"},   # lightly edited, index kept
            ],
        })
        # no recreate of the kept question
        sc.create_open_question.assert_not_called()
        # updated in place by id
        sc.client.table.return_value.update.return_value.eq.assert_any_call("id", "q-1")


class TestCollapseBackstop:
    async def test_backstop_archives_task_supersedes_decision_and_alerts(self):
        sc = _sc(
            tasks=[
                {"id": "t-old", "title": "Compile the contact list", "assignee": "Eyal",
                 "created_at": "2026-07-16T20:29"},
                {"id": "t-new", "title": "Compile contact list", "assignee": "Eyal",
                 "created_at": "2026-07-16T20:38"},   # near-dup, newer
            ],
            decisions=[
                {"id": "d-old", "description": "Focus on large-scale agriculture",
                 "created_at": "2026-07-16T20:29"},
                {"id": "d-new", "description": "Focus on large scale agriculture",
                 "created_at": "2026-07-16T20:38"},
            ],
        )
        alert = AsyncMock()
        with patch.object(af, "supabase_client", sc), \
             patch("services.alerting.send_system_alert", alert):
            await af._collapse_duplicate_children("m1")
        # newer task archived, older kept
        sc.update_task.assert_called_once_with("t-new", status="archived")
        # newer decision superseded onto the older keeper
        sc.supersede_decision.assert_called_once_with("d-new", superseded_by="d-old")
        alert.assert_awaited_once()

    async def test_backstop_noop_when_clean(self):
        sc = _sc(tasks=[
            {"id": "a", "title": "Book flights", "assignee": "Eyal", "created_at": "1"},
            {"id": "b", "title": "Draft the deck", "assignee": "Roye", "created_at": "2"},
        ])
        alert = AsyncMock()
        with patch.object(af, "supabase_client", sc), \
             patch("services.alerting.send_system_alert", alert):
            await af._collapse_duplicate_children("m1")
        sc.update_task.assert_not_called()
        sc.supersede_decision.assert_not_called()
        alert.assert_not_awaited()

    async def test_backstop_keeps_eyal_and_roye_split(self):
        # Same-title different-assignee is NOT a duplicate -> never collapsed.
        sc = _sc(tasks=[
            {"id": "e", "title": "Review the directory", "assignee": "Eyal", "created_at": "1"},
            {"id": "r", "title": "Review the directory", "assignee": "Roye", "created_at": "2"},
        ])
        alert = AsyncMock()
        with patch.object(af, "supabase_client", sc), \
             patch("services.alerting.send_system_alert", alert):
            await af._collapse_duplicate_children("m1")
        sc.update_task.assert_not_called()
        alert.assert_not_awaited()


# ===========================================================================
# Review-driven hardening (2026-07-22 code review) — one test per fix
# ===========================================================================
class TestContentTokenDiscriminator:
    """The false-merge risk root: raw Jaccard can't tell a stopword reword from a
    one-content-word change. Content tokens can."""

    def test_stopword_reword_is_dup(self):
        assert is_near_dup("boost the client acquisition process",
                           "boost client acquisition process")

    def test_single_content_word_change_is_not_dup(self):
        # 'Q3' vs 'Q4' — one content token differs -> distinct, must NOT merge.
        assert not is_near_dup("prioritize the North-region pilot in Q3",
                               "prioritize the North-region pilot in Q4")

    def test_content_word_swap_is_not_dup(self):
        assert not is_near_dup("Approved a 12-month runway budget",
                               "Approved a 12-month hiring budget")


class TestFinding1_DbRowsHaveIds:
    """The critical one: the summary-edit path passes id-LESS structured_data;
    apply_edits must still reconcile against the DB rows (which carry ids), or
    every item becomes a create (duplicate-on-edit)."""

    async def test_idless_structured_data_still_matches_db_rows_in_place(self):
        sc = _sc(tasks=[{"id": "uuid-a", "meeting_id": "m1", "title": "Alpha",
                         "assignee": "Eyal", "priority": "M", "deadline": None,
                         "category": "", "status": "pending"}])
        payload = {"summary": "S", "tasks": [
            {"index": 1, "title": "Alpha v2", "assignee": "Eyal", "priority": "M",
             "deadline": None, "category": "", "status": "pending"}],
            "decisions": [], "follow_ups": [], "open_questions": []}
        # structured_data carries an ID-LESS copy (as the real pending content does)
        idless = {"tasks": [{"title": "Alpha", "assignee": "Eyal"}],
                  "decisions": [], "follow_ups": [], "open_questions": []}
        with patch.object(af, "supabase_client", sc), \
             patch.object(af, "call_llm", return_value=(json.dumps(payload), None)):
            await af.apply_edits("m1", [{"op": "rename"}], structured_data=idless)
        # matched the DB row in place; did NOT create a duplicate
        sc.update_task.assert_called_once()
        assert sc.update_task.call_args.args[0] == "uuid-a"
        sc.create_tasks_batch.assert_not_called()


class TestFinding6_Tier2SecondaryGuard:
    def test_tier2_indexless_respects_assignee(self):
        # Two same-title tasks, LLM drops indices and swaps order -> must NOT
        # swap assignees (Tier 2 now checks the secondary key).
        old = [{"id": "e", "title": "Prepare deck", "assignee": "Eyal"},
               {"id": "r", "title": "Prepare deck", "assignee": "Roye"}]
        edited = [{"title": "Prepare deck", "assignee": "Roye"},
                  {"title": "Prepare deck", "assignee": "Eyal"}]
        plan = reconcile_children(old, edited, lambda t: t.get("title", ""),
                                  secondary_of=lambda t: t.get("assignee", ""))
        # Roye's edited item must map to Roye's row, Eyal's to Eyal's
        by_id = {oid: it for oid, it in plan["updates"]}
        assert by_id["r"]["assignee"] == "Roye"
        assert by_id["e"]["assignee"] == "Eyal"
        assert plan["deletes"] == []


class TestFinding2_ExactBeforeFuzzy:
    def test_no_identity_swap_between_similar_originals(self):
        # Reworded items must each land on the original with matching content,
        # never fuzzy-claim a sibling's row.
        old = [{"id": "a", "title": "Review the Q3 revenue report"},
               {"id": "b", "title": "Review the Q4 revenue report"}]
        edited = [{"title": "Review Q4 revenue report"},   # -> b
                  {"title": "Review Q3 revenue report"}]   # -> a
        plan = reconcile_children(old, edited, lambda t: t.get("title", ""))
        by_id = {oid: it for oid, it in plan["updates"]}
        assert "Q4" in by_id["b"]["title"] and "Q3" in by_id["a"]["title"]
        assert plan["deletes"] == []


class TestFinding5_EmptySectionGuard:
    def test_empty_edited_section_preserves_rows(self):
        old = [{"id": "a", "title": "T1"}, {"id": "b", "title": "T2"}]
        plan = reconcile_children(old, [], lambda t: t.get("title", ""))
        assert plan["deletes"] == [] and plan["creates"] == []
        assert plan.get("protected_empty") is True


class TestFinding3_BackstopSecondaryGuards:
    def test_distinct_followups_by_led_by_not_grouped(self):
        rows = [{"id": "1", "title": "Board update", "led_by": "Eyal"},
                {"id": "2", "title": "Board update", "led_by": "Roye"}]
        groups = find_duplicate_groups(rows, lambda f: f.get("title", ""),
                                       secondary_of=lambda f: f.get("led_by", ""))
        assert groups == []

    async def test_backstop_does_not_touch_distinct_followups(self):
        sc = _sc(follow_ups=[
            {"id": "1", "title": "Board update", "led_by": "Eyal", "created_at": "1"},
            {"id": "2", "title": "Board update", "led_by": "Roye", "created_at": "2"},
        ])
        alert = AsyncMock()
        with patch.object(af, "supabase_client", sc), \
             patch("services.alerting.send_system_alert", alert):
            await af._collapse_duplicate_children("m1")
        sc.client.table.return_value.delete.return_value.eq.assert_not_called()
        alert.assert_not_awaited()


class TestFinding8_BackstopDeindexes:
    async def test_collapsed_decision_is_deindexed(self):
        sc = _sc(decisions=[
            {"id": "d-old", "description": "Ship the MVP in Q3", "created_at": "1"},
            {"id": "d-new", "description": "Ship MVP in Q3", "created_at": "2"},
        ])
        alert = AsyncMock()
        deindex = MagicMock()
        with patch.object(af, "supabase_client", sc), \
             patch("services.alerting.send_system_alert", alert), \
             patch("processors.semantic_index.deindex", deindex):
            await af._collapse_duplicate_children("m1")
        sc.supersede_decision.assert_called_once_with("d-new", superseded_by="d-old")
        deindex.assert_called_once_with("decision", "d-new")


# ===========================================================================
# Re-review round 2 (2026-07-22) — fixes for what the first fixes missed
# ===========================================================================
class TestReReviewFixes:
    def test_series_a_vs_b_not_merged(self):
        # 'a' is NOT a stopword — it's the sole distinguisher here.
        assert not is_near_dup("Approve the Series A term sheet",
                               "Approve the Series B term sheet")
        assert not is_near_dup("Prepare the Plan A deck", "Prepare the Plan B deck")
        assert not is_near_dup("Go with Option A", "Go with Option B")

    def test_article_reword_still_merges(self):
        # dropping 'the' (still a stopword) is still a duplicate
        assert is_near_dup("boost the client acquisition process",
                           "boost client acquisition process")

    def test_hebrew_items_sharing_latin_token_not_merged(self):
        # Two distinct Hebrew items embedding the same Latin name must NOT
        # collapse to one via the exact-normalized path.
        assert not is_near_dup("שיחה עם Bar Topper",
                               "מייל ל Bar Topper")

    def test_followup_cascade_respects_led_by(self):
        # The RECONCILE (not just the backstop) must guard follow-ups by led_by,
        # or a distinct same-title follow-up is deleted on an unrelated edit.
        old = [{"id": "e", "title": "Board update", "led_by": "Eyal"},
               {"id": "r", "title": "Board update", "led_by": "Roye"}]
        edited = [{"title": "Board update", "led_by": "Eyal"},
                  {"title": "Board update", "led_by": "Roye"}]
        plan = reconcile_children(old, edited, lambda f: f.get("title", ""),
                                  secondary_of=lambda f: f.get("led_by", ""))
        assert plan["deletes"] == [] and plan["creates"] == []
        assert {oid for oid, _ in plan["updates"]} == {"e", "r"}

    def test_question_cascade_respects_raised_by(self):
        old = [{"id": "a", "question": "Do we need a PhD agronomist?", "raised_by": "Eyal"},
               {"id": "b", "question": "Do we need a PhD agronomist?", "raised_by": "Paolo"}]
        edited = [{"question": "Do we need a PhD agronomist?", "raised_by": "Eyal"},
                  {"question": "Do we need a PhD agronomist?", "raised_by": "Paolo"}]
        plan = reconcile_children(old, edited, lambda q: q.get("question", ""),
                                  secondary_of=lambda q: q.get("raised_by", ""))
        assert plan["deletes"] == [] and plan["creates"] == []
