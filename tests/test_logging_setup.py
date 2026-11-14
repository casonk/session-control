"""Tests for the redacting stderr logging setup."""

from __future__ import annotations

import logging

import pytest

from session_control import cli
from session_control.logging_setup import RedactingFilter, configure_logging, resolve_level

SECRET = "sk-ant-api03-" + "A" * 40


@pytest.fixture(autouse=True)
def _restore_root():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_resolve_level_prefers_argument_then_env(monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_LOG_LEVEL", "warning")
    assert resolve_level(None) == logging.WARNING
    assert resolve_level("debug") == logging.DEBUG
    assert resolve_level("nonsense") == logging.INFO
    monkeypatch.delenv("SESSION_CONTROL_LOG_LEVEL")
    assert resolve_level(None) == logging.INFO


def test_configure_logging_is_idempotent_and_redacts(capsys):
    configure_logging("info")
    configure_logging("info")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_session_control", False)]
    assert len(ours) == 1
    logging.getLogger("session_control.test").warning("token %s leaked", SECRET)
    err = capsys.readouterr().err
    assert "leaked" in err and SECRET not in err
    assert "WARNING session_control.test:" in err


def test_filter_redacts_exception_text():
    try:
        raise RuntimeError(f"bad {SECRET}")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, "f", 1, "failed", None, sys.exc_info())
    RedactingFilter().filter(record)
    assert SECRET not in (record.exc_text or "")


def test_cli_log_level_flag_sets_root_level(app_config):
    from unittest import mock

    with mock.patch.object(cli.AppConfig, "from_env", return_value=app_config):
        assert cli.main(["--log-level", "debug", "memory", "failures"]) == 0
    assert logging.getLogger().level == logging.DEBUG
