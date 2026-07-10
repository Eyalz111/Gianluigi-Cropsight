"""Decision-vs-action-item disambiguation in the extraction prompt (2026-07).

Eyal: decisions were being confused with action items at extraction. The prompt
now carries an explicit disambiguation block (`_decision_extraction_rules`) with a
test + examples so choices route to "decisions" and doable work routes to "tasks"
(and neither is double-counted). These pin its content and its wiring.
"""
import inspect

import pytest

try:
    import processors.transcript_processor as tp
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import transcript_processor ({e})", allow_module_level=True)


class TestDecisionExtractionRules:
    def test_disambiguates_decision_from_action_item(self):
        rules = tp._decision_extraction_rules()
        assert "DECISION vs ACTION ITEM" in rules      # the explicit distinction
        assert "WHO does WHAT" in rules                # the action-item test
        assert "CHOICE" in rules                       # a decision is a choice
        assert "NOT decisions" in rules                # negative guidance
        assert "twice" in rules.lower()                # don't double-count choice+task

    def test_names_the_common_confusions_as_not_decisions(self):
        rules = tp._decision_extraction_rules().lower()
        # promises / status updates / open questions are NOT decisions
        assert "i'll send" in rules or "let me check" in rules
        assert "open_question" in rules

    def test_keeps_structured_decision_requirements(self):
        rules = tp._decision_extraction_rules()
        assert "rationale" in rules
        assert "options_considered" in rules
        assert "confidence" in rules
        assert "review_date" in rules

    def test_wired_into_extraction_system_prompt(self):
        # The prompt string carries the {decision_rules} placeholder AND the
        # extraction function replaces it with the helper — so the disambiguation
        # actually reaches the model's system prompt.
        source = inspect.getsource(tp.extract_structured_data)
        assert "{decision_rules}" in source
        assert "_decision_extraction_rules()" in source

    def test_action_item_section_cross_references_decisions(self):
        source = inspect.getsource(tp.extract_structured_data)
        assert "DISTINCT from a DECISION" in source

    def test_helper_has_no_stray_format_placeholders(self):
        # The block is spliced via str.replace of "{decision_rules}"; it must not
        # itself contain a "{decision_rules}" token (would leave the prompt dirty).
        assert "{decision_rules}" not in tp._decision_extraction_rules()
