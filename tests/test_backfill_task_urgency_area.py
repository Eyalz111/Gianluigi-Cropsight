"""PR4 — one-time backfill of urgency + area on existing tasks.

Tests the pure derivation functions and the FIRM guardrail: the backfill update
payload NEVER contains deadline / deadline_confidence.
"""
import importlib.util
import os
from datetime import date, timedelta

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "backfill_task_urgency_area.py")
_spec = importlib.util.spec_from_file_location("backfill_ua", _PATH)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


def _d(days):
    return (date.today() + timedelta(days=days)).isoformat()


class TestDeriveUrgency:
    def test_explicit_near_is_high(self):
        assert backfill._derive_urgency({"deadline": _d(2), "deadline_confidence": "EXPLICIT"}) == "H"

    def test_explicit_past_is_high(self):
        assert backfill._derive_urgency({"deadline": _d(-5), "deadline_confidence": "EXPLICIT"}) == "H"

    def test_explicit_mid_is_medium(self):
        assert backfill._derive_urgency({"deadline": _d(10), "deadline_confidence": "EXPLICIT"}) == "M"

    def test_explicit_far_is_low(self):
        assert backfill._derive_urgency({"deadline": _d(60), "deadline_confidence": "EXPLICIT"}) == "L"

    def test_urgent_text_is_high(self):
        assert backfill._derive_urgency({"title": "ship the demo ASAP"}) == "H"

    def test_default_is_medium(self):
        assert backfill._derive_urgency({"title": "review the doc"}) == "M"

    def test_inferred_deadline_does_not_drive_high(self):
        # Only EXPLICIT deadlines drive proximity; an INFERRED date is ignored.
        assert backfill._derive_urgency(
            {"deadline": _d(1), "deadline_confidence": "INFERRED", "title": "x"}
        ) == "M"


class TestResolveArea:
    BN = {
        "bd & sales": {"id": "a-bd", "name": "BD & Sales"},
        "product & tech": {"id": "a-pt", "name": "Product & Tech"},
    }

    def test_label_match(self):
        assert backfill._resolve_area({"label": "BD & Sales"}, self.BN) == ("a-bd", "BD & Sales")

    def test_category_match(self):
        assert backfill._resolve_area({"category": "Product & Tech"}, self.BN) == ("a-pt", "Product & Tech")

    def test_title_phrase_match(self):
        assert backfill._resolve_area({"title": "fix the Product & Tech pipeline"}, self.BN) == ("a-pt", "Product & Tech")

    def test_unmatched_is_non_area(self):
        assert backfill._resolve_area({"title": "buy more coffee"}, self.BN) == (None, "non-area")


class TestGuardrail:
    def test_build_update_never_touches_deadline(self):
        upd = backfill._build_update(
            {"title": "x ASAP", "deadline": _d(1), "deadline_confidence": "EXPLICIT"}, {}
        )
        assert "deadline" not in upd
        assert "deadline_confidence" not in upd
        assert set(upd.keys()) == {"urgency", "area_id", "area_label"}
