"""
Tests for Phase 5.2: Outline Generation.

Tests cover:
- Outline generation with mocked data queries
- Graceful degradation: one query fails, rest succeed
- All queries fail → all-unavailable outline
- Telegram formatting (auto/ask confidence)
- Template-driven section ordering (format_prep_document_v2)
- Focus instruction injection
- Timeline mode calculation
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


def _make_event(title="Tech Review", start="2026-03-17T10:00:00+02:00"):
    return {
        "title": title,
        "start": start,
        "attendees": [
            {"displayName": "Eyal Zror", "email": "eyal@cropsight.com"},
            {"displayName": "Roye Tadmor", "email": "roye@cropsight.com"},
        ],
        "location": "Zoom",
    }


# =============================================================================
# Test generate_prep_outline
# =============================================================================

class TestGeneratePrepOutline:
    """Tests for generate_prep_outline()."""

    @pytest.mark.asyncio
    async def test_generates_outline_with_data(self):
        """Should gather data per template queries and return structured outline."""
        with patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.call_llm") as mock_llm:

            mock_db.get_tasks.return_value = [{"title": "Fix bug", "status": "pending"}]
            mock_db.search_embeddings.return_value = []
            mock_db.list_decisions.return_value = []
            mock_db.get_open_questions.return_value = []
            mock_db.get_commitments.return_value = [{"commitment": "Deliver MVP"}]
            mock_db.list_entities.return_value = []
            mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
            mock_llm.return_value = ('["Review tasks", "Check Gantt"]', {})

            from processors.meeting_prep import generate_prep_outline

            outline = await generate_prep_outline(_make_event(), "founders_technical")

            assert outline["meeting_type"] == "founders_technical"
            assert outline["template_name"] == "Founders Technical Review"
            assert len(outline["sections"]) > 0
            assert outline["event_start_time"] == "2026-03-17T10:00:00+02:00"

    @pytest.mark.asyncio
    async def test_graceful_degradation_one_query_fails(self):
        """One failing query should not crash — shows unavailable, rest succeed."""
        with patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.call_llm") as mock_llm:

            # Tasks succeed, but get_commitments raises
            mock_db.get_tasks.return_value = [{"title": "Task 1"}]
            mock_db.search_embeddings.return_value = []
            mock_db.list_decisions.return_value = []
            mock_db.get_open_questions.return_value = []
            mock_db.get_commitments.side_effect = Exception("DB timeout")
            mock_db.list_entities.return_value = []
            mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
            mock_llm.return_value = ('["Item 1"]', {})

            from processors.meeting_prep import generate_prep_outline

            outline = await generate_prep_outline(_make_event(), "founders_technical")

            # Should still return an outline
            assert outline["meeting_type"] == "founders_technical"

            # Find the commitments section — should be unavailable
            statuses = [s["status"] for s in outline["sections"]]
            assert any("unavailable" in s for s in statuses)

            # Other sections should be ok
            ok_sections = [s for s in outline["sections"] if s["status"] == "ok"]
            assert len(ok_sections) > 0

    @pytest.mark.asyncio
    async def test_all_queries_fail_still_returns(self):
        """Even with all queries failing, outline should still be valid (no crash)."""
        with patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.call_llm") as mock_llm:

            mock_db.get_tasks.side_effect = Exception("fail")
            mock_db.search_embeddings.side_effect = Exception("fail")
            mock_db.list_decisions.side_effect = Exception("fail")
            mock_db.get_open_questions.side_effect = Exception("fail")
            mock_db.get_commitments.side_effect = Exception("fail")
            mock_db.list_entities.side_effect = Exception("fail")
            mock_embed.embed_text = AsyncMock(side_effect=Exception("fail"))
            mock_llm.return_value = ('["Fallback"]', {})

            from processors.meeting_prep import generate_prep_outline

            outline = await generate_prep_outline(_make_event(), "founders_technical")

            assert outline["meeting_type"] == "founders_technical"
            # Some queries degrade gracefully (return empty), some raise
            # Key assertion: outline is valid and has sections
            assert len(outline["sections"]) > 0
            # At least commitments should be unavailable (raises directly)
            unavailable = [s for s in outline["sections"] if "unavailable" in s["status"]]
            assert len(unavailable) >= 1

    @pytest.mark.asyncio
    async def test_generic_template_works(self):
        """Generic template with minimal data queries should work."""
        with patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.call_llm") as mock_llm:

            mock_db.get_tasks.return_value = []
            mock_db.search_embeddings.return_value = []
            mock_db.list_decisions.return_value = []
            mock_db.get_open_questions.return_value = []
            mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
            mock_llm.return_value = ('["Item"]', {})

            from processors.meeting_prep import generate_prep_outline

            outline = await generate_prep_outline(_make_event(), "generic")
            assert outline["meeting_type"] == "generic"


# =============================================================================
# Test format_outline_for_telegram
# =============================================================================

class TestFormatOutlineForTelegram:
    """Tests for format_outline_for_telegram()."""

    def test_auto_confidence_no_question(self):
        """Auto confidence should not include 'I think this is...'."""
        from processors.meeting_prep import format_outline_for_telegram

        outline = {
            "event": _make_event(),
            "template_name": "Founders Technical Review",
            "sections": [
                {"name": "Tasks", "status": "ok", "item_count": 3},
                {"name": "Decisions", "status": "ok", "item_count": 2},
            ],
            "suggested_agenda": ["Review tasks", "Check decisions"],
        }

        text = format_outline_for_telegram(outline, confidence="auto")
        assert "I think this is" not in text
        assert "Tech Review" in text
        assert "3 tasks" in text  # Briefing card shows section name + count

    def test_ask_confidence_shows_question(self):
        """Ask confidence should include 'I think this is...'."""
        from processors.meeting_prep import format_outline_for_telegram

        outline = {
            "event": _make_event(),
            "template_name": "Founders Technical Review",
            "signals": ["title_match", "day_match"],
            "sections": [
                {"name": "Tasks", "status": "ok", "item_count": 1},
            ],
            "suggested_agenda": ["Item 1"],
        }

        text = format_outline_for_telegram(outline, confidence="ask")
        assert "I think this is" in text
        assert "Founders Technical Review" in text

    def test_unavailable_sections_shown(self):
        """Unavailable sections should show in outline."""
        from processors.meeting_prep import format_outline_for_telegram

        outline = {
            "event": _make_event(),
            "template_name": "Test",
            "sections": [
                {"name": "Gantt", "status": "unavailable: timeout", "item_count": 0},
            ],
            "suggested_agenda": [],
        }

        text = format_outline_for_telegram(outline)
        assert "unavailable" in text.lower()  # Briefing card shows "Unavailable: gantt"

    def test_agenda_items_shown(self):
        """Suggested agenda items should appear in output."""
        from processors.meeting_prep import format_outline_for_telegram

        outline = {
            "event": _make_event(),
            "template_name": "Test",
            "sections": [],
            "suggested_agenda": ["Review sprint", "Plan next week"],
        }

        text = format_outline_for_telegram(outline)
        assert "Review sprint" in text
        assert "Plan next week" in text


# =============================================================================
# Test format_prep_document_v2
# =============================================================================

class TestFormatPrepDocumentV2:
    """Tests for template-aware document generation."""

    def test_basic_document_generation(self):
        """Should produce valid Markdown with header and sections."""
        from processors.meeting_prep import format_prep_document_v2
        from config.meeting_prep_templates import get_template

        template = get_template("founders_technical")
        sections = [
            {"name": "Roye's Open Tasks", "status": "ok", "data": {"Roye": [{"title": "Fix ML pipeline"}]}, "item_count": 1},
            {"name": "Recent Decisions", "status": "ok", "data": [{"description": "Use PyTorch"}], "item_count": 1},
        ]

        doc = format_prep_document_v2(_make_event(), template, sections)
        assert "# Meeting Prep: Tech Review" in doc
        assert "Founders Technical Review" in doc
        assert "Fix ML pipeline" in doc

    def test_focus_instructions_in_document(self):
        """Focus instructions from Eyal should appear in document."""
        from processors.meeting_prep import format_prep_document_v2
        from config.meeting_prep_templates import get_template

        template = get_template("generic")
        doc = format_prep_document_v2(
            _make_event(), template, [],
            focus_instructions=["Focus on MVP timeline", "Skip stakeholder section"],
        )
        assert "Focus on MVP timeline" in doc
        assert "Skip stakeholder section" in doc
        assert "Focus Areas" in doc

    def test_unavailable_sections_marked(self):
        """Unavailable sections should say so in document."""
        from processors.meeting_prep import format_prep_document_v2
        from config.meeting_prep_templates import get_template

        template = get_template("generic")
        sections = [
            {"name": "Recent Decisions", "status": "unavailable: timeout", "data": None, "item_count": 0},
        ]
        doc = format_prep_document_v2(_make_event(), template, sections)
        assert "unavailable" in doc.lower()


# =============================================================================
# Test format_gantt_for_document
# =============================================================================

class TestFormatGanttForDocument:

    def test_formats_gantt_rows(self):
        from processors.meeting_prep import format_gantt_for_document

        gantt_data = {
            "section": "Product & Technology",
            "items": [
                {"subsection": "ML Pipeline", "status": "On Track", "owner": "Roye", "week": 12},
                {"subsection": "API", "status": "Delayed", "owner": "Eyal", "week": 12},
            ],
        }
        rows = format_gantt_for_document(gantt_data)
        assert len(rows) == 2
        assert rows[0][0] == "Product & Technology"
        assert rows[0][1] == "ML Pipeline"

    def test_empty_gantt_data(self):
        from processors.meeting_prep import format_gantt_for_document

        rows = format_gantt_for_document({})
        assert rows == []


# =============================================================================
# Test calculate_timeline_mode
# =============================================================================

class TestCalculateTimelineMode:

    def test_normal_mode(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(30) == "normal"
        assert calculate_timeline_mode(25) == "normal"

    def test_compressed_mode(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(18) == "compressed"
        assert calculate_timeline_mode(13) == "compressed"

    def test_urgent_mode(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(10) == "urgent"
        assert calculate_timeline_mode(7) == "urgent"

    def test_emergency_mode(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(5) == "emergency"
        assert calculate_timeline_mode(3) == "emergency"

    def test_skip_mode(self):
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(1.5) == "skip"
        assert calculate_timeline_mode(0) == "skip"

    def test_exact_boundaries(self):
        """Test exact boundary values."""
        from processors.meeting_prep import calculate_timeline_mode
        assert calculate_timeline_mode(24) == "compressed"  # <= 24, > 12
        assert calculate_timeline_mode(12) == "urgent"      # <= 12, > 6
        assert calculate_timeline_mode(6) == "emergency"    # <= 6, > 2
        assert calculate_timeline_mode(2) == "skip"         # <= 2


# =============================================================================
# Test Since Last Meeting
# =============================================================================

class TestSinceLastMeeting:
    """Tests for 'since last meeting' data query."""

    @pytest.mark.asyncio
    async def test_since_last_meeting_with_data(self):
        """Should return changes since last meeting of same type."""
        with patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.call_llm") as mock_llm:

            mock_db.get_last_meeting_of_type.return_value = {
                "meeting_date": "2026-03-10",
                "title": "Tech Review",
                "meeting_type": "founders_technical",
            }
            mock_db.get_changes_since.return_value = {
                "tasks_completed": [{"title": "Fix bug", "assignee": "Roye"}],
                "tasks_newly_overdue": [],
                "new_decisions": [{"description": "Use PyTorch"}],
                "commitments_fulfilled": [],
            }
            mock_db.get_tasks.return_value = []
            mock_db.search_embeddings.return_value = []
            mock_db.list_decisions.return_value = []
            mock_db.get_open_questions.return_value = []
            mock_db.get_commitments.return_value = []
            mock_db.list_entities.return_value = []
            mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
            mock_llm.return_value = ('["Review tasks"]', {})

            from processors.meeting_prep import generate_prep_outline

            outline = await generate_prep_outline(_make_event(), "founders_technical")

            # Since Last Meeting should be the first section
            assert outline["sections"][0]["name"] == "Since Last Meeting"
            assert outline["sections"][0]["status"] == "ok"
            data = outline["sections"][0]["data"]
            assert len(data["tasks_completed"]) == 1
            assert len(data["new_decisions"]) == 1

    @pytest.mark.asyncio
    async def test_since_last_meeting_no_prior(self):
        """No prior meeting should return a note, not crash."""
        with patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.call_llm") as mock_llm:

            mock_db.get_last_meeting_of_type.return_value = None
            mock_db.get_tasks.return_value = []
            mock_db.search_embeddings.return_value = []
            mock_db.list_decisions.return_value = []
            mock_db.get_open_questions.return_value = []
            mock_db.get_commitments.return_value = []
            mock_db.list_entities.return_value = []
            mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
            mock_llm.return_value = ('["Review tasks"]', {})

            from processors.meeting_prep import generate_prep_outline

            outline = await generate_prep_outline(_make_event(), "founders_technical")

            since = next((s for s in outline["sections"] if s["name"] == "Since Last Meeting"), None)
            assert since is not None
            assert "note" in since["data"]

    def test_format_since_last_meeting_with_data(self):
        """Should format changes into readable text."""
        from processors.meeting_prep import format_since_last_meeting

        data = {
            "last_meeting_date": "2026-03-10T10:00:00",
            "tasks_completed": [{"title": "Fix bug", "assignee": "Roye"}],
            "tasks_newly_overdue": [{"title": "Deploy", "assignee": "Eyal"}],
            "new_decisions": [{"description": "Use PyTorch"}],
            "commitments_fulfilled": [],
        }
        text = format_since_last_meeting(data)
        assert "2026-03-10" in text
        assert "Fix bug" in text
        assert "Deploy" in text
        assert "PyTorch" in text

    def test_format_since_last_meeting_no_prior(self):
        """Should return note text for first meeting."""
        from processors.meeting_prep import format_since_last_meeting

        data = {"note": "First meeting of this type — no prior data"}
        text = format_since_last_meeting(data)
        assert "First meeting" in text

    def test_format_since_last_meeting_no_changes(self):
        """Empty changes should say so."""
        from processors.meeting_prep import format_since_last_meeting

        data = {
            "last_meeting_date": "2026-03-10",
            "tasks_completed": [],
            "tasks_newly_overdue": [],
            "new_decisions": [],
            "commitments_fulfilled": [],
        }
        text = format_since_last_meeting(data)
        assert "No significant changes" in text


# =============================================================================
# Test Narrative Outline
# =============================================================================

class TestNarrativeOutline:
    """Tests for narrative outline generation."""

    @pytest.mark.asyncio
    async def test_narrative_included_in_outline(self):
        """Outline should include a narrative field."""
        with patch("processors.meeting_prep.supabase_client") as mock_db, \
             patch("processors.meeting_prep.embedding_service") as mock_embed, \
             patch("processors.meeting_prep.call_llm") as mock_llm:

            mock_db.get_tasks.return_value = [{"title": "Fix bug", "status": "pending"}]
            mock_db.search_embeddings.return_value = []
            mock_db.list_decisions.return_value = []
            mock_db.get_open_questions.return_value = []
            mock_db.get_commitments.return_value = [{"commitment": "Deliver MVP"}]
            mock_db.list_entities.return_value = []
            mock_db.get_last_meeting_of_type.return_value = None
            mock_embed.embed_text = AsyncMock(return_value=[0.1] * 1536)
            # First call = narrative, second call = agenda
            mock_llm.side_effect = [
                ("Roye has 1 open task. 1 commitment pending.", {}),
                ('["Review tasks", "Check commitments"]', {}),
            ]

            from processors.meeting_prep import generate_prep_outline

            outline = await generate_prep_outline(_make_event(), "founders_technical")
            assert "narrative" in outline
            assert "Roye" in outline["narrative"]

    def test_narrative_in_telegram_format(self):
        """Telegram format should use narrative instead of data inventory."""
        from processors.meeting_prep import format_outline_for_telegram

        outline = {
            "event": _make_event(),
            "template_name": "Founders Technical Review",
            "narrative": "Roye completed cloud eval. Pipeline still delayed 7 days.",
            "sections": [
                {"name": "Tasks", "status": "ok", "item_count": 3},
            ],
            "suggested_agenda": ["Review pipeline"],
        }

        text = format_outline_for_telegram(outline, confidence="auto")
        assert "cloud eval" in text
        assert "Data:" not in text  # Narrative replaces data inventory

    def test_fallback_when_no_narrative(self):
        """Should fall back to data inventory if no narrative."""
        from processors.meeting_prep import format_outline_for_telegram

        outline = {
            "event": _make_event(),
            "template_name": "Test",
            "sections": [
                {"name": "Tasks", "status": "ok", "item_count": 3},
            ],
            "suggested_agenda": [],
        }

        text = format_outline_for_telegram(outline)
        assert "3 tasks" in text  # Fallback data inventory
