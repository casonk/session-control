from __future__ import annotations

import argparse
from datetime import timedelta

import pytest

from session_control.cli import (
    _build_parser,
    _parse_duration,
    _relative_short_subagent_age,
    _validate_bind,
)


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


def test_relative_short_subagent_age_accepts_equivalent_forms():
    baseline = timedelta(days=180)

    assert _relative_short_subagent_age(baseline, percent="25%") == timedelta(days=45)
    assert _relative_short_subagent_age(baseline, multiplier="4x") == timedelta(days=45)


@pytest.mark.parametrize(
    ("percent", "multiplier"),
    [("0%", None), ("101%", None), (None, "0x"), (None, "NaNx"), ("25%", "4x")],
)
def test_relative_short_subagent_age_rejects_invalid_factors(percent, multiplier):
    with pytest.raises(argparse.ArgumentTypeError):
        _relative_short_subagent_age(timedelta(days=180), percent=percent, multiplier=multiplier)


def test_prune_relative_flags_are_mutually_exclusive():
    parser = _build_parser()

    assert (
        parser.parse_args(["prune", "--short-subagent-percent", "25%"]).short_subagent_percent
        == "25%"
    )
    assert (
        parser.parse_args(["prune", "--short-subagent-multiplier", "4x"]).short_subagent_multiplier
        == "4x"
    )
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["prune", "--short-subagent-percent", "25%", "--short-subagent-multiplier", "4x"]
        )
