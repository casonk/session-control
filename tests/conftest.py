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
        grok_root=tmp_path / "grok",
        agy_root=tmp_path / "agy",
        trash_dir=tmp_path / "trash",
        secret_key="test-secret",
    )


@pytest.fixture(autouse=True)
def _isolated_portfolio_site(tmp_path_factory, monkeypatch):
    """Resolve the portfolio site from an empty store, never this machine's real one."""
    monkeypatch.setenv("PORTFOLIO_STORE", str(tmp_path_factory.mktemp("portfolio-store")))
    monkeypatch.delenv("PORTFOLIO_SITE_DIR", raising=False)
    monkeypatch.delenv("PORTFOLIO_ALLOWED_HOSTS", raising=False)
