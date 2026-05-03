from __future__ import annotations

from pathlib import Path

import pytest

from session_control.config import AppConfig


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        codex_root=tmp_path / "codex",
        claude_root=tmp_path / "claude",
        continue_root=tmp_path / "continue",
        copilot_root=tmp_path / "copilot",
        trash_dir=tmp_path / "trash",
        secret_key="test-secret",
    )
