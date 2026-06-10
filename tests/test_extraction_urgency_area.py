"""PR3 — shadow: extraction + manual injection populate urgency/area.

Tests the two pure seams: the post-extraction normalizer
(transcript_processor._normalize_task_urgency_area) and the manual-injection
unifier (debrief._unify_manual_task), incl. the no-invented-dates guardrail.
"""
from unittest.mock import patch

import processors.debrief as debrief
from processors.transcript_processor import _normalize_task_urgency_area

AREAS = [{"id": "a-bd", "name": "BD & Sales"}, {"id": "a-pt", "name": "Product & Tech"}]


# -------------------------------------------------- extraction normalizer -----
class TestNormalizer:
    def test_urgency_coerced(self):
        tasks = [{"urgency": "h"}, {"urgency": "x"}, {"urgency": None}, {"urgency": "L"}]
        _normalize_task_urgency_area(tasks, AREAS)
        assert [t["urgency"] for t in tasks] == ["H", "M", "M", "L"]

    def test_area_resolves_or_non_area(self):
        tasks = [{"area": "bd & sales"}, {"area": "nonsense"}, {"area": None}]
        _normalize_task_urgency_area(tasks, AREAS)
        assert tasks[0]["area_id"] == "a-bd" and tasks[0]["area_label"] == "BD & Sales"
        assert tasks[1]["area_id"] is None and tasks[1]["area_label"] == "non-area"
        assert tasks[2]["area_label"] == "non-area"

    def test_backstop_drops_inferred_deadline_on_urgent(self):
        tasks = [{"urgency": "H", "deadline": "2026-07-01", "deadline_confidence": "INFERRED"}]
        _normalize_task_urgency_area(tasks, AREAS)
        assert tasks[0]["deadline"] is None
        assert tasks[0]["deadline_confidence"] == "NONE"

    def test_backstop_keeps_explicit_deadline_on_urgent(self):
        tasks = [{"urgency": "H", "deadline": "2026-07-01", "deadline_confidence": "EXPLICIT"}]
        _normalize_task_urgency_area(tasks, AREAS)
        assert tasks[0]["deadline"] == "2026-07-01"
        assert tasks[0]["deadline_confidence"] == "EXPLICIT"


# --------------------------------------------------- manual-injection unify ---
def _areas(areas):
    return patch.object(debrief.supabase_client, "get_areas", return_value=areas)


class TestUnifyManualTask:
    def test_asap_is_urgent_with_no_deadline(self):
        with _areas([]):
            out = debrief._unify_manual_task({"title": "ship the demo ASAP", "description": ""})
        assert out["urgency"] == "H"
        assert out["deadline_confidence"] == "NONE"  # the reminder-visibility fix

    def test_explicit_deadline_is_explicit(self):
        with _areas([]):
            out = debrief._unify_manual_task({"title": "do x", "deadline": "2026-07-01"})
        assert out["deadline_confidence"] == "EXPLICIT"
        assert out["urgency"] == "M"

    def test_explicit_urgency_field_respected(self):
        with _areas([]):
            out = debrief._unify_manual_task({"title": "do x", "urgency": "L"})
        assert out["urgency"] == "L"

    def test_label_passthrough(self):
        with _areas([]):
            out = debrief._unify_manual_task({"title": "do x", "label": "Moldova Pilot"})
        assert out["label"] == "Moldova Pilot"

    def test_area_word_match(self):
        with _areas([{"id": "a-bd", "name": "BD & Sales"}]):
            out = debrief._unify_manual_task({"title": "follow up on the BD & Sales pipeline"})
        assert out["area_id"] == "a-bd"
        assert out["area_label"] == "BD & Sales"

    def test_area_defaults_non_area(self):
        with _areas(AREAS):
            out = debrief._unify_manual_task({"title": "buy more coffee"})
        assert out["area_id"] is None
        assert out["area_label"] == "non-area"
