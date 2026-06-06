from __future__ import annotations

from session_control.scanner import SessionScanner
from session_control.web import create_app
from tests.helpers import seed_claude, seed_codex, seed_continue


class StaticClaudeStatus:
    def __init__(self, status):
        self.status = status
        self.started = False

    def start(self):
        self.started = True

    def snapshot(self):
        return self.status


def _csrf(client):
    response = client.get("/")
    assert response.status_code == 200
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_index_renders_sessions_and_resume_command(app_config):
    seed_continue(app_config.continue_root)
    app = create_app(app_config)

    response = app.test_client().get("/")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Continue repo summary" in text
    assert "cn --fork 9f4b464d-495f-432d-8d16-31aa4e7ac7ea" in text


def test_index_renders_codex_usage_summary(app_config):
    seed_codex(app_config.codex_root, token_count=True)
    app = create_app(app_config)

    response = app.test_client().get("/")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Codex last turn" in text
    assert "24,000 / 258,400" in text
    assert "125,000" in text
    assert "33%" in text
    assert "17%" in text


def test_index_renders_claude_usage_summary_without_limits(app_config):
    seed_claude(app_config.claude_root, token_count=True)
    claude_status = StaticClaudeStatus(
        {
            "ok": True,
            "subscription_type": "pro",
            "limits_available": False,
            "message": "Claude CLI auth status does not expose usage or limit windows.",
        }
    )
    app = create_app(app_config, claude_status_poller=claude_status)

    response = app.test_client().get("/")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert claude_status.started is True
    assert "Claude last turn" in text
    assert "346" in text
    assert "claude-sonnet-4-6" in text
    assert "200 read" in text
    assert "100 created" in text
    assert "Claude limits" in text
    assert "not exposed" in text
    assert "pro" in text


def test_delete_route_requires_csrf(app_config):
    seed_continue(app_config.continue_root)
    app = create_app(app_config)
    session = SessionScanner(app_config).scan(providers=("continue",)).sessions[0]

    response = app.test_client().post(f"/sessions/{session.public_id}/delete")

    assert response.status_code == 403


def test_delete_route_removes_session(app_config):
    seed_continue(app_config.continue_root)
    app = create_app(app_config)
    client = app.test_client()
    token = _csrf(client)
    session = SessionScanner(app_config).scan(providers=("continue",)).sessions[0]

    response = client.post(
        f"/sessions/{session.public_id}/delete",
        data={"csrf_token": token},
        headers={"Origin": "http://localhost"},
    )

    assert response.status_code == 302
    assert not SessionScanner(app_config).scan(providers=("continue",)).sessions
