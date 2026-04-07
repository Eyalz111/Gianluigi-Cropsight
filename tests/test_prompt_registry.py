"""
Tests for the YAML prompt registry (Phase 1b).

Verifies loading, fallback, reload, singleton, QA check, and
that YAML content matches Python fallback constants.
"""

import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

from config.prompt_registry import PromptRegistry, PROMPTS_DIR


class TestPromptRegistryLoad:
    """Tests for loading YAML prompt files."""

    def _fresh_registry(self):
        """Create a fresh registry (bypass singleton)."""
        PromptRegistry._instance = None
        return PromptRegistry()

    def test_loads_all_yaml_files(self):
        """Registry loads all prompts from config/prompts/."""
        reg = self._fresh_registry()
        reg.load()
        # Should have 20 prompts (10 system + 2 debrief + 5 weekly + 3 signal)
        assert reg._loaded
        assert len(reg.prompt_names) == 20

    def test_get_returns_content(self):
        """get() returns prompt content for existing prompt."""
        reg = self._fresh_registry()
        reg.load()
        content = reg.get("system_prompt_template")
        assert content is not None
        assert "Gianluigi" in content
        assert len(content) > 100

    def test_get_returns_none_for_missing(self):
        """get() returns None for non-existent prompt name."""
        reg = self._fresh_registry()
        reg.load()
        assert reg.get("nonexistent_prompt") is None

    def test_auto_loads_on_first_get(self):
        """get() triggers load() if not yet loaded."""
        reg = self._fresh_registry()
        assert not reg._loaded
        # First get() should trigger load
        content = reg.get("tone_guardrails")
        assert reg._loaded
        assert content is not None

    def test_prompt_names_property(self):
        """prompt_names returns all loaded prompt names."""
        reg = self._fresh_registry()
        reg.load()
        names = reg.prompt_names
        assert "system_prompt_template" in names
        assert "debrief_system_prompt" in names
        assert "weekly_review_system_prompt" in names
        assert "signal_synthesis_system" in names


class TestPromptRegistryFallback:
    """Tests for fallback behavior when YAML is missing or broken."""

    def _fresh_registry(self):
        PromptRegistry._instance = None
        return PromptRegistry()

    def test_fallback_on_missing_directory(self):
        """Registry handles missing prompts directory gracefully."""
        reg = self._fresh_registry()
        with patch("config.prompt_registry.PROMPTS_DIR", Path("/nonexistent/path")):
            reg.load()
        assert reg._loaded
        assert len(reg.prompt_names) == 0

    def test_fallback_on_bad_yaml(self):
        """Registry logs error on malformed YAML but continues."""
        reg = self._fresh_registry()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write bad YAML
            bad_file = Path(tmpdir) / "bad.yaml"
            bad_file.write_text("not: valid: yaml: [[[", encoding="utf-8")
            with patch("config.prompt_registry.PROMPTS_DIR", Path(tmpdir)):
                reg.load()
        assert reg._loaded
        assert len(reg._load_errors) > 0

    def test_fallback_on_non_list_yaml(self):
        """Registry handles YAML that isn't a list."""
        reg = self._fresh_registry()
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "notlist.yaml"
            bad_file.write_text("key: value\n", encoding="utf-8")
            with patch("config.prompt_registry.PROMPTS_DIR", Path(tmpdir)):
                reg.load()
        assert len(reg._load_errors) == 1
        assert "expected list" in reg._load_errors[0]

    def test_system_prompt_uses_fallback_when_yaml_missing(self):
        """get_system_prompt() falls back to Python constant when YAML empty."""
        reg = self._fresh_registry()
        with patch("config.prompt_registry.PROMPTS_DIR", Path("/nonexistent")):
            reg.load()
        # Force registry to be the one used by system_prompt module
        with patch("core.system_prompt.prompt_registry", reg):
            from core.system_prompt import get_system_prompt
            result = get_system_prompt()
            assert "Gianluigi" in result
            assert len(result) > 1000


class TestPromptRegistryReload:
    """Tests for reload functionality."""

    def _fresh_registry(self):
        PromptRegistry._instance = None
        return PromptRegistry()

    def test_reload_detects_no_changes(self):
        """reload() with no file changes reports no changes."""
        reg = self._fresh_registry()
        reg.load()
        result = reg.reload()
        assert result["prompts_loaded"] == 20
        assert result["changes"] == []
        assert result["errors"] == []

    def test_reload_detects_new_prompt(self):
        """reload() detects newly added prompts."""
        reg = self._fresh_registry()
        reg.load()
        original_count = len(reg.prompt_names)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Copy existing prompts
            import shutil
            for f in PROMPTS_DIR.glob("*.yaml"):
                shutil.copy(f, tmpdir)

            # Add a new prompt
            new_file = Path(tmpdir) / "extra.yaml"
            new_file.write_text(
                '- name: test_new_prompt\n  version: "1.0"\n  description: test\n  content: hello\n',
                encoding="utf-8",
            )

            with patch("config.prompt_registry.PROMPTS_DIR", Path(tmpdir)):
                result = reg.reload()

        assert "NEW: test_new_prompt" in result["changes"]


class TestPromptRegistryHealthCheck:
    """Tests for the health check feature."""

    def _fresh_registry(self):
        PromptRegistry._instance = None
        return PromptRegistry()

    def test_healthy_when_no_issues(self):
        """health_check returns healthy when all files load."""
        reg = self._fresh_registry()
        reg.load()
        health = reg.health_check()
        assert health["status"] == "healthy"
        assert health["prompts_loaded"] == 20
        assert health["load_errors"] == 0

    def test_warning_on_load_errors(self):
        """health_check returns warning when there are load errors."""
        reg = self._fresh_registry()
        reg.load()
        # Simulate a load error
        reg._load_errors = ["fake error"]
        health = reg.health_check()
        assert health["status"] == "warning"
        assert len(health["issues"]) > 0


class TestQASchedulerPromptCheck:
    """Tests for prompt health in the QA scheduler."""

    def test_qa_check_includes_prompt_health(self):
        """run_qa_check() includes prompt_health in checks."""
        from schedulers.qa_scheduler import run_qa_check
        report = run_qa_check()
        assert "prompt_health" in report["checks"]
        ph = report["checks"]["prompt_health"]
        assert "prompts_loaded" in ph
        assert ph["prompts_loaded"] == 20


class TestPromptContentMatch:
    """Verify that YAML content matches Python fallback constants."""

    def test_system_prompt_yaml_matches_python(self):
        """system_prompt_template in YAML matches SYSTEM_PROMPT constant."""
        PromptRegistry._instance = None
        reg = PromptRegistry()
        reg.load()
        from core.system_prompt import SYSTEM_PROMPT
        assert reg.get("system_prompt_template") == SYSTEM_PROMPT

    def test_debrief_prompt_yaml_matches_python(self):
        """debrief_system_prompt in YAML matches Python fallback."""
        PromptRegistry._instance = None
        reg = PromptRegistry()
        reg.load()
        from core.debrief_prompt import _DEBRIEF_SYSTEM_PROMPT_FALLBACK
        assert reg.get("debrief_system_prompt") == _DEBRIEF_SYSTEM_PROMPT_FALLBACK
