from __future__ import annotations

from dataclasses import replace

from werkzeug.datastructures import MultiDict

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


class CapturingActions:
    def __init__(self):
        self.calls = []
        self.deleted = []

    def open_in_webterm(self, public_id, *, codex_permission_preset=None):
        self.calls.append((public_id, codex_permission_preset))

    def open_many_in_webterm(self, public_ids, *, codex_permission_preset=None):
        self.calls.append((public_ids, codex_permission_preset))
        return type("BulkOpen", (), {"opened": tuple(public_ids), "errors": ()})()

    def delete_many(self, public_ids):
        self.deleted.append(public_ids)
        return type("BulkDelete", (), {"deleted": tuple(public_ids), "errors": ()})()


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


def test_index_renders_codex_permission_controls(app_config):
    seed_codex(
        app_config.codex_root,
        approval_policy="on-request",
        sandbox_mode="workspace-write",
    )
    app = create_app(app_config)

    response = app.test_client().get("/")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "permissions: workspace-write / on-request" in text
    assert "Launch permissions" in text
    assert "Use Codex config" in text
    assert "Full auto" in text


def test_index_renders_bulk_selection_controls(app_config):
    seed_continue(app_config.continue_root)
    app = create_app(app_config)

    response = app.test_client().get("/")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Select all visible" in text
    assert "Open selected" in text
    assert "Delete selected" in text
    assert "data-session-select" in text


def test_open_route_passes_codex_permission_preset(app_config):
    config = replace(app_config, webterm_url="https://webterm.example.local")
    seed_codex(config.codex_root)
    session = SessionScanner(config).scan(providers=("codex",)).sessions[0]
    actions = CapturingActions()
    app = create_app(config, actions=actions)
    client = app.test_client()
    token = _csrf(client)

    response = client.post(
        f"/sessions/{session.public_id}/open",
        data={"csrf_token": token, "codex_permission_preset": "full-auto"},
        headers={"Origin": "http://localhost"},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "https://webterm.example.local"
    assert actions.calls == [(session.public_id, "full-auto")]


def test_bulk_open_route_passes_selected_sessions(app_config):
    config = replace(app_config, webterm_url="https://webterm.example.local")
    seed_continue(config.continue_root)
    seed_codex(config.codex_root)
    sessions = SessionScanner(config).scan().sessions
    selected = tuple(session.public_id for session in sessions[:2])
    actions = CapturingActions()
    app = create_app(config, actions=actions)
    client = app.test_client()
    token = _csrf(client)

    response = client.post(
        "/sessions/bulk/open",
        data=MultiDict(
            [
                ("csrf_token", token),
                ("session_id", selected[0]),
                ("session_id", selected[1]),
                ("codex_permission_preset", "full-auto"),
            ]
        ),
        headers={"Origin": "http://localhost"},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "https://webterm.example.local"
    assert actions.calls == [(selected, "full-auto")]


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


def test_bulk_delete_route_removes_selected_sessions(app_config):
    seed_continue(app_config.continue_root)
    seed_codex(app_config.codex_root)
    selected = tuple(session.public_id for session in SessionScanner(app_config).scan().sessions)
    app = create_app(app_config)
    client = app.test_client()
    token = _csrf(client)

    response = client.post(
        "/sessions/bulk/delete",
        data=MultiDict(
            [
                ("csrf_token", token),
                ("session_id", selected[0]),
                ("session_id", selected[1]),
            ]
        ),
        headers={"Origin": "http://localhost"},
    )

    assert response.status_code == 302
    assert SessionScanner(app_config).scan().sessions == ()
