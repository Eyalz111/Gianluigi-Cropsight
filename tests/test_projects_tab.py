"""
Projects tab — the editable face of canonical_projects. [2026-07-23]

The vocabulary resolve_label enforces, made visible and editable, so a rename is
a one-cell edit that backfills every reference — the lesson from having to run a
script for SatYield→CropSight. Editing the Name RENAMES; a blank-id row is a new
project.

The load-bearing, consequential path is the rename: it mutates `label` across
tasks/decisions/open_questions/follow_up_meetings + renames the topic thread and
keeps the old name as an alias. These tests pin that it sweeps ALL tables and
never fractures the vocabulary.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def sc():
    try:
        from services.supabase_client import supabase_client
    except Exception as e:  # pragma: no cover
        pytest.skip(f"cannot import supabase_client ({e})")
    return supabase_client


class _Tbl:
    """A per-table fake that records updates and answers selects."""

    def __init__(self, name, store):
        self.name = name
        self.store = store          # {table: [rows]}
        self._filters = {}
        self._neq = {}
        self._pending = None

    def select(self, *a, **k): return self
    def eq(self, col, val): self._filters[col] = val; return self
    def neq(self, col, val): self._neq[col] = val; return self
    def limit(self, *a, **k): return self

    def update(self, payload): self._pending = ("update", payload); return self

    def execute(self):
        rows = self.store.get(self.name, [])
        if self._pending and self._pending[0] == "update":
            payload = self._pending[1]
            for r in rows:
                if all(r.get(c) == v for c, v in self._filters.items()):
                    r.update(payload)
            self._pending = None
            return MagicMock(data=[])
        # select
        out = [r for r in rows
               if all(r.get(c) == v for c, v in self._filters.items())
               and all(r.get(c) != v for c, v in self._neq.items())]
        self._filters, self._neq = {}, {}
        return MagicMock(data=out)


def _client(store):
    return MagicMock(table=lambda name: _Tbl(name, store))


class TestRename:
    def test_rename_backfills_every_label_table_and_topic(self, sc, monkeypatch):
        store = {
            "canonical_projects": [{"id": "p1", "name": "SatYield Accuracy Model",
                                    "aliases": ["the model"]}],
            "tasks": [{"id": "t1", "label": "SatYield Accuracy Model"},
                      {"id": "t2", "label": "Other"}],
            "decisions": [{"id": "d1", "label": "SatYield Accuracy Model"}],
            "open_questions": [{"id": "q1", "label": "SatYield Accuracy Model"},
                               {"id": "q2", "label": "SatYield Accuracy Model"}],
            "follow_up_meetings": [{"id": "m1", "label": "SatYield Accuracy Model"}],
            "topic_threads": [{"id": "tt1", "topic_name": "SatYield Accuracy Model",
                               "topic_name_lower": "satyield accuracy model"}],
        }
        monkeypatch.setattr(sc, "_client", _client(store))
        monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)

        res = sc.rename_canonical_project("p1", "CropSight Accuracy Model")

        assert res["renamed"] is True
        assert res["relabelled"] == {"tasks": 1, "decisions": 1,
                                     "open_questions": 2, "follow_up_meetings": 1}
        assert res["topic_threads"] == 1
        # canonical renamed, OLD name folded into aliases
        cp = store["canonical_projects"][0]
        assert cp["name"] == "CropSight Accuracy Model"
        assert "SatYield Accuracy Model" in cp["aliases"]
        assert "the model" in cp["aliases"], "existing aliases preserved"
        # every reference swept; the unrelated task untouched
        assert store["tasks"][0]["label"] == "CropSight Accuracy Model"
        assert store["tasks"][1]["label"] == "Other"
        assert store["open_questions"][0]["label"] == "CropSight Accuracy Model"
        assert store["topic_threads"][0]["topic_name"] == "CropSight Accuracy Model"

    def test_rename_to_same_name_is_a_noop(self, sc, monkeypatch):
        store = {"canonical_projects": [{"id": "p1", "name": "X", "aliases": []}]}
        monkeypatch.setattr(sc, "_client", _client(store))
        assert sc.rename_canonical_project("p1", "X")["renamed"] is False

    def test_rename_collision_is_refused(self, sc, monkeypatch):
        store = {"canonical_projects": [
            {"id": "p1", "name": "A", "aliases": []},
            {"id": "p2", "name": "B", "aliases": []},
        ]}
        monkeypatch.setattr(sc, "_client", _client(store))
        with pytest.raises(ValueError, match="already named"):
            sc.rename_canonical_project("p1", "B")

    def test_blank_new_name_refused(self, sc, monkeypatch):
        store = {"canonical_projects": [{"id": "p1", "name": "A", "aliases": []}]}
        monkeypatch.setattr(sc, "_client", _client(store))
        with pytest.raises(ValueError):
            sc.rename_canonical_project("p1", "   ")


class TestReconcile:
    def _setup(self, monkeypatch, sheet_rows, db_projects, areas=None):
        import processors.sheets_sync as ss
        import services.google_sheets as gs
        from config.settings import settings

        monkeypatch.setattr(settings, "PROJECTS_RECONCILE_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "PROJECTS_RECONCILE_SHADOW_MODE", False, raising=False)
        monkeypatch.setattr(settings, "TASK_TRACKER_SHEET_ID", "sheet-x", raising=False)

        fake = MagicMock()
        fake.get_all_projects = AsyncMock(return_value=sheet_rows)
        fake.add_projects_batch = AsyncMock(return_value=True)
        fake._update_cell = AsyncMock(return_value=None)
        monkeypatch.setattr(gs, "sheets_service", fake)

        sc = ss.supabase_client
        calls = {"rename": [], "create": [], "update": []}
        monkeypatch.setattr(sc, "get_canonical_projects", lambda *a, **k: db_projects)
        monkeypatch.setattr(sc, "get_areas", lambda *a, **k: areas or [])
        monkeypatch.setattr(sc, "rename_canonical_project",
                            lambda pid, name: calls["rename"].append((pid, name)) or {"renamed": True})
        monkeypatch.setattr(sc, "add_canonical_project",
                            lambda **k: calls["create"].append(k) or {"id": "new-p", **k})
        monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)
        # client.table(...).update(...).eq(...).execute() for plain updates
        monkeypatch.setattr(sc, "_client", MagicMock())
        return ss, calls, fake

    async def test_name_change_triggers_rename(self, monkeypatch):
        sheet = [{"row_number": 2, "id": "p1", "name": "CropSight Accuracy Model",
                  "aliases": "", "area": "", "description": ""}]
        db = [{"id": "p1", "name": "SatYield Accuracy Model", "aliases": [], "area_id": None}]
        ss, calls, _ = self._setup(monkeypatch, sheet, db)

        res = await ss.reconcile_projects()

        assert res["renamed"] == 1
        assert calls["rename"] == [("p1", "CropSight Accuracy Model")]
        assert res["renames"][0] == {"from": "SatYield Accuracy Model",
                                     "to": "CropSight Accuracy Model"}

    async def test_blank_id_row_creates_and_writes_id_back(self, monkeypatch):
        sheet = [{"row_number": 5, "id": "", "name": "New Grant Program",
                  "aliases": "grants, funding", "area": "", "description": "EU money"}]
        ss, calls, fake = self._setup(monkeypatch, sheet, [])

        res = await ss.reconcile_projects()

        assert res["created"] == 1
        assert calls["create"][0]["name"] == "New Grant Program"
        assert calls["create"][0]["aliases"] == ["grants", "funding"]
        fake._update_cell.assert_awaited_once()

    async def test_unchanged_project_is_a_noop(self, monkeypatch):
        sheet = [{"row_number": 2, "id": "p1", "name": "Moldova Pilot",
                  "aliases": "", "area": "", "description": ""}]
        db = [{"id": "p1", "name": "Moldova Pilot", "aliases": [], "area_id": None}]
        ss, calls, _ = self._setup(monkeypatch, sheet, db)

        res = await ss.reconcile_projects()

        assert res["renamed"] == 0 and res["created"] == 0
        assert calls["rename"] == []

    async def test_empty_sheet_with_db_projects_aborts(self, monkeypatch):
        ss, calls, fake = self._setup(monkeypatch, [], [{"id": "p1", "name": "X"}])
        res = await ss.reconcile_projects()
        assert res.get("error") == "sheet_read_empty"
        assert calls["rename"] == [] and calls["create"] == []

    async def test_disabled_is_a_noop(self, monkeypatch):
        import processors.sheets_sync as ss
        from config.settings import settings
        monkeypatch.setattr(settings, "PROJECTS_RECONCILE_ENABLED", False, raising=False)
        assert "skipped" in await ss.reconcile_projects()
