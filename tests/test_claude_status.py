from __future__ import annotations

import json
import sys

from session_control.claude_status import collect_claude_status


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
