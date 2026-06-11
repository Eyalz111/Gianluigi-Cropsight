"""PR1 — operational-task floor: urgency field plumbing.

2026-06 category realignment: the per-task area surface is gone — create_task/
create_tasks_batch no longer take or write area_id/area_label (the retained DB
columns get their defaults), and category is canonicalized at this choke point.

Mirrors tests/test_deadline_confidence.py's create_task payload-capture pattern.
"""
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------- model -------
class TestTaskUrgencyAreaSchema:
    def test_urgency_enum_values(self):
        from models.schemas import TaskUrgency
        assert TaskUrgency.HIGH.value == "H"
        assert TaskUrgency.MEDIUM.value == "M"
        assert TaskUrgency.LOW.value == "L"

    def test_task_defaults(self):
        from models.schemas import Task, TaskUrgency
        t = Task(title="t", assignee="a")
        assert t.urgency == TaskUrgency.MEDIUM
        # deprecated columns retain their model defaults
        assert t.area_id is None
        assert t.area_label == "non-area"

    def test_task_accepts_urgency(self):
        from models.schemas import Task, TaskUrgency
        t = Task(title="t", assignee="a", urgency=TaskUrgency.HIGH)
        assert t.urgency == TaskUrgency.HIGH


# ------------------------------------------------------- supabase plumbing ----
def _mock_sc():
    from services.supabase_client import SupabaseClient
    with patch.object(SupabaseClient, "__init__", return_value=None):
        sc = SupabaseClient()
    sc.log_action = MagicMock()
    sc.get_areas = MagicMock(return_value=[
        {"id": "a-1", "name": "SALES & BUSINESS DEVELOPMENT"},
        {"id": "a-2", "name": "PRODUCT & TECHNOLOGY"},
    ])
    captured = {}
    fake_query = MagicMock()
    fake_query.insert.return_value = fake_query

    def _exec():
        captured["payload"] = fake_query.insert.call_args.args[0]
        return MagicMock(data=[{"id": "t1"}])

    fake_query.execute.side_effect = _exec
    mock_client = MagicMock()
    mock_client.table.return_value = fake_query
    sc._client = mock_client
    return sc, captured


class TestCreateTaskPlumbing:
    def test_create_task_passes_urgency_and_canonical_category(self):
        sc, captured = _mock_sc()
        sc.create_task("title", "Eyal", urgency="H", category="BD & Sales")
        p = captured["payload"]
        assert p["urgency"] == "H"
        # legacy taxonomy canonicalized at the choke point
        assert p["category"] == "SALES & BUSINESS DEVELOPMENT"
        # area fields are no longer written (DB column defaults apply)
        assert "area_id" not in p
        assert "area_label" not in p

    def test_create_task_defaults(self):
        sc, captured = _mock_sc()
        sc.create_task("title", "Eyal")
        p = captured["payload"]
        assert p["urgency"] == "M"
        assert p["category"] == "General"
        assert "area_id" not in p
        assert "area_label" not in p

    def test_create_tasks_batch_passes_urgency_and_canonical_category(self):
        sc, captured = _mock_sc()
        sc.create_tasks_batch("m-1", [
            {"title": "t1", "urgency": "H", "category": "Product & Tech"},
            {"title": "t2"},  # defaults
        ])
        rows = captured["payload"]
        assert rows[0]["urgency"] == "H"
        assert rows[0]["category"] == "PRODUCT & TECHNOLOGY"
        assert "area_id" not in rows[0]
        assert "area_label" not in rows[0]
        # defaults for the second row
        assert rows[1]["urgency"] == "M"
        assert rows[1]["category"] == "General"
