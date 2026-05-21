"""
Tests for the v2.5 PR5 extraction-muzzle toggle.

The "aim for 3-7 action items" consolidation cap is the muzzle. It must stay on
by default and switch to a completeness-first rule when EXTRACTION_MUZZLE_REMOVED
is set (the cutover).
"""

import pytest

try:
    import processors.transcript_processor as tp
except Exception as e:
    pytest.skip(f"cannot import transcript_processor ({e})", allow_module_level=True)


class TestMuzzleToggle:
    def test_muzzle_on_by_default(self, monkeypatch):
        monkeypatch.setattr(tp.settings, "EXTRACTION_MUZZLE_REMOVED", False)
        rule = tp._consolidation_rule()
        assert "Aim for 3-7 action items" in rule

    def test_muzzle_removed_drops_cap(self, monkeypatch):
        monkeypatch.setattr(tp.settings, "EXTRACTION_MUZZLE_REMOVED", True)
        rule = tp._consolidation_rule()
        assert "3-7" not in rule
        assert "Completeness over brevity" in rule

    def test_both_modes_keep_same_deliverable_merge(self, monkeypatch):
        # Genuine same-deliverable consolidation guidance survives either way.
        monkeypatch.setattr(tp.settings, "EXTRACTION_MUZZLE_REMOVED", False)
        assert "deliverable" in tp._consolidation_rule()
        monkeypatch.setattr(tp.settings, "EXTRACTION_MUZZLE_REMOVED", True)
        assert "deliverable" in tp._consolidation_rule()
