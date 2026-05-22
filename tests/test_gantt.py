"""
Tests for the v3 Gantt redesign (REVISED): per-lane linkage, read-back, and nudges.
Core safety invariant under test: read-back + nudges NEVER write the board, and
linkage uses knowledge_links 'gantt_covers' (DB-only). No live DB/Sheets.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


# ----------------------------------------------------------------------------
# gantt_tagging — matching helpers (kept; module still used by legacy tooling)
# ----------------------------------------------------------------------------
def test_tagging_helpers():
    import processors.gantt_tagging as gt
    assert gt._owner_prefix("[E/R] MVP work") == "[E/R]"
    assert gt._owner_prefix("  [R] thing") == "[R]"
    assert gt._owner_prefix("no prefix") is None
    assert gt._overlap("MVP Product", "MVP Product Delivery") >= 0.4
    assert gt._overlap("alpha beta", "zzz qqq") == 0.0


# ----------------------------------------------------------------------------
# gantt_nudge — status/sensitivity logic + divergence rubric, cap, dedupe
# ----------------------------------------------------------------------------
def test_nudge_status_and_sensitivity():
    import processors.gantt_nudge as gn
    assert gn._topic_status({"brief_json": {"current_status": "blocked"}}) == "blocked"
    assert gn._topic_status({"brief_json": {}}) == "unknown"
    assert gn._is_ceo_only({"brief_json": {"sensitivity": "ceo"}}) is True
    assert gn._is_ceo_only({"brief_json": {"sensitivity": "founders"}}) is False


def test_nudges_divergence_cap_and_ceo_skip(monkeypatch):
    import processors.gantt_nudge as gn
    sc = gn.supabase_client
    # 8 lanes, each linked to a topic that has a blocker but board active -> severity-3 nudge
    lanes = [{"id": f"g{i}", "lane_type": "execution", "lane_index": i, "status": "active", "area_id": "a"}
             for i in range(8)]
    monkeypatch.setattr(sc, "get_gantt_rows", lambda *a, **k: lanes)
    monkeypatch.setattr(sc, "get_knowledge_links",
                        lambda **k: [{"to_id": "t-" + k["from_id"]}])
    monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [])

    def fake_topic(tid):
        ceo = tid.endswith("g0")  # one CEO-tier topic -> must be skipped
        return {"topic_name": tid, "brief_json": {
            "current_status": "active", "sensitivity": "ceo" if ceo else "founders",
            "open_items": [{"kind": "blocker", "description": "stuck"}]}}
    monkeypatch.setattr(sc, "get_topic_thread", fake_topic)

    res = gn.compute_gantt_nudges(sheet="X", shadow=True)
    assert res["nudges"] == gn._CAP          # capped at 5 globally
    assert all("g0" not in n["gantt_row_id"] for n in res["items"])  # CEO lane skipped


def test_nudges_shadow_writes_nothing(monkeypatch):
    import processors.gantt_nudge as gn
    sc = gn.supabase_client
    monkeypatch.setattr(sc, "get_gantt_rows", lambda *a, **k: [
        {"id": "g1", "lane_type": "execution", "lane_index": 1, "status": "active", "area_id": "a"}])
    monkeypatch.setattr(sc, "get_knowledge_links", lambda **k: [{"to_id": "t1"}])
    monkeypatch.setattr(sc, "get_pending_approvals_by_status", lambda *a, **k: [])
    monkeypatch.setattr(sc, "get_topic_thread", lambda tid: {
        "topic_name": "T", "brief_json": {"current_status": "blocked", "open_items": [{"kind": "blocker", "description": "x"}]}})
    upserts = []
    monkeypatch.setattr(sc, "upsert_pending_approval", lambda **k: upserts.append(k))
    res = gn.compute_gantt_nudges(sheet="X", shadow=True)
    assert res["nudges"] >= 1
    assert upserts == []  # shadow persists nothing


# ----------------------------------------------------------------------------
# gantt_readback — DB-only, shadow writes nothing, NEVER calls board batchUpdate
# ----------------------------------------------------------------------------
async def test_readback_shadow_no_board_write(monkeypatch):
    import processors.gantt_readback as rb
    import services.google_sheets as gsvc

    # patch the names AS BOUND in gantt_readback (imported at module load)
    monkeypatch.setattr(rb, "_get_color_map", lambda: {"active": "#b7d7b0"})
    monkeypatch.setattr(rb, "_sheets_color_to_hex", lambda bg: (bg or {}).get("hex", ""))
    monkeypatch.setattr(rb, "_load_schema_metadata",
                        lambda: {"week_offset": 9, "first_week_col": "E", "max_week": 20})
    # lane at row 10, cols 0,1,2 filled active (contiguous)
    grid = {"sheets": [{"data": [{"rowData": [
        {"values": [{"formattedValue": "[R] w", "effectiveFormat": {"backgroundColor": {"hex": "#b7d7b0"}}}
                    if ci in (0, 1, 2) else {} for ci in range(12)]}
        if rn == 10 else {"values": []} for rn in range(1, 11)]}]}]}
    fake_svc = MagicMock()
    fake_svc.service.spreadsheets.return_value.get.return_value.execute.return_value = grid
    monkeypatch.setattr(gsvc, "sheets_service", fake_svc)

    sc = rb.supabase_client
    monkeypatch.setattr(sc, "get_gantt_rows", lambda *a, **k: [
        {"id": "g1", "lane_type": "execution", "lane_index": 1, "display_order": 10}])
    monkeypatch.setattr(sc, "get_gantt_row_snapshots", lambda *a, **k: {})
    monkeypatch.setattr(sc, "log_action", lambda *a, **k: None)

    res = await rb.reconcile_gantt_lanes(sheet="X", shadow=True)
    assert res["pulled"] == 1                       # detected Eyal's span
    fake_svc.service.spreadsheets.return_value.batchUpdate.assert_not_called()  # NEVER writes the board


# ----------------------------------------------------------------------------
# gantt_linkage — apply uses knowledge_links 'gantt_covers', no board write
# ----------------------------------------------------------------------------
def test_linkage_apply_uses_gantt_covers(monkeypatch):
    import processors.gantt_linkage as gl
    calls = []
    monkeypatch.setattr(gl.supabase_client, "create_knowledge_link",
                        lambda **k: calls.append(k) or {})
    res = gl.apply_lane_links([
        {"gantt_row_id": "g1", "candidates": [{"topic_id": "t1"}, {"topic_id": "t2"}]}])
    assert res["links_created"] == 2
    assert all(c["link_type"] == "gantt_covers" and c["from_type"] == "gantt_row" for c in calls)
