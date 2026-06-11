"""PR3 (updated for the 2026-06 category realignment) — extraction + manual
injection populate urgency + a canonical category.

Tests the two pure seams: the post-extraction normalizer
(transcript_processor._normalize_task_urgency_area — urgency coercion, category
canonicalization against the live Gantt areas, the no-invented-dates guardrail)
and the manual-injection unifier (debrief._unify_manual_task). The per-task
area_id/area_label pair is gone — category carries the Gantt-area taxonomy.
"""
from unittest.mock import patch

import processors.debrief as debrief
from processors.transcript_processor import _normalize_task_urgency_area

AREAS = [
    {"id": "a-bd", "name": "SALES & BUSINESS DEVELOPMENT"},
    {"id": "a-pt", "name": "PRODUCT & TECHNOLOGY"},
]


# -------------------------------------------------- extraction normalizer -----
class TestNormalizer:
    def test_urgency_coerced(self):
        tasks = [{"urgency": "h"}, {"urgency": "x"}, {"urgency": None}, {"urgency": "L"}]
        _normalize_task_urgency_area(tasks, AREAS)
        assert [t["urgency"] for t in tasks] == ["H", "M", "M", "L"]

    def test_category_canonicalized(self):
        tasks = [
            {"category": "sales & business development"},  # live-area match
            {"category": "bd & sales"},                    # legacy taxonomy
            {"category": "nonsense"},                      # unknown — kept as-is
            {"category": None},                            # blank -> General
        ]
        _normalize_task_urgency_area(tasks, AREAS)
        assert tasks[0]["category"] == "SALES & BUSINESS DEVELOPMENT"
        assert tasks[1]["category"] == "SALES & BUSINESS DEVELOPMENT"
        assert tasks[2]["category"] == "nonsense"
        assert tasks[3]["category"] == "General"
        # the area FK/label pair is gone from the task surface
        for t in tasks:
            assert "area_id" not in t
            assert "area_label" not in t

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

    def test_category_hint_canonicalized(self):
        # the LLM's legacy-taxonomy hint resolves through the legacy map
        with _areas(AREAS):
            out = debrief._unify_manual_task({"title": "do x", "category": "bd & sales"})
        assert out["category"] == "SALES & BUSINESS DEVELOPMENT"

    def test_category_word_match(self):
        with _areas([{"id": "a-bd", "name": "SALES & BUSINESS DEVELOPMENT"}]):
            out = debrief._unify_manual_task(
                {"title": "follow up on the SALES & BUSINESS DEVELOPMENT pipeline"}
            )
        assert out["category"] == "SALES & BUSINESS DEVELOPMENT"

    def test_category_defaults_general(self):
        with _areas(AREAS):
            out = debrief._unify_manual_task({"title": "buy more coffee"})
        assert out["category"] == "General"
