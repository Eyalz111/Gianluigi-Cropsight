"""
Tests for guardrails/content_filter.py

Tests personal content filtering and emotional language reframing.
"""

import pytest


class TestFilterPersonalContent:
    """Tests for filtering personal content from text."""

    def test_removes_health_mentions(self):
        """Health-related content should be removed."""
        from guardrails.content_filter import filter_personal_content

        text = "Eyal mentioned his doctor appointment next week. The MVP is on track."
        result = filter_personal_content(text)

        assert "doctor" not in result.lower()
        assert "MVP is on track" in result

    def test_removes_social_banter(self):
        """Social banter should be removed."""
        from guardrails.content_filter import filter_personal_content

        text = "How was your weekend? The API integration is complete."
        result = filter_personal_content(text)

        assert "weekend" not in result.lower()
        assert "API integration is complete" in result

    def test_preserves_business_content(self):
        """Business content should be preserved."""
        from guardrails.content_filter import filter_personal_content

        text = "We decided to use semantic versioning. The deadline is Friday."
        result = filter_personal_content(text)

        assert "semantic versioning" in result
        assert "deadline" in result

    def test_reframes_business_relevant_personal(self):
        """Personal content with business impact should be reframed."""
        from guardrails.content_filter import filter_personal_content

        text = "Roye's wedding in April means he won't be available that week."
        result = filter_personal_content(text)

        # Should be reframed, not removed
        assert "won't be" in result.lower() or "not available" in result.lower() or "personal commitment" in result.lower()

    def test_handles_empty_text(self):
        """Empty text should return empty."""
        from guardrails.content_filter import filter_personal_content

        result = filter_personal_content("")
        assert result == ""

        result = filter_personal_content(None)
        assert result is None


class TestIdentifyPersonalSections:
    """Tests for identifying personal content sections."""

    def test_identifies_health_content(self):
        """Should identify health-related content."""
        from guardrails.content_filter import identify_personal_sections

        text = "Eyal mentioned his hospital visit. The project is going well."
        flagged = identify_personal_sections(text)

        assert len(flagged) >= 1
        assert any("hospital" in f.get("reason", "").lower() for f in flagged)

    def test_marks_business_relevance(self):
        """Should mark content with business relevance."""
        from guardrails.content_filter import identify_personal_sections

        text = "Due to a medical appointment, the deadline will be delayed."
        flagged = identify_personal_sections(text)

        assert len(flagged) >= 1
        # Should have business relevance flag
        relevant = [f for f in flagged if f.get("has_business_relevance")]
        assert len(relevant) >= 1


class TestIdentifyEmotionalLanguage:
    """Tests for identifying emotional characterizations."""

    def test_identifies_frustrated(self):
        """Should identify 'frustrated' as emotional language."""
        from guardrails.content_filter import identify_emotional_language

        text = "Roye seemed frustrated with the API performance."
        flagged = identify_emotional_language(text)

        assert len(flagged) >= 1
        assert any("frustrated" in f.get("original", "").lower() for f in flagged)

    def test_identifies_angry(self):
        """Should identify 'angry' as emotional language."""
        from guardrails.content_filter import identify_emotional_language

        text = "The client was angry about the delay."
        flagged = identify_emotional_language(text)

        assert len(flagged) >= 1

    def test_provides_suggestions(self):
        """Should provide professional alternatives."""
        from guardrails.content_filter import identify_emotional_language

        text = "Paolo was worried about the timeline."
        flagged = identify_emotional_language(text)

        assert len(flagged) >= 1
        assert any(f.get("suggestion") for f in flagged)

    def test_no_emotional_language(self):
        """Should return empty for neutral text."""
        from guardrails.content_filter import identify_emotional_language

        text = "The team discussed the roadmap and agreed on priorities."
        flagged = identify_emotional_language(text)

        assert len(flagged) == 0


class TestReframeEmotionalLanguage:
    """Tests for reframing emotional language."""

    def test_reframes_frustrated(self):
        """Should reframe 'frustrated' to professional alternative."""
        from guardrails.content_filter import reframe_emotional_language

        text = "Roye was frustrated with the results."
        result = reframe_emotional_language(text)

        assert "frustrated" not in result.lower()
        assert "raised concerns about" in result.lower() or "roye" in result.lower()

    def test_reframes_angry(self):
        """Should reframe 'angry' to professional alternative."""
        from guardrails.content_filter import reframe_emotional_language

        text = "The client seemed angry about the issue."
        result = reframe_emotional_language(text)

        assert "angry" not in result.lower()

    def test_reframes_multiple_emotions(self):
        """Should reframe multiple emotional words."""
        from guardrails.content_filter import reframe_emotional_language

        text = "Eyal was frustrated and Paolo seemed worried."
        result = reframe_emotional_language(text)

        assert "frustrated" not in result.lower()
        assert "worried" not in result.lower()

    def test_preserves_neutral_text(self):
        """Should preserve neutral text unchanged."""
        from guardrails.content_filter import reframe_emotional_language

        text = "The team agreed on the approach."
        result = reframe_emotional_language(text)

        assert result == text


class TestValidateSummaryTone:
    """Tests for validating summary tone."""

    def test_flags_emotional_language(self):
        """Should flag emotional language in summaries."""
        from guardrails.content_filter import validate_summary_tone

        summary = "Roye was frustrated with the API. Paolo seemed worried."
        issues = validate_summary_tone(summary)

        emotional_issues = [i for i in issues if i["type"] == "emotional_language"]
        assert len(emotional_issues) >= 1

    def test_flags_personal_content(self):
        """Should flag personal content in summaries."""
        from guardrails.content_filter import validate_summary_tone

        summary = "Eyal mentioned his doctor visit. The MVP is ready."
        issues = validate_summary_tone(summary)

        personal_issues = [i for i in issues if "personal" in i["type"]]
        assert len(personal_issues) >= 1

    def test_no_issues_for_clean_summary(self):
        """Should return empty for clean summaries."""
        from guardrails.content_filter import validate_summary_tone

        summary = """
        The team discussed MVP progress. Key decisions:
        1. Use semantic versioning for the API
        2. Target Q2 for client launch
        """
        issues = validate_summary_tone(summary)

        # Should have no warnings (info level might still exist)
        warnings = [i for i in issues if i.get("severity") == "warning"]
        assert len(warnings) == 0


class TestCleanSummaryForDistribution:
    """Tests for the main cleaning function."""

    def test_applies_all_filters(self):
        """Should apply all filters to prepare summary for distribution."""
        from guardrails.content_filter import clean_summary_for_distribution

        summary = """
        Eyal mentioned his doctor appointment.
        Roye was frustrated with the API performance.
        The team decided to use semantic versioning.
        """

        result = clean_summary_for_distribution(summary)

        # Personal content removed
        assert "doctor" not in result.lower()
        # Emotional language reframed
        assert "frustrated" not in result.lower()
        # Business content preserved
        assert "semantic versioning" in result

    def test_handles_external_participants(self):
        """Should apply external participant rules."""
        from guardrails.content_filter import clean_summary_for_distribution

        summary = "John Smith from the law firm said we need more documentation."

        result = clean_summary_for_distribution(
            summary,
            external_participants=["John Smith"],
            external_roles={"John Smith": "the legal advisor"}
        )

        assert "John Smith" not in result
        assert "legal advisor" in result.lower()


class TestReframePersonalCircumstance:
    """Tests for reframing personal circumstances."""

    def test_reframes_wedding_mention(self):
        """Should reframe wedding to personal commitment."""
        from guardrails.content_filter import reframe_personal_circumstance

        result = reframe_personal_circumstance(
            personal_context="Roye's wedding in April",
            business_impact=""
        )

        assert "wedding" not in result.lower() or "personal commitment" in result.lower()

    def test_uses_business_impact_if_provided(self):
        """Should use provided business impact."""
        from guardrails.content_filter import reframe_personal_circumstance

        result = reframe_personal_circumstance(
            personal_context="Roye's wedding in April",
            business_impact="Roye will be unavailable April 15-22"
        )

        assert "unavailable April 15-22" in result


class TestExternalParticipantRules:
    """Tests for external participant attribution rules."""

    def test_replaces_name_with_role(self):
        """Should replace external names with role references."""
        from guardrails.content_filter import apply_external_participant_rules

        text = "John mentioned that the contract needs revision."

        result = apply_external_participant_rules(
            text,
            external_names=["John"],
            external_roles={"John": "the client representative"}
        )

        assert "John" not in result
        assert "client representative" in result

    def test_handles_multiple_external_participants(self):
        """Should handle multiple external participants."""
        from guardrails.content_filter import apply_external_participant_rules

        text = "John and Sarah discussed the proposal. John agreed."

        result = apply_external_participant_rules(
            text,
            external_names=["John", "Sarah"],
            external_roles={
                "John": "the investor",
                "Sarah": "the legal counsel"
            }
        )

        assert "John" not in result
        assert "Sarah" not in result

    def test_uses_generic_when_no_role(self):
        """Should use generic reference when no role provided."""
        from guardrails.content_filter import apply_external_participant_rules

        text = "Mike suggested a different approach."

        result = apply_external_participant_rules(
            text,
            external_names=["Mike"],
            external_roles=None
        )

        assert "Mike" not in result
        assert "external contact" in result.lower()
