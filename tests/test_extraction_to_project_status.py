"""The link from a meeting transcript to the Project Status sheet.

Eyal's model: a summary approved on Telegram feeds the Tasks tracker AND the
Project Status sheet in parallel, both off the same database. That is the
design — but until 2026-08-07 the second half could not happen.

The Project Status sheet is PROJECT-centric and its auto-injection requires
project_id. create_tasks_batch — the path every transcript goes through — set
`label` and nothing else, so every extracted task landed with project_id NULL.
Auto-injection would have run forever and injected nothing.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.supabase_client import SupabaseClient


def _batch(tasks, links=None, projects=None):
    """Run create_tasks_batch against a mocked DB, return the inserted rows."""
    sb = SupabaseClient.__new__(SupabaseClient)
    captured = {}

    def _insert(payload):
        captured["rows"] = payload
        chain = MagicMock()
        chain.execute.return_value = MagicMock(
            data=[{**r, "id": f"id{i}"} for i, r in enumerate(payload)])
        return chain

    client = MagicMock()
    client.table.return_value.insert.side_effect = _insert
    with patch.object(SupabaseClient, "client", client), \
         patch.object(sb, "get_areas", return_value=[], create=True), \
         patch.object(sb, "list_team_members", return_value=[], create=True), \
         patch.object(sb, "get_canonical_projects",
                      return_value=projects or [], create=True), \
         patch.object(sb, "get_topic_project_links",
                      return_value=links or {}, create=True), \
         patch.object(sb, "resolve_assignee", side_effect=lambda v, roster=None: v,
                      create=True), \
         patch.object(sb, "resolve_category", side_effect=lambda v, areas=None: v,
                      create=True), \
         patch.object(sb, "resolve_label",
                      side_effect=lambda v, **k: v or "", create=True):
        sb.create_tasks_batch("m1", tasks)
    return captured.get("rows", [])


class TestExtractedTasksReachTheSheet:
    def test_a_label_matching_a_project_name_attaches(self):
        rows = _batch([{"title": "Ship it", "label": "Cloud Infrastructure"}],
                      projects=[{"id": "p_cloud", "name": "Cloud Infrastructure"}])
        assert rows[0]["project_id"] == "p_cloud"

    def test_an_approved_topic_link_attaches(self):
        """The whole point of topic_project_links: approve "AWS Setup ->
        Cloud Infrastructure" once, and every future task carrying that topic
        attaches by itself."""
        rows = _batch([{"title": "Ship it", "label": "AWS Setup"}],
                      links={"aws setup": "p_cloud"},
                      projects=[{"id": "p_cloud", "name": "Cloud Infrastructure"}])
        assert rows[0]["project_id"] == "p_cloud"

    def test_a_link_outranks_a_name_match(self):
        rows = _batch([{"title": "Ship it", "label": "Cloud Infrastructure"}],
                      links={"cloud infrastructure": "p_override"},
                      projects=[{"id": "p_cloud", "name": "Cloud Infrastructure"}])
        assert rows[0]["project_id"] == "p_override"

    def test_an_unmatched_topic_stays_null_rather_than_guessing(self):
        """A wrong project is worse than none — it surfaces as a link proposal."""
        rows = _batch([{"title": "Ship it", "label": "Some New Topic"}],
                      projects=[{"id": "p_cloud", "name": "Cloud Infrastructure"}])
        assert "project_id" not in rows[0]

    def test_a_task_with_no_label_stays_null(self):
        rows = _batch([{"title": "Ship it", "label": ""}])
        assert "project_id" not in rows[0]

    def test_the_rest_of_the_row_is_unchanged(self):
        rows = _batch([{"title": "Ship it", "label": "X", "priority": "H",
                        "urgency": "H"}])
        assert rows[0]["title"] == "Ship it"
        assert rows[0]["status"] == "pending"
        assert rows[0]["meeting_id"] == "m1"
