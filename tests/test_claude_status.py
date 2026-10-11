from __future__ import annotations

import json
import subprocess
import sys

import pytest

from session_control.claude_status import ClaudeStatusPoller, collect_claude_status


def test_collect_claude_status_sanitizes_auth_details():
    payload = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "user@example.test",
        "orgId": "org-secret",
        "orgName": "Example Org",
        "subscriptionType": "pro",
    }
    status = collect_claude_status(
        (sys.executable, "-c", f"import json; print(json.dumps({payload!r}))"),
        timeout_seconds=5,
    )

    assert status["ok"] is True
    assert status["logged_in"] is True
    assert status["auth_method"] == "claude.ai"
    assert status["api_provider"] == "firstParty"
    assert status["subscription_type"] == "pro"
    assert status["limits_available"] is False
    assert "email" not in json.dumps(status)
    assert "org-secret" not in json.dumps(status)


def test_collect_claude_status_captures_future_limit_fields():
    payload = {
        "loggedIn": True,
        "subscriptionType": "pro",
        "usageLimits": {
            "remaining": 42,
            "resetAt": "2026-06-06T12:00:00Z",
            "email": "user@example.test",
        },
    }
    status = collect_claude_status(
        (sys.executable, "-c", f"import json; print(json.dumps({payload!r}))"),
        timeout_seconds=5,
    )

    assert status["ok"] is True
    assert status["limits_available"] is True
    assert status["limits"]["usageLimits"]["remaining"] == 42
    assert status["limits"]["usageLimits"]["resetAt"] == "2026-06-06T12:00:00Z"
    assert "email" not in status["limits"]["usageLimits"]


@pytest.mark.parametrize("usage_failure", [False, True])
def test_poller_never_resumes_or_saves_a_session(monkeypatch, usage_failure):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command == ("claude", "auth", "status", "--json"):
            payload = {"loggedIn": True}
        else:
            assert "--no-session-persistence" in command
            assert "--print" in command
            assert "/usage" in command
            assert not {"--resume", "--continue", "--session-id"}.intersection(command)
            if usage_failure:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            payload = {"result": "Current session: 25% used"}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr("session_control.claude_status.subprocess.run", run)
    poller = ClaudeStatusPoller(("claude", "auth", "status", "--json"), 300, 10)
    for _ in range(2):
        status = poller.refresh()
        assert status["limits_available"] is (not usage_failure)
        if not usage_failure:
            assert status["usage_limits"]["primary"]["used_percent"] == 25
    assert len(commands) == 4


@pytest.mark.parametrize(
    "auth", [{"loggedIn": False}, {"loggedIn": True, "usageLimits": {"remaining": 42}}]
)
def test_poller_skips_usage_when_unneeded(monkeypatch, auth):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(auth), "")

    monkeypatch.setattr("session_control.claude_status.subprocess.run", run)
    poller = ClaudeStatusPoller(("claude", "auth", "status", "--json"), 300, 10)
    poller.refresh()
    assert commands == [("claude", "auth", "status", "--json")]
