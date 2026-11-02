"""Failure paths, sanitising and usage parsing of the Claude status poller.

The poller shells out to the real ``claude`` CLI, which can cost tokens and shows
account details, so ``subprocess.run`` is replaced in every test here: none of them
may start it. What the poller must do with whatever comes back is the point: report a
failure as a status rather than raise, and never let account or credential fields reach
the page.
"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from session_control import claude_status
from session_control.claude_status import (
    ClaudeStatusPoller,
    _extract_limit_sections,
    _parse_reset_time,
    _parse_usage_text,
    _sanitize_value,
    _truncate_line,
    collect_claude_status,
    collect_claude_usage,
)

RUN = "session_control.claude_status.subprocess.run"


def done(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# --------------------------------------------------------------------------- collect_claude_status


def test_an_empty_command_is_reported_not_run():
    with mock.patch(RUN) as run:
        status = collect_claude_status((), 5)
    assert status["ok"] is False and "empty" in status["message"]
    run.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "text"),
    [
        (FileNotFoundError("claude"), "failed: claude"),
        (PermissionError("denied"), "failed: denied"),
        (subprocess.TimeoutExpired("claude", 5), "timed out"),
    ],
)
def test_a_command_that_cannot_run_is_a_status_not_an_exception(failure, text):
    with mock.patch(RUN, side_effect=failure):
        status = collect_claude_status(("claude", "auth", "status"), 5)
    assert status["ok"] is False
    assert text in status["message"]
    assert status["limits_available"] is False and status["limits"] == {}


def test_the_command_runs_with_a_floor_of_one_second_and_no_shell():
    with mock.patch(RUN, return_value=done("{}")) as run:
        collect_claude_status(("claude", "auth", "status"), 0)
    assert run.call_args.args[0] == ("claude", "auth", "status")
    assert run.call_args.kwargs["timeout"] == 1
    assert run.call_args.kwargs["check"] is False
    assert "shell" not in run.call_args.kwargs


def test_a_failing_command_reports_the_first_line_of_its_error_truncated():
    long_line = "x" * 400
    for result, expected in (
        (done(stderr="first line\nsecond", returncode=1), "first line"),
        (done(stdout="from stdout", returncode=2), "from stdout"),
        (done(returncode=3), "Claude status command failed."),
        (done(stderr=long_line, returncode=1), "x" * 180),
    ):
        with mock.patch(RUN, return_value=result):
            assert collect_claude_status(("c",), 5)["message"] == expected


@pytest.mark.parametrize(
    ("stdout", "text"),
    [
        ("not json", "did not return JSON"),
        ("[1, 2]", "non-object JSON"),
        ('"text"', "non-object JSON"),
    ],
)
def test_output_that_is_not_a_json_object_is_reported(stdout, text):
    with mock.patch(RUN, return_value=done(stdout)):
        status = collect_claude_status(("c",), 5)
    assert status["ok"] is False and text in status["message"]


def test_empty_output_means_not_logged_in_rather_than_an_error():
    with mock.patch(RUN, return_value=done("")):
        status = collect_claude_status(("c",), 5)
    assert status["ok"] is True and status["logged_in"] is False
    assert status["auth_method"] == "" and status["limits_available"] is False


def test_the_message_says_whether_limits_were_exposed():
    with mock.patch(RUN, return_value=done(json.dumps({"loggedIn": True, "quota": {"left": 5}}))):
        assert "exposed limit" in collect_claude_status(("c",), 5)["message"]
    with mock.patch(RUN, return_value=done(json.dumps({"loggedIn": True}))):
        assert "does not expose" in collect_claude_status(("c",), 5)["message"]


def test_truncate_line_handles_empty_input():
    assert _truncate_line("") == "Claude status command failed."
    assert _truncate_line("\n\nsecond") == "Claude status command failed."
    assert _truncate_line("ok\nmore") == "ok"


# --------------------------------------------------------------------------- sanitising


def test_sensitive_keys_are_removed_at_every_depth_and_in_lists():
    value = {
        "remaining": 5,
        "Email": "a@b.test",
        "nested": {"apiKey": "k", "orgName": "o", "left": 2, "accountId": "x"},
        "windows": [{"token": "t", "used": 1}, {"secret": "s"}],
        "SESSION_TOKEN": "t",
    }
    assert _sanitize_value(value) == {
        "remaining": 5,
        "nested": {"left": 2},
        "windows": [{"used": 1}],
    }


def test_values_are_kept_by_type_and_long_strings_are_cut():
    assert _sanitize_value(True) is True
    assert _sanitize_value(3) == 3 and _sanitize_value(2.5) == 2.5
    assert _sanitize_value("y" * 500) == "y" * 160
    assert _sanitize_value(None) is None
    assert _sanitize_value(object()) is None
    assert _sanitize_value(("a", "tuple")) is None


def test_empty_results_are_dropped_rather_than_kept_as_noise():
    assert _sanitize_value({"a": {}, "b": [], "c": "", "d": None, "e": {"f": {}}}) == {}
    assert _sanitize_value([{}, []]) == []


def test_only_limit_like_top_level_keys_are_extracted():
    data = {
        "loggedIn": True,
        "usageLimits": {"remaining": 3, "email": "x"},
        "QuotaWindow": "5h",
        "resetAt": "2026-06-06T12:00:00Z",
        "email": "a@b.test",
        "emptyLimit": {},
        "tokenLimit": {"token": "secret"},
    }
    assert _extract_limit_sections(data) == {
        "usageLimits": {"remaining": 3},
        "QuotaWindow": "5h",
        "resetAt": "2026-06-06T12:00:00Z",
    }


# --------------------------------------------------------------------------- usage


def test_usage_runs_in_a_fresh_unsaved_session():
    with mock.patch(RUN, return_value=done(json.dumps({"result": "5% used"}))) as run:
        collect_claude_usage(7)
    command = run.call_args.args[0]
    assert command[0] == "claude"
    assert "--no-session-persistence" in command
    assert command[command.index("--print") + 1] == "/usage"
    assert run.call_args.kwargs["timeout"] == 7


@pytest.mark.parametrize(
    ("outcome", "text"),
    [
        ({"side_effect": OSError("gone")}, "claude /usage failed: gone"),
        ({"side_effect": subprocess.TimeoutExpired("claude", 5)}, "timed out"),
        ({"return_value": done(returncode=1)}, "exited with error"),
        ({"return_value": done("nope")}, "did not return JSON"),
        ({"return_value": done("[1]")}, "non-object JSON"),
        ({"return_value": done(json.dumps({"result": "nothing useful"}))}, "No usage data"),
        ({"return_value": done("{}")}, "No usage data"),
    ],
)
def test_usage_failures_are_reported_as_data(outcome, text):
    with mock.patch(RUN, **outcome):
        usage = collect_claude_usage(5)
    assert usage["ok"] is False and text in usage["message"]
    assert usage["limits_available"] is False


def test_two_usage_windows_become_primary_and_secondary():
    text = "Session: 12.5% used - resets Jun 6, 3pm (UTC)\nWeek: 40% used\nignored line"
    usage = _parse_usage_text("now", text)
    assert usage["ok"] is True and usage["limits_available"] is True
    assert usage["primary"]["used_percent"] == 12.5
    assert usage["secondary"] == {"used_percent": 40.0, "resets_at": ""}


def test_a_single_window_has_no_secondary():
    usage = _parse_usage_text("now", "7% used")
    assert usage["primary"]["used_percent"] == 7.0 and usage["secondary"] is None


REF = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("resets Jun 6, 3pm (UTC)", "2026-06-06T15:00:00Z"),
        ("Resets JUN 6, 3:30PM (UTC)", "2026-06-06T15:30:00Z"),
        ("resets Jun 6, 12am (UTC)", "2026-06-06T00:00:00Z"),
        ("resets Jun 6, 12pm (UTC)", "2026-06-06T12:00:00Z"),
        ("resets Jun 6, 9am (Etc/GMT-2)", "2026-06-06T07:00:00Z"),
        ("no reset time here", ""),
        ("resets Jun 6, 3pm (Not/AZone)", ""),
        ("resets Foo 6, 3pm (UTC)", ""),
        ("resets Jun 31, 3pm (UTC)", ""),
        ("resets Feb 30, 3pm (UTC)", ""),
    ],
)
def test_reset_times_are_parsed_to_utc_or_left_blank(line, expected):
    assert _parse_reset_time(line, REF) == expected


def test_a_reset_date_already_past_this_year_means_next_year():
    december = datetime(2026, 12, 20, tzinfo=timezone.utc)
    assert _parse_reset_time("resets Jan 5, 1am (UTC)", december) == "2027-01-05T01:00:00Z"


# --------------------------------------------------------------------------- the poller


def test_intervals_have_a_floor_of_one_second():
    poller = ClaudeStatusPoller(("c",), 0, -3)
    assert (poller.interval_seconds, poller.timeout_seconds) == (1, 1)


def test_a_snapshot_is_a_copy_and_none_until_the_first_refresh():
    poller = ClaudeStatusPoller(("c",), 60, 5)
    assert poller.snapshot() is None
    with mock.patch.object(claude_status, "collect_claude_status", return_value={"ok": False}):
        poller.refresh()
    snapshot = poller.snapshot()
    snapshot["ok"] = "tampered"
    assert poller.snapshot() == {"ok": False}


def test_usage_limits_are_fetched_only_for_a_logged_in_status_without_limits():
    poller = ClaudeStatusPoller(("c",), 60, 5)
    usage = {"ok": True, "primary": {"used_percent": 1}}
    cases = (
        ({"ok": True, "logged_in": True, "limits_available": False}, True),
        ({"ok": True, "logged_in": True, "limits_available": True}, False),
        ({"ok": True, "logged_in": False, "limits_available": False}, False),
        ({"ok": False, "logged_in": True, "limits_available": False}, False),
    )
    for status, fetched in cases:
        with (
            mock.patch.object(claude_status, "collect_claude_status", return_value=dict(status)),
            mock.patch.object(claude_status, "collect_claude_usage", return_value=usage) as collect,
        ):
            result = poller.refresh()
        assert collect.called is fetched, status
        assert ("usage_limits" in result) is fetched


def test_a_failed_usage_lookup_leaves_the_status_without_limits():
    poller = ClaudeStatusPoller(("c",), 60, 5)
    base = {"ok": True, "logged_in": True, "limits_available": False}
    with (
        mock.patch.object(claude_status, "collect_claude_status", return_value=dict(base)),
        mock.patch.object(claude_status, "collect_claude_usage", return_value={"ok": False}),
    ):
        result = poller.refresh()
    assert result["limits_available"] is False and "usage_limits" not in result


def test_the_loop_refreshes_until_stopped_and_waits_the_interval():
    poller = ClaudeStatusPoller(("c",), 42, 5)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        poller.stop()
        return True

    poller._stop = SimpleNamespace(is_set=poller._stop.is_set, set=poller._stop.set, wait=wait)
    with mock.patch.object(poller, "refresh") as refresh:
        poller._run()
    refresh.assert_called_once_with()
    assert waits == [42]


def test_a_stopped_poller_does_not_refresh_at_all():
    poller = ClaudeStatusPoller(("c",), 1, 5)
    poller.stop()
    with mock.patch.object(poller, "refresh") as refresh:
        poller._run()
    refresh.assert_not_called()


def test_start_runs_one_daemon_thread_and_a_second_start_is_a_no_op():
    poller = ClaudeStatusPoller(("c",), 1, 5)
    started = threading.Event()
    release = threading.Event()

    def blocking_run():
        started.set()
        release.wait(5)

    poller._run = blocking_run  # type: ignore[method-assign]
    poller.start()
    assert started.wait(5)
    thread = poller._thread
    assert thread.daemon is True and thread.name == "claude-status-poller"
    poller.start()
    assert poller._thread is thread
    release.set()
    thread.join(5)
