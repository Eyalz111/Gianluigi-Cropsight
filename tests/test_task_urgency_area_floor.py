"""PR1 — operational-task floor: urgency + area fields (additive, no behavior change).

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
        assert t.area_id is None
        assert t.area_label == "non-area"

    def test_task_accepts_urgency_area(self):
        from models.schemas import Task, TaskUrgency
        from uuid import uuid4
        aid = uuid4()
        t = Task(title="t", assignee="a", urgency=TaskUrgency.HIGH,
                 area_id=aid, area_label="BD & Sales")
        assert t.urgency == TaskUrgency.HIGH
        assert t.area_id == aid
        assert t.area_label == "BD & Sales"


# ------------------------------------------------------- supabase plumbing ----
def _mock_sc():
    from services.supabase_client import SupabaseClient
    with patch.object(SupabaseClient, "__init__", return_value=None):
        sc = SupabaseClient()
    sc.log_action = MagicMock()
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
    def test_create_task_passes_urgency_area(self):
        sc, captured = _mock_sc()
        sc.create_task("title", "Eyal", urgency="H", area_id="a-1", area_label="BD & Sales")
        p = captured["payload"]
        assert p["urgency"] == "H"
        assert p["area_id"] == "a-1"
        assert p["area_label"] == "BD & Sales"

    def test_create_task_defaults(self):
        sc, captured = _mock_sc()
        sc.create_task("title", "Eyal")
        p = captured["payload"]
        assert p["urgency"] == "M"
        assert p["area_id"] is None
        assert p["area_label"] == "non-area"

    def test_create_tasks_batch_passes_urgency_area(self):
        sc, captured = _mock_sc()
        sc.create_tasks_batch("m-1", [
            {"title": "t1", "urgency": "H", "area_id": "a-1", "area_label": "Product & Tech"},
            {"title": "t2"},  # defaults
        ])
        rows = captured["payload"]
        assert rows[0]["urgency"] == "H"
        assert rows[0]["area_id"] == "a-1"
        assert rows[0]["area_label"] == "Product & Tech"
        # defaults for the second row
        assert rows[1]["urgency"] == "M"
        assert rows[1]["area_id"] is None
        assert rows[1]["area_label"] == "non-area"
