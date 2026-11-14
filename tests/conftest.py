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
        # Fixture sessions carry fixed dates; time-based retention is tested explicitly.
        memory_note_retention_days=0,
        memory_archive_retention_days=0,
    )


@pytest.fixture(autouse=True)
def _isolated_portfolio_site(tmp_path_factory, monkeypatch):
    """Resolve the portfolio site from an empty store, never this machine's real one."""
    monkeypatch.setenv("PORTFOLIO_STORE", str(tmp_path_factory.mktemp("portfolio-store")))
    monkeypatch.delenv("PORTFOLIO_SITE_DIR", raising=False)
    monkeypatch.delenv("PORTFOLIO_ALLOWED_HOSTS", raising=False)


@pytest.fixture(autouse=True)
def _no_systemd_scope(monkeypatch):
    """Never wrap tmux in ``systemd-run``, whatever the host (a CI runner has a user manager)."""
    from session_control import actions

    monkeypatch.setattr(actions, "_scope_prefix", lambda: [])


@pytest.fixture(autouse=True)
def _restore_root_logging(monkeypatch):
    """cli.main installs log handlers on the root logger; keep them out of other tests.

    The log file is switched off so macOS and Windows runs never write to a real profile.
    """
    import logging

    monkeypatch.setenv("SESSION_CONTROL_LOG_FILE", "off")
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
