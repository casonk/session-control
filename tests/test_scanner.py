from __future__ import annotations

from dataclasses import replace

from session_control.scanner import SessionScanner
from tests.helpers import seed_claude, seed_codex, seed_continue, seed_copilot


def test_scanner_discovers_supported_provider_sessions(app_config):
    seed_codex(app_config.codex_root)
    seed_claude(app_config.claude_root)
    seed_continue(app_config.continue_root)
    seed_copilot(app_config.copilot_root)

    report = SessionScanner(app_config).scan()

    assert not report.errors
    by_provider = {session.provider: session for session in report.sessions}
    assert set(by_provider) == {"codex", "claude", "continue", "copilot"}
    assert by_provider["codex"].title == "Fix statement downloader"
    assert by_provider["codex"].resume_command.endswith(
        "codex resume 019d016b-30c2-7992-970a-b6082c1a2723"
    )
    assert by_provider["claude"].resume_command.endswith(
        "claude --resume c7df09f0-b9f2-4563-853e-f64fd095128f"
    )
    assert by_provider["continue"].resume_command.endswith(
        "cn --fork 9f4b464d-495f-432d-8d16-31aa4e7ac7ea"
    )
    assert by_provider["copilot"].resume_command.endswith(
        "copilot --resume=3e6d83f8-5629-47d2-9a64-2779c509c808"
    )


def test_scanner_can_limit_provider(app_config):
    seed_codex(app_config.codex_root)
    seed_claude(app_config.claude_root)

    report = SessionScanner(app_config).scan(providers=("claude",))

    assert [session.provider for session in report.sessions] == ["claude"]


def test_codex_resume_command_uses_recorded_model(app_config):
    seed_codex(app_config.codex_root, model="gpt-5.4")

    session = SessionScanner(app_config).scan(providers=("codex",)).sessions[0]

    assert session.resume_command.endswith(
        "codex resume --model gpt-5.4 019d016b-30c2-7992-970a-b6082c1a2723"
    )
    assert session.metadata["model"] == "gpt-5.4"
    assert session.metadata["resume_model"] == "gpt-5.4"


def test_codex_resume_model_override_wins(app_config):
    config = replace(app_config, codex_resume_model="gpt-5.3-codex")
    seed_codex(config.codex_root, model="gpt-5.4")

    session = SessionScanner(config).scan(providers=("codex",)).sessions[0]

    assert session.resume_command.endswith(
        "codex resume --model gpt-5.3-codex 019d016b-30c2-7992-970a-b6082c1a2723"
    )
    assert session.metadata["model"] == "gpt-5.4"
    assert session.metadata["resume_model"] == "gpt-5.3-codex"
