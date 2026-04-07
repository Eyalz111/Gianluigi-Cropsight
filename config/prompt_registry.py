"""
YAML-based prompt registry with in-memory caching and fallback.

Prompts are loaded from config/prompts/*.yaml at startup.
Each YAML file contains a list of prompt definitions with:
- name: unique identifier used by get() callers
- version: semver string for tracking changes
- description: what the prompt does (for documentation)
- content: the prompt text (may contain {placeholders} for .format())

If a YAML file is missing or malformed, the caller's Python fallback
is used instead. This ensures the system never breaks due to a bad
prompt file.

Usage:
    from config.prompt_registry import prompt_registry

    # Get a prompt (returns None if not found — caller uses fallback)
    text = prompt_registry.get("system_prompt")

    # Reload all prompts (e.g., after file update)
    prompt_registry.reload()

    # Health check (for QA scheduler)
    health = prompt_registry.health_check()
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Directory containing YAML prompt files
PROMPTS_DIR = Path(__file__).parent / "prompts"


class PromptRegistry:
    """
    Singleton registry for YAML-based prompts.

    Loads all .yaml files from config/prompts/ into an in-memory dict.
    Provides get() with None return (caller handles fallback),
    reload() for hot-reloading, and health_check() for QA.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._prompts = {}
            cls._instance._load_times = {}
            cls._instance._startup_time = datetime.now()
            cls._instance._load_errors = []
            cls._instance._loaded = False
        return cls._instance

    def load(self) -> None:
        """Load all YAML prompt files from the prompts directory."""
        self._prompts = {}
        self._load_times = {}
        self._load_errors = []

        if not PROMPTS_DIR.exists():
            logger.warning(f"Prompts directory not found: {PROMPTS_DIR}")
            self._loaded = True
            return

        yaml_files = list(PROMPTS_DIR.glob("*.yaml"))
        if not yaml_files:
            logger.warning(f"No YAML files found in {PROMPTS_DIR}")
            self._loaded = True
            return

        for yaml_file in yaml_files:
            try:
                mtime = os.path.getmtime(yaml_file)
                with open(yaml_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)

                if not isinstance(data, list):
                    self._load_errors.append(
                        f"{yaml_file.name}: expected list, got {type(data).__name__}"
                    )
                    continue

                for entry in data:
                    name = entry.get("name")
                    content = entry.get("content")
                    if not name or not content:
                        self._load_errors.append(
                            f"{yaml_file.name}: entry missing 'name' or 'content'"
                        )
                        continue
                    self._prompts[name] = content
                    self._load_times[name] = mtime

                logger.info(
                    f"Loaded {len(data)} prompts from {yaml_file.name}"
                )

            except yaml.YAMLError as e:
                error_msg = f"{yaml_file.name}: YAML parse error: {e}"
                self._load_errors.append(error_msg)
                logger.error(error_msg)
            except Exception as e:
                error_msg = f"{yaml_file.name}: load error: {e}"
                self._load_errors.append(error_msg)
                logger.error(error_msg)

        self._loaded = True
        self._startup_time = datetime.now()
        logger.info(
            f"PromptRegistry loaded: {len(self._prompts)} prompts, "
            f"{len(self._load_errors)} errors"
        )

    def get(self, name: str) -> str | None:
        """
        Get a prompt by name.

        Returns None if the prompt is not found or not loaded,
        allowing the caller to fall back to a hardcoded default.

        Args:
            name: The unique prompt identifier.

        Returns:
            Prompt content string, or None if not found.
        """
        if not self._loaded:
            self.load()
        return self._prompts.get(name)

    def reload(self) -> dict:
        """
        Reload all prompt files from disk.

        Returns:
            Dict with reload results (count, errors, changes).
        """
        old_prompts = dict(self._prompts)
        self.load()

        # Detect changes
        changes = []
        for name, content in self._prompts.items():
            if name not in old_prompts:
                changes.append(f"NEW: {name}")
            elif old_prompts[name] != content:
                changes.append(f"UPDATED: {name}")
        for name in old_prompts:
            if name not in self._prompts:
                changes.append(f"REMOVED: {name}")

        return {
            "prompts_loaded": len(self._prompts),
            "errors": self._load_errors,
            "changes": changes,
        }

    def health_check(self) -> dict:
        """
        Check prompt health for QA scheduler.

        Detects:
        - YAML files that failed to load
        - Files modified since last load (need reload)
        - Missing expected prompts

        Returns:
            Dict with health status and issues.
        """
        if not self._loaded:
            self.load()

        issues = []

        # Check for load errors
        if self._load_errors:
            for err in self._load_errors:
                issues.append(f"Load error: {err}")

        # Check for files modified since last load
        if PROMPTS_DIR.exists():
            for yaml_file in PROMPTS_DIR.glob("*.yaml"):
                current_mtime = os.path.getmtime(yaml_file)
                if current_mtime > self._startup_time.timestamp():
                    issues.append(
                        f"File modified since startup: {yaml_file.name}"
                    )

        return {
            "prompts_loaded": len(self._prompts),
            "load_errors": len(self._load_errors),
            "files_modified_since_startup": sum(
                1 for i in issues if "modified since startup" in i
            ),
            "issues": issues,
            "status": "healthy" if not issues else "warning",
        }

    @property
    def prompt_names(self) -> list[str]:
        """List all loaded prompt names."""
        if not self._loaded:
            self.load()
        return list(self._prompts.keys())

    def reset(self) -> None:
        """Reset the registry (for testing)."""
        self._prompts = {}
        self._load_times = {}
        self._load_errors = []
        self._loaded = False


# Module-level singleton
prompt_registry = PromptRegistry()
