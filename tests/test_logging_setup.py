"""Tests for the redacting stderr logging setup."""

from __future__ import annotations

import logging
import sys

import pytest

from session_control import cli
from session_control.logging_setup import RedactingFilter, configure_logging, resolve_level

SECRET = "sk-ant-api03-" + "A" * 40


@pytest.fixture(autouse=True)
def _restore_root():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
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


def test_default_log_path_per_platform(tmp_path):
    from session_control.logging_setup import default_log_path

    env = {"HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert (
        default_log_path("linux", env)
        == tmp_path / ".local/state/session-control/session-control.log"
    )
    assert default_log_path("linux", env | {"XDG_STATE_HOME": str(tmp_path / "st")}).parent == (
        tmp_path / "st" / "session-control"
    )
    assert default_log_path("darwin", env) == (
        tmp_path / "Library/Logs/session-control/session-control.log"
    )
    assert default_log_path("win32", env) == (
        tmp_path / "Local/session-control/Logs/session-control.log"
    )


def test_resolve_log_file_modes(tmp_path, monkeypatch):
    from session_control.logging_setup import resolve_log_file

    monkeypatch.delenv("SESSION_CONTROL_LOG_FILE", raising=False)
    assert resolve_log_file(None, "linux") is None  # the journal covers Linux
    assert resolve_log_file(None, "darwin") is not None
    assert resolve_log_file(None, "win32") is not None
    assert resolve_log_file("off", "win32") is None
    assert resolve_log_file("default", "linux") is not None
    assert resolve_log_file(str(tmp_path / "x.log"), "linux") == tmp_path / "x.log"
    monkeypatch.setenv("SESSION_CONTROL_LOG_FILE", str(tmp_path / "env.log"))
    assert resolve_log_file(None, "linux") == tmp_path / "env.log"


def test_file_logging_is_private_redacted_and_idempotent(tmp_path):
    import stat

    target = tmp_path / "logs" / "sc.log"
    configure_logging("info", str(target))
    configure_logging("info", str(target))
    files = [
        h for h in logging.getLogger().handlers if getattr(h, "_session_control", "") == "file"
    ]
    assert len(files) == 1
    logging.getLogger("session_control.test").warning("secret %s here", SECRET)
    files[0].flush()
    text = target.read_text()
    assert "here" in text and SECRET not in text
    if hasattr(stat, "S_IMODE") and not sys.platform.startswith("win"):
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_unwritable_log_file_does_not_stop_startup(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    configure_logging("info", str(blocker / "sub" / "sc.log"))
    assert "unavailable" in capsys.readouterr().err


def test_rotation_failure_keeps_logging(tmp_path):
    from unittest import mock

    from session_control.logging_setup import _SafeRotatingFileHandler

    handler = _SafeRotatingFileHandler(tmp_path / "r.log", maxBytes=10, backupCount=1)
    logger = logging.getLogger("rot.test")
    logger.propagate = False
    logger.addHandler(handler)
    with mock.patch("os.rename", side_effect=PermissionError("held open")):
        for _ in range(5):
            logger.warning("0123456789 line")
    handler.close()
    assert (tmp_path / "r.log").read_text().count("line") == 5
