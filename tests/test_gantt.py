"""
Tests for the v3 Gantt redesign (chunk 2): status rollup, tagging match,
tag-column safety, and the reconcile read-back detection. No live DB/Sheets.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


# ----------------------------------------------------------------------------
# gantt_status — pure logic
# ----------------------------------------------------------------------------
def test_status_map_and_topic_status():
    import processors.gantt_status as gs
    assert gs._STATUS_MAP["active"] == "active"
    assert gs._STATUS_MAP["blocked"] == "blocked"
    assert gs._STATUS_MAP["stale"] == "planned"
    assert gs._STATUS_MAP["closed"] == "completed"
    assert gs._topic_status({"brief_json": {"current_status": "active"}}) == "active"
    assert gs._topic_status({"state_json": {"current_status": "blocked"}}) == "blocked"
    assert gs._topic_status({"brief_json": {}}) is None


def test_sensitivity_gate():
    import processors.gantt_status as gs
    assert gs._is_ceo_only({"brief_json": {"sensitivity": "ceo"}}) is True
    assert gs._is_ceo_only({"sensitivity": "ceo"}) is True
    assert gs._is_ceo_only({"brief_json": {"current_status": "active", "sensitivity": "founders"}}) is False


# ----------------------------------------------------------------------------
# gantt_tagging — matching helpers
# ----------------------------------------------------------------------------
def test_tagging_helpers():
    import processors.gantt_tagging as gt
    assert gt._owner_prefix("[E/R] MVP work") == "[E/R]"
    assert gt._owner_prefix("  [R] thing") == "[R]"
    assert gt._owner_prefix("no prefix") is None
    assert gt._overlap("MVP Product", "MVP Product Delivery") >= 0.4
    assert gt._overlap("alpha beta", "zzz qqq") == 0.0


# ----------------------------------------------------------------------------
# gantt_rows — tag-column safety + resolve
# ----------------------------------------------------------------------------
def test_verify_tag_column_safe(monkeypatch):
    import services.gantt_rows as gr
    monkeypatch.setattr(gr, "_load_schema_metadata",
                        lambda: {"max_week": 20, "week_offset": 9, "first_week_col": "E"})
    monkeypatch.setattr(gr.settings, "GANTT_TAG_COLUMN", "DZ", raising=False)
    ok, msg = gr.verify_tag_column_safe()
    assert ok is True
    monkeypatch.setattr(gr.settings, "GANTT_TAG_COLUMN", "F", raising=False)  # idx 6, before last week col
    ok2, _ = gr.verify_tag_column_safe()
    assert ok2 is False


async def test_resolve_row_by_topic(monkeypatch):
    import services.gantt_rows as gr
    monkeypatch.setattr(gr, "read_row_tags", AsyncMock(return_value={5: "tA", 8: "tB"}))
    assert await gr.resolve_row_by_topic("2026-2027", "tB") == 8
    assert await gr.resolve_row_by_topic("2026-2027", "missing") is None


# ----------------------------------------------------------------------------
# reconcile_gantt — read-back detection (shadow: no writes)
# ----------------------------------------------------------------------------
def _grid(rows_filled: dict, max_cols: int = 12):
    """rows_filled: {row_number: [(col_index0, hexcolor), ...]} -> includeGridData resp."""
    max_row = max(rows_filled) if rows_filled else 0
    rowData = []
    for rn in range(1, max_row + 1):
        cells = []
        filled = dict(rows_filled.get(rn, []))
        for ci in range(max_cols):
            if ci in filled:
                cells.append({"formattedValue": "[R] work",
                              "effectiveFormat": {"backgroundColor": {"hex": filled[ci]}}})
            else:
                cells.append({})
        rowData.append({"values": cells})
    return {"sheets": [{"data": [{"rowData": rowData}]}]}


class TestReconcileGantt:
    async def test_contiguous_pulls_gapped_flags(self, monkeypatch):
        import processors.sheets_sync as ss
        import services.gantt_rows as gr
        import services.gantt_manager as gm
        import guardrails.gantt_guard as gg
        import services.google_sheets as gsvc

        monkeypatch.setattr(gm, "_get_color_map", lambda: {"active": "#b7d7b0"})
        monkeypatch.setattr(gm, "_sheets_color_to_hex", lambda bg: (bg or {}).get("hex", ""))
        monkeypatch.setattr(gg, "_load_schema", lambda: [{"sheet_name": "2026-2027"}])
        monkeypatch.setattr(gg, "_load_schema_metadata",
                            lambda: {"week_offset": 9, "first_week_col": "E", "max_week": 20})
        # row 10 -> topic-A: cols 0,1,2 filled (weeks 9,10,11) contiguous
        # row 11 -> topic-B: cols 0,1 + col 4 filled (weeks 9,10,13) — gap at 11,12
        monkeypatch.setattr(gr, "read_row_tags", AsyncMock(return_value={10: "topic-A", 11: "topic-B"}))
        grid = _grid({10: [(0, "#b7d7b0"), (1, "#b7d7b0"), (2, "#b7d7b0")],
                      11: [(0, "#b7d7b0"), (1, "#b7d7b0"), (4, "#b7d7b0")]})
        fake_svc = MagicMock()
        fake_svc.service.spreadsheets.return_value.get.return_value.execute.return_value = grid
        monkeypatch.setattr(gsvc, "sheets_service", fake_svc)

        sc = ss.supabase_client
        monkeypatch.setattr(sc, "get_gantt_rows", lambda *a, **k: [
            {"sheet_name": "2026-2027", "topic_id": "topic-A", "id": "gA"},
            {"sheet_name": "2026-2027", "topic_id": "topic-B", "id": "gB"},
        ])
        monkeypatch.setattr(sc, "get_gantt_snapshots", lambda *a, **k: {})
        marks = []
        monkeypatch.setattr(sc, "mark_gantt_field_manual", lambda *a, **k: marks.append(a) or True)
        monkeypatch.setattr(sc, "upsert_gantt_snapshot", lambda *a, **k: True)
        monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)

        res = await ss.reconcile_gantt(shadow=True)
        assert res["pulled"] == 1            # topic-A contiguous span 9-11
        assert res["flagged_multigap"] == 1  # topic-B gapped
        assert marks == []                   # shadow writes nothing
