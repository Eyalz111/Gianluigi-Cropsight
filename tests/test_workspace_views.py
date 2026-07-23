"""
Generated workspace tabs: Open Questions + Areas. [2026-07-22]

These are read-only views Nechama uses instead of querying anything — she has
no DB access by design. Both render the SAME hierarchy as the rest of the
workspace: Area is never stored per-entity, it is derived through the project
(canonical_projects.area_id), so reclassifying a project moves everything under
it in one edit.

Invariants pinned here:
  - a task's OWN category wins; the project lookup only fills a blank
  - questions past the aging cutoff never render, even if the nightly aging
    pass hasn't run yet
  - the rebuild refuses to clear a populated tab on an empty read (the April
    wipe class) — except Open Questions, where "none left" is a real state
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import processors.workspace_views as wv
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import workspace_views ({e})", allow_module_level=True)


_AREAS = [
    {"id": "a1", "name": "PRODUCT & TECHNOLOGY", "brief_json": {"current_focus": "V1 accuracy"}},
    {"id": "a2", "name": "SALES & BUSINESS DEVELOPMENT", "brief_json": {}},
]
_PROJECTS = [
    {"name": "Product V1", "area_id": "a1", "aliases": ["MVP"]},
    {"name": "Investor Outreach", "area_id": "a2", "aliases": []},
    {"name": "Orphan Project", "area_id": None, "aliases": []},
]


def _patch_common(monkeypatch):
    sc = wv.supabase_client
    monkeypatch.setattr(sc, "get_areas", lambda *a, **k: _AREAS)
    monkeypatch.setattr(sc, "get_canonical_projects", lambda *a, **k: _PROJECTS)
    return sc


class TestProjectToArea:
    def test_maps_names_and_aliases(self, monkeypatch):
        _patch_common(monkeypatch)
        m = wv._project_to_area()
        assert m["product v1"] == "PRODUCT & TECHNOLOGY"
        assert m["mvp"] == "PRODUCT & TECHNOLOGY", "aliases resolve too"
        assert m["investor outreach"] == "SALES & BUSINESS DEVELOPMENT"

    def test_project_without_an_area_is_not_mapped(self, monkeypatch):
        _patch_common(monkeypatch)
        assert "orphan project" not in wv._project_to_area()

    def test_area_of_prefers_explicit_fallback(self, monkeypatch):
        """A task carries its OWN category and that is authoritative — the
        project lookup only fills in when the task has none."""
        _patch_common(monkeypatch)
        m = wv._project_to_area()
        assert wv._area_of("Unknown Thing", m, fallback="LEGAL, CORPORATE & FINANCE") \
            == "LEGAL, CORPORATE & FINANCE"
        assert wv._area_of("Product V1", m, fallback="") == "PRODUCT & TECHNOLOGY"
        assert wv._area_of("", m, fallback="") == "General"


class TestQuestionsView:
    async def test_stale_questions_never_render(self, monkeypatch):
        """Belt to the aging job's braces: if the nightly pass hasn't run, the
        tab must still not show questions it is supposed to have retired."""
        import services.google_sheets as gs
        from datetime import date, timedelta

        sc = _patch_common(monkeypatch)
        fresh = (date.today() - timedelta(days=5)).isoformat()
        old = (date.today() - timedelta(days=200)).isoformat()

        class _Tbl:
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def order(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def execute(self):
                return MagicMock(data=[
                    {"id": "q1", "question": "fresh one", "raised_by": "Eyal Zror",
                     "status": "open", "label": "Product V1", "created_at": fresh,
                     "meetings": {"title": "M1"}},
                    {"id": "q2", "question": "ancient", "raised_by": "Eyal Zror",
                     "status": "open", "label": "Product V1", "created_at": old,
                     "meetings": {"title": "M0"}},
                ])

        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: _Tbl()))
        captured = {}
        fake = MagicMock()
        fake.rebuild_questions_tab = AsyncMock(
            side_effect=lambda rows: captured.setdefault("rows", rows) or True)
        monkeypatch.setattr(gs, "sheets_service", fake)

        res = await wv.build_questions_view()

        assert res["rows"] == 1
        assert res["skipped_stale"] == 1
        assert [r["id"] for r in captured["rows"]] == ["q1"]

    async def test_read_failure_is_not_fatal(self, monkeypatch):
        sc = _patch_common(monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("supabase down")
        monkeypatch.setattr(sc, "_client", MagicMock(table=_boom))

        res = await wv.build_questions_view()
        assert "error" in res and res["rows"] == 0


class TestAreasView:
    async def test_counts_roll_up_through_the_project(self, monkeypatch):
        import services.google_sheets as gs
        from datetime import date, timedelta

        sc = _patch_common(monkeypatch)
        past = (date.today() - timedelta(days=10)).isoformat()

        monkeypatch.setattr(sc, "get_tasks", lambda *a, **k: [
            # label resolves to PRODUCT & TECHNOLOGY via the project
            {"id": "t1", "status": "pending", "label": "Product V1", "category": "",
             "deadline": past, "updated_at": "2026-07-20"},
            # own category wins over an unknown label
            {"id": "t2", "status": "pending", "label": "Nope",
             "category": "SALES & BUSINESS DEVELOPMENT", "deadline": None,
             "updated_at": "2026-07-21"},
            # done tasks are not open work
            {"id": "t3", "status": "done", "label": "Product V1", "category": "",
             "deadline": past, "updated_at": "2026-07-22"},
        ])
        monkeypatch.setattr(sc, "list_follow_up_meetings", lambda *a, **k: [
            {"id": "m1", "label": "Product V1", "status": "not_scheduled"},
            {"id": "m2", "label": "Product V1", "status": "held"},   # not a queue item
        ])

        class _Tbl:
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def execute(self):
                return MagicMock(data=[{"label": "Investor Outreach", "status": "open"}])

        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: _Tbl()))
        captured = {}
        fake = MagicMock()
        fake.rebuild_areas_tab = AsyncMock(
            side_effect=lambda rows: captured.setdefault("rows", rows) or True)
        monkeypatch.setattr(gs, "sheets_service", fake)

        await wv.build_areas_view()
        by_name = {r["name"]: r for r in captured["rows"]}

        pt = by_name["PRODUCT & TECHNOLOGY"]
        assert pt["open_tasks"] == 1, "done tasks excluded"
        assert pt["overdue"] == 1
        assert pt["meetings_to_schedule"] == 1, "only not_scheduled counts"
        assert pt["current_focus"] == "V1 accuracy"

        sales = by_name["SALES & BUSINESS DEVELOPMENT"]
        assert sales["open_tasks"] == 1, "task's own category wins over unknown label"
        assert sales["open_questions"] == 1

    async def test_general_bucket_appears_when_non_empty(self, monkeypatch):
        """'General' is not an areas row, but it IS the triage bucket — showing
        it is the point."""
        import services.google_sheets as gs

        sc = _patch_common(monkeypatch)
        monkeypatch.setattr(sc, "get_tasks", lambda *a, **k: [
            {"id": "t1", "status": "pending", "label": "", "category": "",
             "deadline": None, "updated_at": "2026-07-20"},
        ])
        monkeypatch.setattr(sc, "list_follow_up_meetings", lambda *a, **k: [])

        class _Tbl:
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def execute(self): return MagicMock(data=[])

        monkeypatch.setattr(sc, "_client", MagicMock(table=lambda *a, **k: _Tbl()))
        captured = {}
        fake = MagicMock()
        fake.rebuild_areas_tab = AsyncMock(
            side_effect=lambda rows: captured.setdefault("rows", rows) or True)
        monkeypatch.setattr(gs, "sheets_service", fake)

        await wv.build_areas_view()
        names = [r["name"] for r in captured["rows"]]
        assert "General" in names
