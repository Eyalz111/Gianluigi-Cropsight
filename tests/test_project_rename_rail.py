"""The Projects-tab rename rail, and merge_canonical_projects.

`test_a_db_side_rename_is_not_reverted` is the one that matters: until
2026-08-07 reconcile_projects compared the sheet's name cell straight against
the database, so it could not tell a human's edit from a stale cell. Any rename
made through MCP, Telegram or a migration script was undone on the next
30-minute tick — and because rename_canonical_project backfills `label` across
five tables, the revert propagated through all of them.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from processors import sheets_sync


def _sheet_row(pid="p1", name="Fundraising & Investors", row=2, **kw):
    row_dict = {"id": pid, "name": name, "row_number": row, "aliases": "",
                "area": "", "description": ""}
    row_dict.update(kw)
    return row_dict


def _db_project(pid="p1", name="Fundraising & Investors", **kw):
    row = {"id": pid, "name": name, "aliases": [], "area_id": None,
           "description": "", "status": "active"}
    row.update(kw)
    return row


async def _run(sheet_rows, db_projects, snapshots, write=True):
    sb = MagicMock()
    sb.get_canonical_projects.return_value = db_projects
    sb.get_project_snapshots.return_value = snapshots
    sb.get_areas.return_value = []
    svc = MagicMock()
    svc.get_all_projects = AsyncMock(return_value=sheet_rows)
    svc._update_cell = AsyncMock()

    with patch.object(sheets_sync, "supabase_client", sb), \
         patch("services.google_sheets.sheets_service", svc), \
         patch.object(sheets_sync.settings, "PROJECTS_RECONCILE_ENABLED", True), \
         patch.object(sheets_sync.settings, "TASK_TRACKER_SHEET_ID", "sid"):
        summary = await sheets_sync.reconcile_projects(shadow=not write)
    return summary, sb, svc


class TestRenameRail:
    @pytest.mark.asyncio
    async def test_a_db_side_rename_is_not_reverted(self):
        """DB says the new name, the sheet cell is stale, the snapshot agrees
        with the sheet. That is the DB advancing — refresh the cell."""
        summary, sb, svc = await _run(
            [_sheet_row(name="EU Grant")],
            [_db_project(name="Grants & Contributions")],
            {"p1": {"title": "EU Grant"}},
        )
        sb.rename_canonical_project.assert_not_called()
        assert summary["renamed"] == 0
        assert summary.get("name_refreshed") == 1
        svc._update_cell.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_human_rename_in_the_sheet_still_renames(self):
        """The feature must survive the fix: cell differs from BOTH the
        snapshot and the DB, so a person typed it."""
        summary, sb, _ = await _run(
            [_sheet_row(name="Grants & Contributions")],
            [_db_project(name="EU Grant")],
            {"p1": {"title": "EU Grant"}},
        )
        sb.rename_canonical_project.assert_called_once_with(
            "p1", "Grants & Contributions")
        assert summary["renamed"] == 1

    @pytest.mark.asyncio
    async def test_no_snapshot_never_renames(self):
        """A rename mutates five tables. Never on incomplete evidence."""
        summary, sb, _ = await _run(
            [_sheet_row(name="Something Else")],
            [_db_project(name="EU Grant")],
            {},
        )
        sb.rename_canonical_project.assert_not_called()
        assert summary["renamed"] == 0
        assert summary.get("unbased_divergence") == 1

    @pytest.mark.asyncio
    async def test_no_snapshot_still_seeds_one(self):
        _, sb, _ = await _run(
            [_sheet_row(name="EU Grant")], [_db_project(name="EU Grant")], {})
        sb.upsert_project_snapshot.assert_called()

    @pytest.mark.asyncio
    async def test_agreement_is_a_no_op(self):
        summary, sb, svc = await _run(
            [_sheet_row(name="EU Grant")],
            [_db_project(name="EU Grant")],
            {"p1": {"title": "EU Grant"}},
        )
        sb.rename_canonical_project.assert_not_called()
        assert summary["renamed"] == 0 and not summary.get("name_refreshed")

    @pytest.mark.asyncio
    async def test_shadow_writes_nothing(self):
        summary, sb, svc = await _run(
            [_sheet_row(name="Grants & Contributions")],
            [_db_project(name="EU Grant")],
            {"p1": {"title": "EU Grant"}},
            write=False,
        )
        sb.rename_canonical_project.assert_not_called()
        sb.upsert_project_snapshot.assert_not_called()
        assert summary["renamed"] == 1        # reported, not performed


class TestMerge:
    def test_dry_run_reports_without_writing(self):
        from services.supabase_client import SupabaseClient

        client = MagicMock()
        rows = {"p_src": [{"id": "p_src", "name": "Pre-Seed Fundraising",
                           "aliases": ["Seed"]}],
                "p_tgt": [{"id": "p_tgt", "name": "Fundraising & Investors",
                           "aliases": []}]}

        def _select(*a, **k):
            chain = MagicMock()
            chain.eq.side_effect = lambda col, val: MagicMock(
                execute=MagicMock(return_value=MagicMock(
                    data=rows.get(val, [{"id": "x"}, {"id": "y"}]))))
            return chain

        client.table.return_value.select.side_effect = _select
        sb = SupabaseClient.__new__(SupabaseClient)
        with patch.object(SupabaseClient, "client", client):
            result = sb.merge_canonical_projects("p_src", "p_tgt", dry_run=True)

        assert result["source"] == "Pre-Seed Fundraising"
        assert result["target"] == "Fundraising & Investors"
        client.table.return_value.update.assert_not_called()

    def test_merging_a_project_into_itself_is_refused(self):
        from services.supabase_client import SupabaseClient

        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.execute.return_value = \
            MagicMock(data=[{"id": "p1", "name": "A", "aliases": []}])
        sb = SupabaseClient.__new__(SupabaseClient)
        with patch.object(SupabaseClient, "client", client):
            assert "error" in sb.merge_canonical_projects("p1", "p1", dry_run=True)

    def test_a_missing_project_is_refused(self):
        from services.supabase_client import SupabaseClient

        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.execute.return_value = \
            MagicMock(data=[])
        sb = SupabaseClient.__new__(SupabaseClient)
        with patch.object(SupabaseClient, "client", client):
            assert "error" in sb.merge_canonical_projects("p1", "p2", dry_run=True)


class TestTopicLinkProposals:
    """A recurring TOPIC must not become a project.

    propose_new_projects proposed a new canonical project for any label
    recurring in >=2 meetings. Correct when `label` was the only association we
    had; wrong once labels officially mean topics — 226 of them against 22
    curated projects. This is what kept PROJECT_LEARNING_ENABLED off.
    """

    def _run(self, unmatched, tasks, projects, pending=None):
        from processors import project_learning as pl

        sb = MagicMock()
        sb.get_unmatched_labels.return_value = unmatched
        sb.get_pending_approvals_by_status.return_value = pending or []
        sb.get_canonical_projects.return_value = projects
        sb.match_label_to_canonical.return_value = None
        sb.client.table.return_value.select.return_value.in_.return_value \
            .not_.is_.return_value.is_.return_value.limit.return_value \
            .execute.return_value = MagicMock(data=tasks)
        with patch.object(pl, "supabase_client", sb):
            return pl.propose_new_projects(), sb

    def test_a_topic_whose_work_has_a_home_becomes_a_LINK(self):
        result, sb = self._run(
            [{"label": "AWS Setup", "meeting_id": "m1"},
             {"label": "AWS Setup", "meeting_id": "m2"}],
            [{"label": "AWS Setup", "project_id": "p_cloud"} for _ in range(4)],
            [{"id": "p_cloud", "name": "Cloud Infrastructure"}],
        )
        assert result["links"] == ["AWS Setup -> Cloud Infrastructure"]
        kwargs = sb.create_pending_approval.call_args.kwargs
        assert kwargs["content_type"] == "topic_project_link"
        assert kwargs["content"]["project_id"] == "p_cloud"

    def test_a_genuinely_new_topic_still_proposes_a_project(self):
        """The original behaviour has to survive for labels with no home."""
        result, sb = self._run(
            [{"label": "Brand New Thing", "meeting_id": "m1"},
             {"label": "Brand New Thing", "meeting_id": "m2"}],
            [], [],
        )
        assert result["links"] == [] and result["proposed"] == 1
        assert sb.create_pending_approval.call_args.kwargs["content_type"] == "project_new"

    def test_a_cross_cutting_topic_is_not_forced_into_one_project(self):
        """Spread thinly across projects — Eyal should see the project
        proposal, not be nudged toward whichever won by a single task."""
        tasks = ([{"label": "Reporting", "project_id": "p1"}] * 2
                 + [{"label": "Reporting", "project_id": "p2"}] * 2)
        result, sb = self._run(
            [{"label": "Reporting", "meeting_id": "m1"},
             {"label": "Reporting", "meeting_id": "m2"}],
            tasks,
            [{"id": "p1", "name": "One"}, {"id": "p2", "name": "Two"}],
        )
        assert result["links"] == []
        assert sb.create_pending_approval.call_args.kwargs["content_type"] == "project_new"

    def test_a_label_seen_in_one_meeting_is_not_proposed_at_all(self):
        result, _ = self._run([{"label": "Once", "meeting_id": "m1"}], [], [])
        assert result["proposed"] == 0
