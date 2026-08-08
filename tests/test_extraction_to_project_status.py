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


AREAS = [{"id": "a_fin", "name": "LEGAL, CORPORATE & FINANCE"},
         {"id": "a_prod", "name": "PRODUCT & TECHNOLOGY"}]
PROJECTS = [{"id": "p_fin", "name": "Finance", "area_id": "a_fin"},
            {"id": "p_cloud", "name": "Cloud Infrastructure", "area_id": "a_prod"},
            {"id": "p_other_fin", "name": "Others — Legal", "area_id": "a_fin"}]


def _batch2(tasks, links=None):
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
         patch.object(sb, "get_areas", return_value=AREAS, create=True), \
         patch.object(sb, "list_team_members", return_value=[], create=True), \
         patch.object(sb, "get_canonical_projects", return_value=PROJECTS, create=True), \
         patch.object(sb, "get_topic_project_links", return_value=links or {}, create=True), \
         patch.object(sb, "resolve_assignee", side_effect=lambda v, roster=None: v, create=True), \
         patch.object(sb, "resolve_category", side_effect=lambda v, areas=None: v, create=True), \
         patch.object(sb, "resolve_label", side_effect=lambda v, **k: v or "", create=True):
        sb.create_tasks_batch("m1", tasks)
    return captured.get("rows", [])


class TestFourTierResolution:
    """Extraction now answers the project question directly, with the closed
    list in front of it. "The system will learn" was too weak an answer: it
    needed a topic to recur across two meetings before it even proposed
    anything, so most tasks would have stayed invisible indefinitely."""

    def test_tier1_what_extraction_said_wins(self):
        rows = _batch2([{"title": "x", "label": "AWS Credit Card",
                         "project": "Finance", "category": "LEGAL, CORPORATE & FINANCE"}])
        assert rows[0]["project_id"] == "p_fin"

    def test_tier1_beats_an_unrelated_label_match(self):
        rows = _batch2([{"title": "x", "label": "Cloud Infrastructure",
                         "project": "Finance", "category": "LEGAL, CORPORATE & FINANCE"}])
        assert rows[0]["project_id"] == "p_fin"

    def test_an_invented_project_name_is_ignored(self):
        """Closed vocabulary — the model cannot mint a project."""
        rows = _batch2([{"title": "x", "label": "topic", "project": "Made Up Thing",
                         "category": "LEGAL, CORPORATE & FINANCE"}])
        assert rows[0]["project_id"] == "p_other_fin"      # falls to the net

    def test_tier2_an_approved_link_is_used_when_extraction_says_null(self):
        rows = _batch2([{"title": "x", "label": "AWS Setup", "project": None,
                         "category": "PRODUCT & TECHNOLOGY"}],
                       links={"aws setup": "p_cloud"})
        assert rows[0]["project_id"] == "p_cloud"

    def test_tier4_falls_to_the_areas_others_bucket(self):
        """The alternative is invisibility — no project means the task never
        appears on the Project Status sheet at all, with no error to notice."""
        rows = _batch2([{"title": "x", "label": "Brand New Topic", "project": None,
                         "category": "LEGAL, CORPORATE & FINANCE"}])
        assert rows[0]["project_id"] == "p_other_fin"

    def test_an_area_with_no_others_bucket_stays_null(self):
        rows = _batch2([{"title": "x", "label": "t", "project": None,
                         "category": "PRODUCT & TECHNOLOGY"}])
        assert "project_id" not in rows[0]

    def test_an_unknown_category_stays_null(self):
        rows = _batch2([{"title": "x", "label": "t", "project": None,
                         "category": "General"}])
        assert "project_id" not in rows[0]


class TestUnresolvableAreaFallsBackToTheMeeting:
    """Two of the four tasks from the 2026-08-08 weekly meeting resolved to
    category "General", which is not an area — so the Others-bucket fallback had
    nothing to catch them and they landed with project_id NULL, invisible on the
    sheet with no error to notice.

    Every task in a batch comes from ONE meeting, so its siblings are the best
    available evidence. Same trick project_status.build_status_pack already uses
    to place open questions, which carry no category at all.
    """

    def test_a_stray_task_is_filed_under_its_siblings_others_bucket(self):
        rows = _batch2([
            {"title": "real one", "label": "x", "project": "Finance",
             "category": "LEGAL, CORPORATE & FINANCE"},
            {"title": "stray", "label": "", "project": None, "category": "General"},
        ])
        stray = next(r for r in rows if r["title"] == "stray")
        assert stray["project_id"] == "p_other_fin"      # visible on the sheet
        # category is NOT guessed: it IS the Gantt area taxonomy, read by the
        # Gantt, the morning brief and the area rollups. Pushing an inference
        # there to fix a visibility problem on one sheet is too wide a blast
        # radius. "General" is the honest answer and stays. [2026-08-08]
        assert stray["category"] == "General"

    def test_the_majority_area_decides_the_bucket(self):
        rows = _batch2([
            {"title": "a", "label": "", "project": None, "category": "LEGAL, CORPORATE & FINANCE"},
            {"title": "b", "label": "", "project": None, "category": "LEGAL, CORPORATE & FINANCE"},
            {"title": "c", "label": "", "project": None, "category": "PRODUCT & TECHNOLOGY"},
            {"title": "stray", "label": "", "project": None, "category": "General"},
        ])
        stray = next(r for r in rows if r["title"] == "stray")
        assert stray["project_id"] == "p_other_fin"   # LEGAL wins the vote
        assert stray["category"] == "General"         # taxonomy left honest

    def test_a_task_that_already_resolved_is_untouched(self):
        rows = _batch2([
            {"title": "a", "label": "", "project": None, "category": "PRODUCT & TECHNOLOGY"},
            {"title": "b", "label": "", "project": "Finance",
             "category": "LEGAL, CORPORATE & FINANCE"},
        ])
        assert next(r for r in rows if r["title"] == "b")["category"] == \
            "LEGAL, CORPORATE & FINANCE"
        assert next(r for r in rows if r["title"] == "b")["project_id"] == "p_fin"

    def test_no_sibling_has_an_area_so_nothing_is_guessed(self):
        rows = _batch2([
            {"title": "a", "label": "", "project": None, "category": "General"},
            {"title": "b", "label": "", "project": None, "category": "General"},
        ])
        assert all(r["category"] == "General" for r in rows)
        assert all("project_id" not in r for r in rows)

    def test_an_explicit_project_survives_the_fallback(self):
        """The fallback fixes the AREA; it must not overwrite a real project."""
        rows = _batch2([
            {"title": "a", "label": "", "project": None, "category": "PRODUCT & TECHNOLOGY"},
            {"title": "stray", "label": "", "project": "Finance", "category": "General"},
        ])
        stray = next(r for r in rows if r["title"] == "stray")
        assert stray["project_id"] == "p_fin"
