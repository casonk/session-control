from __future__ import annotations

import pytest

from session_control.cli import _validate_bind


def test_validate_bind_allows_loopback():
    _validate_bind("127.0.0.1")
    _validate_bind("localhost")


def test_validate_bind_refuses_remote_without_opt_in(monkeypatch):
    monkeypatch.delenv("SESSION_CONTROL_ALLOW_REMOTE", raising=False)

    with pytest.raises(SystemExit, match="non-loopback"):
        _validate_bind("0.0.0.0")


def test_validate_bind_allows_remote_with_opt_in(monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_ALLOW_REMOTE", "1")

    _validate_bind("0.0.0.0")
