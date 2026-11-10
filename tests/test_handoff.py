"""Cross-provider handoff: brief contents, launch command and web route."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from session_control import handoff
from session_control.actions import SessionActionError, SessionActionService
from session_control.scanner import SessionScanner
from session_control.web import create_app
from tests.helpers import seed_claude, seed_codex, write_jsonl


def _sessions(app_config):
    return {s.provider: s for s in SessionScanner(app_config).scan().sessions}


def test_targets_exclude_the_sessions_own_provider():
    everywhere = {"claude", "codex", "grok", "continue", "copilot", "agy"}
    assert set(handoff.HANDOFF_TARGETS) == everywhere
    for provider in everywhere:
        assert set(handoff.handoff_targets_for(provider)) == everywhere - {provider}


@pytest.mark.parametrize(
    ("target", "prefix"),
    [
        ("claude", "claude "),
        ("grok", "grok "),
        ("continue", "cn "),
        ("copilot", "copilot -i "),
        ("agy", "agy -i "),
    ],
)
def test_every_target_gets_its_own_launch_syntax(app_config, tmp_path, target, prefix):
    seed_codex(app_config.codex_root)
    session = _sessions(app_config)["codex"]
    command = handoff.handoff_command(session, target, tmp_path / "b.md")
    assert command.startswith(f"cd /workspace/example-project && {prefix}'Read ")


def test_claude_brief_has_conversation_and_skips_tool_parts(app_config):
    path = seed_claude(app_config.claude_root)
    write_jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": "c7df09f0-b9f2-4563-853e-f64fd095128f",
                "cwd": "/workspace/crew-chief",
                "message": {"role": "user", "content": "Build the service"},
            },
            {
                "type": "assistant",
                "sessionId": "c7df09f0-b9f2-4563-853e-f64fd095128f",
                "cwd": "/workspace/crew-chief",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "secret"}},
                        {"type": "text", "text": "Service built"},
                    ],
                },
            },
        ],
    )
    session = _sessions(app_config)["claude"]
    turns = handoff.recent_turns(session)
    assert [t.role for t in turns] == ["User", "Assistant"]
    brief = handoff.build_brief(session, turns)
    assert "Build the service" in brief and "Service built" in brief
    assert "secret" not in brief


def test_codex_brief_skips_bootstrap_text(app_config):
    path = seed_codex(app_config.codex_root)
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>x</environment_context>"}
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Fixed the downloader"}],
            },
        },
    ]
    write_jsonl(path, rows)
    turns = handoff.recent_turns(_sessions(app_config)["codex"])
    assert [t.text for t in turns] == ["Fixed the downloader"]


def test_unreadable_format_falls_back_to_the_opening_message(app_config):
    session = _sessions_with_preview(app_config)
    brief = handoff.build_brief(session, handoff.recent_turns(session))
    assert "Opening message" in brief and "not readable" in brief


def _sessions_with_preview(app_config):
    seed_codex(app_config.codex_root)
    session = _sessions(app_config)["codex"]
    from dataclasses import replace

    return replace(session, provider="grok", preview="hello there")


def test_brief_is_private_and_truncated(app_config, tmp_path):
    seed_codex(app_config.codex_root)
    session = _sessions(app_config)["codex"]
    big = [handoff.Turn("User", "x" * 5000)] * 40
    brief = handoff.build_brief(session, big)
    assert len(brief) <= handoff.MAX_BRIEF_CHARS + 40
    path = handoff.write_brief(tmp_path / "h", session, brief)
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "h").stat().st_mode & 0o777 == 0o700


def test_command_rejects_same_provider_and_unknown_target(app_config, tmp_path):
    seed_codex(app_config.codex_root)
    session = _sessions(app_config)["codex"]
    with pytest.raises(handoff.HandoffError):
        handoff.handoff_command(session, "codex", tmp_path / "b.md")
    with pytest.raises(handoff.HandoffError):
        handoff.handoff_command(session, "nonsense", tmp_path / "b.md")
    command = handoff.handoff_command(session, "claude", tmp_path / "b.md")
    assert command.startswith("cd /workspace/example-project && claude ")
    assert str(tmp_path / "b.md") in command


def test_service_prepares_a_handoff_and_reports_bad_targets(app_config):
    seed_codex(app_config.codex_root)
    service = SessionActionService(app_config)
    session = _sessions(app_config)["codex"]
    _s, path, command = service.prepare_handoff(session.public_id, "claude")
    assert path.exists() and "claude" in command
    with pytest.raises(SessionActionError):
        service.prepare_handoff(session.public_id, "codex")


def test_web_route_launches_the_target_window(app_config):
    seed_codex(app_config.codex_root)
    config = app_config.__class__(**{**app_config.__dict__, "webterm_url": "https://term.example/"})
    app = create_app(config)
    session = _sessions(config)["codex"]
    with app.test_client() as client:
        client.get("/")
        with client.session_transaction() as flask_session:
            token = flask_session.get("csrf_token")
        with mock.patch.object(
            SessionActionService,
            "handoff_in_webterm",
            return_value=SimpleNamespace(window_index=3),
        ) as launch:
            response = client.post(
                f"/sessions/{session.public_id}/handoff",
                data={"target": "claude", "csrf_token": token},
                headers={"Origin": "http://localhost"},
            )
    assert response.status_code == 302
    launch.assert_called_once_with(session.public_id, "claude")


def test_cli_handoff_prints_the_brief_and_command(app_config, monkeypatch, capsys):
    from session_control import cli

    seed_codex(app_config.codex_root)
    session = _sessions(app_config)["codex"]
    monkeypatch.setattr(cli.AppConfig, "from_env", classmethod(lambda cls: app_config))
    assert cli.main(["handoff", session.public_id, "--to", "claude"]) == 0
    out = capsys.readouterr().out
    assert "Brief:" in out and "Run: cd /workspace/example-project && claude " in out
    assert cli.main(["handoff", session.public_id, "--to", "codex"]) == 1
    assert "already a codex session" in capsys.readouterr().err


def test_list_page_offers_a_handoff_on_every_row(app_config):
    seed_codex(app_config.codex_root)
    seed_claude(app_config.claude_root)
    sessions = _sessions(app_config)
    page = create_app(app_config).test_client().get("/").get_data(as_text=True)
    for provider, session in sessions.items():
        assert f'action="/sessions/{session.public_id}/handoff"' in page
        row = page.split(f'action="/sessions/{session.public_id}/handoff"')[1].split("</form>")[0]
        assert f'<option value="{provider}">' not in row
        assert '<option value="grok">' in row


def _grok_event(kind: str, **update):
    return {
        "method": "session/update",
        "params": {"sessionId": "s", "update": {"sessionUpdate": kind, **update}},
    }


def _grok_text(kind: str, text: str):
    return _grok_event(kind, content={"type": "text", "text": text})


def test_grok_brief_merges_streamed_chunks_and_skips_tool_noise(app_config):
    from tests.helpers import seed_grok

    session_dir = seed_grok(app_config.grok_root)
    write_jsonl(
        session_dir / "updates.jsonl",
        [
            _grok_text("user_message_chunk", "Repair the "),
            _grok_text("user_message_chunk", "Grok integration"),
            _grok_text("agent_thought_chunk", "private reasoning that must not leak"),
            _grok_text("agent_message_chunk", "Reading the code."),
            _grok_event("tool_call", toolCallId="t1", title="Read", rawInput={"path": "x"}),
            _grok_event("tool_call_update", toolCallId="t1", title="Read"),
            _grok_text("agent_message_chunk", "Fixed the bug."),
            _grok_event("turn_completed", stop_reason="end_turn"),
            _grok_text("user_message_chunk", "Thanks, now add tests"),
        ],
    )
    session = _sessions(app_config)["grok"]
    turns = handoff.recent_turns(session)
    assert [(t.role, t.text) for t in turns] == [
        ("User", "Repair the Grok integration"),
        ("Assistant", "Reading the code.\n\nFixed the bug."),
        ("User", "Thanks, now add tests"),
    ]
    brief = handoff.build_brief(session, turns)
    assert "Recent conversation" in brief and "private reasoning" not in brief


def test_grok_turns_tolerate_missing_or_garbled_transcripts(app_config, tmp_path):
    assert handoff._grok_turns(tmp_path / "absent.jsonl") == []
    broken = tmp_path / "updates.jsonl"
    broken.write_text(
        'not json message_chunk\n{"params": 1, "x": "message_chunk"}\n'
        '{"params": {"update": {"sessionUpdate": "user_message_chunk", "content": 7}}}\n',
        encoding="utf-8",
    )
    assert handoff._grok_turns(broken) == []
