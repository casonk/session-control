from __future__ import annotations

import argparse
from datetime import timedelta

import pytest

from session_control.cli import _parse_duration, _validate_bind


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


def test_parse_duration_accepts_days_weeks_and_hours():
    assert _parse_duration("180d") == timedelta(days=180)
    assert _parse_duration("26w") == timedelta(weeks=26)
    assert _parse_duration("4320h") == timedelta(hours=4320)


def test_parse_duration_rejects_unsupported_units():
    with pytest.raises(argparse.ArgumentTypeError, match="Supported duration units"):
        _parse_duration("6mo")
