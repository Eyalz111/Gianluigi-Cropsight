"""
Tier 3.1 (narrow) — approval_status column + central-helper filtering.

Verifies that:
- get_tasks / list_decisions / get_open_questions / list_follow_up_meetings
  filter to approval_status='approved' by default.
- include_pending=True returns all rows (pending + approved).
- Extraction inserts get approval_status='pending' via the DB column default.
- guardrails/approval_flow._promote_children_to_approved flips all 4 tables
  from pending → approved and is idempotent + handles empty meetings.
- QA scheduler safety-net check (written in Phase 3) detects approved meetings
  with pending children and ignores the clean state. (These tests are added
  here so they live next to their subject; they exercise Phase 3 code.)

Runs against live Supabase (skips if creds not configured), same pattern
as tests/test_rls_coverage.py and tests/test_tier3_cascade_fks.py.
"""

from datetime import datetime, timezone

import pytest


def _require_supabase():
    """Skip the test if Supabase isn't configured locally."""
    try:
        from services.supabase_client import supabase_client
        from config.settings import settings
    except Exception as e:
        pytest.skip(f"Cannot import Supabase client ({e})")

    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        pytest.skip("SUPABASE_URL / SUPABASE_KEY not configured")

    return supabase_client


def _insert_meeting(sb, title_suffix: str) -> str:
    """Insert a throwaway meeting and return its UUID."""
    meeting = sb.create_meeting(
        date=datetime.now(timezone.utc),
        title=f"[T3.1 TEST] {title_suffix}",
        participants=["tester"],
        raw_transcript="test transcript",
        summary="test summary",
        sensitivity="founders",
        source_file_path=f"test_tier3_approval_{title_suffix}.txt",
    )
    return meeting["id"]


def _set_approval_status(sb, table: str, fk_col: str, meeting_id: str, status: str):
    """Bulk-set approval_status on a child table for a meeting."""
    sb.client.table(table).update({"approval_status": status}).eq(fk_col, meeting_id).execute()


def _cleanup(sb, meeting_id: str):
    """Hard-delete the throwaway meeting (FK CASCADE handles children)."""
    try:
        sb.client.table("embeddings").delete().eq("source_id", meeting_id).execute()
    except Exception:
        pass
    try:
        sb.client.table("meetings").delete().eq("id", meeting_id).execute()
    except Exception:
        pass


class TestHelperFiltering:
    def test_get_tasks_filters_pending_by_default(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "get_tasks_default")
        try:
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 pending task", "assignee": "tester", "priority": "M"},
            ])
            # DB column default should set this one to 'pending'.
            # Manually add an 'approved' sibling via direct update.
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 approved task", "assignee": "tester", "priority": "M"},
            ])
            # Flip just one of them to 'approved'.
            sb.client.table("tasks").update({"approval_status": "approved"}).eq(
                "meeting_id", meeting_id
            ).eq("title", "T3.1 approved task").execute()

            approved_only = [
                t for t in sb.get_tasks(limit=500)
                if t.get("meeting_id") == meeting_id
            ]
            assert len(approved_only) == 1
            assert approved_only[0]["title"] == "T3.1 approved task"
        finally:
            _cleanup(sb, meeting_id)

    def test_get_tasks_include_pending_returns_all(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "get_tasks_include_pending")
        try:
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 pending include", "assignee": "tester", "priority": "M"},
                {"title": "T3.1 approved include", "assignee": "tester", "priority": "M"},
            ])
            sb.client.table("tasks").update({"approval_status": "approved"}).eq(
                "meeting_id", meeting_id
            ).eq("title", "T3.1 approved include").execute()

            all_tasks = [
                t for t in sb.get_tasks(limit=500, include_pending=True)
                if t.get("meeting_id") == meeting_id
            ]
            assert len(all_tasks) == 2
            titles = {t["title"] for t in all_tasks}
            assert "T3.1 pending include" in titles
            assert "T3.1 approved include" in titles
        finally:
            _cleanup(sb, meeting_id)

    def test_list_decisions_filters_pending_by_default(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "list_decisions_default")
        try:
            sb.create_decisions_batch(meeting_id, [
                {"description": "T3.1 pending decision"},
                {"description": "T3.1 approved decision"},
            ])
            sb.client.table("decisions").update({"approval_status": "approved"}).eq(
                "meeting_id", meeting_id
            ).eq("description", "T3.1 approved decision").execute()

            approved_only = [
                d for d in sb.list_decisions(meeting_id=meeting_id)
            ]
            assert len(approved_only) == 1
            assert approved_only[0]["description"] == "T3.1 approved decision"

            all_decisions = [
                d for d in sb.list_decisions(meeting_id=meeting_id, include_pending=True)
            ]
            assert len(all_decisions) == 2
        finally:
            _cleanup(sb, meeting_id)

    def test_get_open_questions_filters_pending_by_default(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "get_open_questions_default")
        try:
            sb.create_open_questions_batch(meeting_id, [
                {"question": "T3.1 pending Q?", "raised_by": "tester"},
                {"question": "T3.1 approved Q?", "raised_by": "tester"},
            ])
            sb.client.table("open_questions").update({"approval_status": "approved"}).eq(
                "meeting_id", meeting_id
            ).eq("question", "T3.1 approved Q?").execute()

            approved_only = sb.get_open_questions(meeting_id=meeting_id)
            assert len(approved_only) == 1
            assert approved_only[0]["question"] == "T3.1 approved Q?"

            all_q = sb.get_open_questions(meeting_id=meeting_id, include_pending=True)
            assert len(all_q) == 2
        finally:
            _cleanup(sb, meeting_id)

    def test_list_follow_up_meetings_filters_pending_by_default(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "list_follow_ups_default")
        try:
            sb.client.table("follow_up_meetings").insert([
                {
                    "source_meeting_id": meeting_id,
                    "title": "T3.1 pending follow-up",
                    "led_by": "tester",
                },
                {
                    "source_meeting_id": meeting_id,
                    "title": "T3.1 approved follow-up",
                    "led_by": "tester",
                },
            ]).execute()
            sb.client.table("follow_up_meetings").update({"approval_status": "approved"}).eq(
                "source_meeting_id", meeting_id
            ).eq("title", "T3.1 approved follow-up").execute()

            approved_only = sb.list_follow_up_meetings(source_meeting_id=meeting_id)
            assert len(approved_only) == 1
            assert approved_only[0]["title"] == "T3.1 approved follow-up"

            all_fu = sb.list_follow_up_meetings(
                source_meeting_id=meeting_id, include_pending=True
            )
            assert len(all_fu) == 2
        finally:
            _cleanup(sb, meeting_id)

    def test_extraction_writes_pending_status_by_default(self):
        """
        The batch-insert paths don't set approval_status explicitly; they
        rely on the DB column default. New rows must land as 'pending'.
        """
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "extraction_default")
        try:
            created = sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 default check", "assignee": "tester", "priority": "M"},
            ])
            task_id = created[0]["id"]
            r = (
                sb.client.table("tasks")
                .select("approval_status")
                .eq("id", task_id)
                .limit(1)
                .execute()
            )
            assert r.data[0]["approval_status"] == "pending"
        finally:
            _cleanup(sb, meeting_id)


class TestPromoteChildrenToApproved:
    def test_promote_flips_all_four_tables(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "promote_all_four")
        try:
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 promote task", "assignee": "tester", "priority": "M"},
            ])
            sb.create_decisions_batch(meeting_id, [
                {"description": "T3.1 promote decision"},
            ])
            sb.create_open_questions_batch(meeting_id, [
                {"question": "T3.1 promote Q?", "raised_by": "tester"},
            ])
            sb.client.table("follow_up_meetings").insert({
                "source_meeting_id": meeting_id,
                "title": "T3.1 promote follow-up",
                "led_by": "tester",
            }).execute()

            # Sanity — everything starts 'pending'
            for table, fk in [
                ("tasks", "meeting_id"),
                ("decisions", "meeting_id"),
                ("open_questions", "meeting_id"),
                ("follow_up_meetings", "source_meeting_id"),
            ]:
                r = sb.client.table(table).select("approval_status").eq(fk, meeting_id).execute()
                assert all(row["approval_status"] == "pending" for row in r.data), (
                    f"{table} should start as pending"
                )

            # Act
            from guardrails.approval_flow import _promote_children_to_approved
            _promote_children_to_approved(meeting_id)

            # Assert — all four tables flipped
            for table, fk in [
                ("tasks", "meeting_id"),
                ("decisions", "meeting_id"),
                ("open_questions", "meeting_id"),
                ("follow_up_meetings", "source_meeting_id"),
            ]:
                r = sb.client.table(table).select("approval_status").eq(fk, meeting_id).execute()
                assert r.data, f"{table} should have at least one row"
                assert all(row["approval_status"] == "approved" for row in r.data), (
                    f"{table} should be approved after promote"
                )
        finally:
            _cleanup(sb, meeting_id)

    def test_promote_is_idempotent(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "promote_idempotent")
        try:
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 idempotent", "assignee": "tester", "priority": "M"},
            ])
            from guardrails.approval_flow import _promote_children_to_approved
            _promote_children_to_approved(meeting_id)
            _promote_children_to_approved(meeting_id)  # second call should no-op safely

            r = sb.client.table("tasks").select("approval_status").eq(
                "meeting_id", meeting_id
            ).execute()
            assert all(row["approval_status"] == "approved" for row in r.data)
        finally:
            _cleanup(sb, meeting_id)

    def test_promote_handles_empty_meeting(self):
        """Meeting with zero children — promote should no-op without error."""
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "promote_empty")
        try:
            from guardrails.approval_flow import _promote_children_to_approved
            # Should not raise.
            _promote_children_to_approved(meeting_id)
        finally:
            _cleanup(sb, meeting_id)


class TestQaSafetyNet:
    """
    Phase 3 tests for _check_approved_meetings_with_pending_children.
    Added here so they live next to the T3.1 subject they're defending.
    """

    def test_safety_net_detects_approved_meeting_with_pending_children(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "safety_net_dirty")
        try:
            # Flip meeting to approved; leave children pending (simulates
            # a partial _promote_children_to_approved failure).
            sb.client.table("meetings").update({
                "approval_status": "approved"
            }).eq("id", meeting_id).execute()

            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 safety-net pending task", "assignee": "tester", "priority": "M"},
            ])
            # Task is pending by default.

            from schedulers.qa_scheduler import _check_approved_meetings_with_pending_children
            issues: list[str] = []
            result = _check_approved_meetings_with_pending_children(issues)

            assert result["inconsistent_meetings"] >= 1
            # At least one detail entry should reference our meeting_id
            mids = {d.get("meeting_id") for d in result.get("details", [])}
            assert meeting_id in mids
            # Issue message should mention our meeting
            assert any(meeting_id[:8] in msg for msg in issues), (
                f"Expected issue message to contain {meeting_id[:8]}; got: {issues}"
            )
        finally:
            _cleanup(sb, meeting_id)

    def test_safety_net_ignores_clean_state(self):
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "safety_net_clean")
        try:
            sb.client.table("meetings").update({
                "approval_status": "approved"
            }).eq("id", meeting_id).execute()

            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 safety-net clean task", "assignee": "tester", "priority": "M"},
            ])
            # Promote to approved so parent + child are consistent.
            sb.client.table("tasks").update({"approval_status": "approved"}).eq(
                "meeting_id", meeting_id
            ).execute()

            from schedulers.qa_scheduler import _check_approved_meetings_with_pending_children
            issues: list[str] = []
            result = _check_approved_meetings_with_pending_children(issues)

            # This specific meeting must not be flagged (it's clean).
            mids = {d.get("meeting_id") for d in result.get("details", [])}
            assert meeting_id not in mids, (
                f"Clean meeting {meeting_id} should not be flagged by safety-net"
            )
        finally:
            _cleanup(sb, meeting_id)

    def test_safety_net_ignores_pending_meetings(self):
        """
        The safety-net check only scans meetings with approval_status='approved'.
        A meeting in 'pending' state with pending children is not an inconsistency
        — it's in-flight work that hasn't been approved yet.
        """
        sb = _require_supabase()
        meeting_id = _insert_meeting(sb, "safety_net_pending_meeting")
        try:
            # _insert_meeting creates with approval_status='pending' by default.
            sb.create_tasks_batch(meeting_id, [
                {"title": "T3.1 safety-net pending meeting task", "assignee": "tester", "priority": "M"},
            ])

            from schedulers.qa_scheduler import _check_approved_meetings_with_pending_children
            issues: list[str] = []
            result = _check_approved_meetings_with_pending_children(issues)

            # Must NOT be flagged — the meeting is pending, not approved.
            mids = {d.get("meeting_id") for d in result.get("details", [])}
            assert meeting_id not in mids
        finally:
            _cleanup(sb, meeting_id)
