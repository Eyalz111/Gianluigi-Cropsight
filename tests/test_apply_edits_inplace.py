"""Phase 1 Step 2 (P2): apply_edits updates tasks IN PLACE (UUIDs survive).

The old apply_edits delete+recreated a meeting's tasks on every edit, minting new
UUIDs. The Sheet rows from the earlier distribution carried the OLD UUIDs, so the
reconcile orphaned them and re-added the new tasks as fresh rows — two rows per
task (the 43-vs-25 count on the 07-06 weekly). These tests pin the new behavior:
a kept/renamed task keeps its UUID (update_task), only genuinely-added tasks are
created, only genuinely-removed tasks are deleted.
"""
import json
from unittest.mock import patch, MagicMock

import guardrails.approval_flow as af


def _sc(tasks):
    sc = MagicMock()
    sc.get_meeting.return_value = {"summary": "S", "title": "M", "date": "2026-07-07"}
    sc.list_decisions.return_value = []
    sc.get_tasks.return_value = tasks
    sc.list_follow_up_meetings.return_value = []
    sc.get_open_questions.return_value = []
    sc.update_meeting.return_value = None
    sc._serialize_datetime.side_effect = lambda d: (d or None)
    return sc


async def _run(sc, llm_tasks):
    payload = {
        "summary": "S", "decisions": [], "tasks": llm_tasks,
        "follow_ups": [], "open_questions": [],
    }
    with patch.object(af, "supabase_client", sc), \
         patch.object(af, "call_llm", return_value=(json.dumps(payload), None)):
        return await af.apply_edits("m1", [{"op": "rename"}])


_ALPHA = {"id": "uuid-a", "meeting_id": "m1", "title": "Alpha", "assignee": "Roye",
          "priority": "H", "deadline": None, "category": "", "status": "pending"}
_BETA = {"id": "uuid-b", "meeting_id": "m1", "title": "Beta", "assignee": "Eyal",
         "priority": "M", "deadline": None, "category": "", "status": "pending"}


class TestApplyEditsInPlace:
    async def test_rename_preserves_uuid_no_recreate(self):
        sc = _sc([dict(_ALPHA), dict(_BETA)])
        await _run(sc, [
            {"index": 1, "title": "Alpha v2", "assignee": "Roye", "priority": "H",
             "deadline": None, "category": "", "status": "pending"},
            {"index": 2, "title": "Beta", "assignee": "Eyal", "priority": "M",
             "deadline": None, "category": "", "status": "pending"},
        ])
        # both existing tasks updated in place — UUIDs survive
        assert sc.update_task.call_count == 2
        ids = {c.args[0] for c in sc.update_task.call_args_list}
        assert ids == {"uuid-a", "uuid-b"}
        a = next(c for c in sc.update_task.call_args_list if c.args[0] == "uuid-a")
        assert a.kwargs["title"] == "Alpha v2"
        # nothing recreated (the duplicate-row bug)
        sc.create_tasks_batch.assert_not_called()

    async def test_added_task_is_created_existing_preserved(self):
        sc = _sc([dict(_ALPHA), dict(_BETA)])
        await _run(sc, [
            {"index": 1, "title": "Alpha", "assignee": "Roye", "priority": "H",
             "deadline": None, "category": "", "status": "pending"},
            {"index": 2, "title": "Beta", "assignee": "Eyal", "priority": "M",
             "deadline": None, "category": "", "status": "pending"},
            {"title": "Gamma (new)", "assignee": "Paolo", "priority": "M",
             "deadline": None, "category": "", "status": "pending"},  # no index -> new
        ])
        assert sc.update_task.call_count == 2  # existing kept in place
        sc.create_tasks_batch.assert_called_once()
        new_batch = sc.create_tasks_batch.call_args.args[1]
        assert len(new_batch) == 1 and new_batch[0]["title"] == "Gamma (new)"

    async def test_removed_task_is_deleted(self):
        sc = _sc([dict(_ALPHA), dict(_BETA)])
        await _run(sc, [
            {"index": 1, "title": "Alpha", "assignee": "Roye", "priority": "H",
             "deadline": None, "category": "", "status": "pending"},
        ])  # Beta dropped by the edit
        assert sc.update_task.call_count == 1  # only Alpha
        assert {c.args[0] for c in sc.update_task.call_args_list} == {"uuid-a"}
        # Beta (uuid-b) deleted by id
        sc.client.table.return_value.delete.return_value.eq.assert_any_call("id", "uuid-b")
        sc.create_tasks_batch.assert_not_called()

    async def test_indexless_kept_task_matches_by_title(self):
        # LLM dropped the index but the title is unchanged -> still update in place
        # (title fallback), never recreate.
        sc = _sc([dict(_ALPHA), dict(_BETA)])
        await _run(sc, [
            {"title": "Alpha", "assignee": "Roye", "priority": "H",
             "deadline": None, "category": "", "status": "pending"},
            {"title": "Beta", "assignee": "Eyal", "priority": "M",
             "deadline": None, "category": "", "status": "pending"},
        ])
        assert sc.update_task.call_count == 2
        assert {c.args[0] for c in sc.update_task.call_args_list} == {"uuid-a", "uuid-b"}
        sc.create_tasks_batch.assert_not_called()
