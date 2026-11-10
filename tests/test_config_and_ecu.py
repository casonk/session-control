"""Reading configuration from the environment, and the ``ecu`` status probe.

Configuration decides where session files are read from and moved to, so a bad value
must fall back to a safe default rather than raise or point somewhere surprising. The
home directory and every lookup of the real ``ecu`` binary are redirected, so a
machine that has ``ecu`` installed behaves like one that does not.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from session_control import config as config_module
from session_control import ecu_status
from session_control.config import AppConfig


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("SESSION_CONTROL_") or name == "XDG_STATE_HOME":
            monkeypatch.delenv(name)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- defaults


def test_defaults_live_under_the_home_directory(clean_env):
    config = AppConfig.from_env()
    assert config.codex_root == clean_env / ".codex"
    assert config.claude_root == clean_env / ".claude"
    assert config.continue_root == clean_env / ".continue"
    assert config.copilot_root == clean_env / ".copilot"
    assert config.grok_root == clean_env / ".grok"
    assert config.agy_root == clean_env / ".gemini/antigravity-cli"
    assert config.trash_dir == clean_env / ".local/share/session-control/trash"
    assert config.digest_dir == clean_env / ".local/share/session-control/digests"
    assert config.crew_chief_registry == clean_env / ".local/state/crew-chief/code-sessions.jsonl"


def test_other_defaults_are_conservative(clean_env):
    config = AppConfig.from_env()
    assert (config.trash_retention_days, config.digest_retention_days) == (7, 365)
    assert config.claude_status_poll_enabled is False
    assert config.public_origin is None and config.webterm_url is None
    assert config.allowed_origins == () and config.monitor_targets == ()
    assert config.federation_tokens == {}
    assert config.tmux_session == "main"
    assert config.claude_status_command == ("claude", "auth", "status", "--json")
    assert config.codex_permission_preset == "default"


def test_each_provider_root_can_be_moved_and_a_tilde_is_expanded(clean_env, monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_CODEX_ROOT", "~/elsewhere/codex")
    monkeypatch.setenv("SESSION_CONTROL_TRASH_DIR", str(clean_env / "my-trash"))
    config = AppConfig.from_env()
    assert config.codex_root == Path("~/elsewhere/codex").expanduser()
    assert config.trash_dir == clean_env / "my-trash"


def test_a_blank_path_variable_means_the_default(clean_env, monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_CLAUDE_ROOT", "")
    assert AppConfig.from_env().claude_root == clean_env / ".claude"


def test_the_secret_key_is_random_unless_configured(clean_env, monkeypatch):
    first, second = AppConfig.from_env().secret_key, AppConfig.from_env().secret_key
    assert first != second and len(first) == 64
    monkeypatch.setenv("SESSION_CONTROL_SECRET_KEY", "fixed")
    assert AppConfig.from_env().secret_key == "fixed"


def test_the_crew_chief_registry_follows_xdg_state_home(clean_env, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(clean_env / "state"))
    assert (
        AppConfig.from_env().crew_chief_registry
        == clean_env / "state/crew-chief/code-sessions.jsonl"
    )
    monkeypatch.setenv("SESSION_CONTROL_CREW_CHIEF_REGISTRY", str(clean_env / "reg.jsonl"))
    assert AppConfig.from_env().crew_chief_registry == clean_env / "reg.jsonl"


def test_provider_root_picks_the_matching_directory(clean_env):
    config = AppConfig.from_env()
    for provider in ("codex", "claude", "continue", "copilot", "grok", "agy"):
        assert config.provider_root(provider) == getattr(config, f"{provider}_root")
    with pytest.raises(KeyError):
        config.provider_root("unknown")


# --------------------------------------------------------------------------- parsing helpers


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("14", 14),
        (" 3 ", 3),
        ("0", 1),
        ("-5", 1),
        ("abc", 7),
        ("1.5", 7),
        ("", 7),
        ("   ", 7),
        (None, 7),
    ],
)
def test_integer_settings_clamp_to_at_least_one_and_fall_back_when_unparseable(
    monkeypatch, value, expected
):
    if value is None:
        monkeypatch.delenv("CC_INT", raising=False)
    else:
        monkeypatch.setenv("CC_INT", value)
    assert config_module._int_env("CC_INT", 7) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2.5", 2.5), ("0", 0.0), ("-1", 0.0), ("nope", 5.0), ("", 5.0), (None, 5.0), ("1e1", 10.0)],
)
def test_float_settings_clamp_to_zero_and_fall_back_when_unparseable(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CC_FLOAT", raising=False)
    else:
        monkeypatch.setenv("CC_FLOAT", value)
    assert config_module._float_env("CC_FLOAT", 5.0) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("a,b", ("a", "b")), (" a , ,b ,", ("a", "b")), ("", ()), (",,", ()), ("one", ("one",))],
)
def test_csv_values_are_trimmed_and_blanks_dropped(value, expected):
    assert config_module._split_csv(value) == expected


def test_federation_tokens_are_keyed_by_lowercase_node_id_and_malformed_pairs_are_dropped():
    parsed = config_module._token_map("Alpha=tok1, beta = tok2 ,gamma,=empty,delta=, eps=a=b")
    assert parsed == {"alpha": "tok1", "beta": "tok2", "eps": "a=b"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("TRUE", True),
        (" yes ", True),
        ("on", True),
        ("0", False),
        ("off", False),
        ("", False),
        (None, False),
    ],
)
def test_truthy_settings(value, expected):
    assert config_module._truthy(value) is expected


def test_a_command_setting_is_split_like_a_shell_and_blank_means_the_default(monkeypatch):
    default = ("claude", "auth", "status")
    monkeypatch.setenv("CC_CMD", "my-tool --flag 'two words'")
    assert config_module._command_env("CC_CMD", default) == ("my-tool", "--flag", "two words")
    monkeypatch.setenv("CC_CMD", "   ")
    assert config_module._command_env("CC_CMD", default) == default
    monkeypatch.setenv("CC_CMD", "")
    assert config_module._command_env("CC_CMD", default) == default
    monkeypatch.delenv("CC_CMD")
    assert config_module._command_env("CC_CMD", default) == default


def test_optional_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("CC_OPT", raising=False)
    assert config_module._optional_path_env("CC_OPT") is None
    monkeypatch.setenv("CC_OPT", "")
    assert config_module._optional_path_env("CC_OPT") is None
    monkeypatch.setenv("CC_OPT", str(tmp_path / "x"))
    assert config_module._optional_path_env("CC_OPT") == tmp_path / "x"


def test_a_full_environment_is_read_into_the_matching_fields(clean_env, monkeypatch):
    for name, value in {
        "TRASH_RETENTION_DAYS": "30",
        "PUBLIC_ORIGIN": "https://sessions.example",
        "ALLOWED_ORIGINS": "https://a.example, https://b.example",
        "MAX_PREVIEW_CHARS": "120",
        "WEBTERM_URL": "https://term.example",
        "TMUX_SESSION": "work",
        "CODEX_RESUME_MODEL": "gpt-x",
        "CLAUDE_STATUS_POLL": "1",
        "CLAUDE_STATUS_POLL_INTERVAL": "60",
        "CLAUDE_STATUS_TIMEOUT": "3",
        "SCAN_CACHE_TTL": "0",
        "MONITOR_TARGETS": "https://one.example,https://two.example",
        "MONITOR_INTERVAL": "2.5",
        "FEDERATION_NODE_ID": "node-a",
        "FEDERATION_NODE_LABEL": "Node A",
        "FEDERATION_TOKENS": "node-b=secret",
    }.items():
        monkeypatch.setenv(f"SESSION_CONTROL_{name}", value)
    config = AppConfig.from_env()
    assert config.trash_retention_days == 30
    assert config.public_origin == "https://sessions.example"
    assert config.allowed_origins == ("https://a.example", "https://b.example")
    assert config.max_preview_chars == 120
    assert (config.webterm_url, config.tmux_session) == ("https://term.example", "work")
    assert config.codex_resume_model == "gpt-x"
    assert config.claude_status_poll_enabled is True
    assert (config.claude_status_poll_interval_seconds, config.claude_status_timeout_seconds) == (
        60,
        3,
    )
    assert config.scan_cache_ttl_seconds == 0.0
    assert config.monitor_targets == ("https://one.example", "https://two.example")
    assert config.monitor_interval_seconds == 2.5
    assert (config.federation_node_id, config.federation_node_label) == ("node-a", "Node A")
    assert config.federation_tokens == {"node-b": "secret"}


# --------------------------------------------------------------------------- ecu


def completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


@pytest.fixture
def no_ecu(clean_env, monkeypatch):
    monkeypatch.setattr(ecu_status.shutil, "which", lambda name: None)
    return clean_env


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_configured_binary_wins_over_the_path_and_home(no_ecu, monkeypatch):
    configured = make_executable(no_ecu / "custom" / "ecu")
    make_executable(no_ecu / ".local/bin/ecu")
    monkeypatch.setenv("SESSION_CONTROL_ECU_BIN", f"  {configured}  ")
    assert ecu_status._binary() == str(configured)


def test_a_configured_binary_that_is_not_executable_is_skipped(no_ecu, monkeypatch):
    not_executable = no_ecu / "ecu-noexec"
    not_executable.write_text("x", encoding="utf-8")
    not_executable.chmod(0o644)
    fallback = make_executable(no_ecu / ".local/bin/ecu")
    monkeypatch.setenv("SESSION_CONTROL_ECU_BIN", str(not_executable))
    assert ecu_status._binary() == str(fallback)


def test_the_binary_on_the_path_is_used_before_the_home_one(no_ecu, monkeypatch):
    on_path = make_executable(no_ecu / "bin" / "ecu")
    make_executable(no_ecu / ".local/bin/ecu")
    monkeypatch.setattr(ecu_status.shutil, "which", lambda name: str(on_path))
    assert ecu_status._binary() == str(on_path)


def test_no_binary_anywhere_is_none(no_ecu):
    assert ecu_status._binary() is None


def test_a_missing_ecu_says_how_to_point_at_it(no_ecu):
    document, reason = ecu_status.read_status()
    assert document is None
    assert "SESSION_CONTROL_ECU_BIN" in reason


def test_a_good_status_is_the_parsed_document(no_ecu, monkeypatch):
    binary = make_executable(no_ecu / ".local/bin/ecu")
    payload = {"leases": [], "gpu": {"free_mib": 1000}}
    with mock.patch.object(
        ecu_status.subprocess, "run", return_value=completed(json.dumps(payload))
    ) as run:
        document, reason = ecu_status.read_status(timeout=7)
    assert (document, reason) == (payload, "")
    assert run.call_args.args[0] == [str(binary), "status", "--json"]
    assert run.call_args.kwargs["timeout"] == 7


def test_a_timeout_is_reported_with_its_limit(no_ecu):
    make_executable(no_ecu / ".local/bin/ecu")
    with mock.patch.object(
        ecu_status.subprocess, "run", side_effect=subprocess.TimeoutExpired("ecu", 20)
    ):
        document, reason = ecu_status.read_status(timeout=20)
    assert document is None and "timed out after 20s" in reason


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (completed(stdout="not json", stderr="first\nlast line\n", returncode=3), "last line"),
        (completed(stdout="", stderr="", returncode=1), "ecu status exited 1"),
        (completed(stdout="", stderr="  \n", returncode=0), "ecu status exited 0"),
        (completed(stdout='["a"]', returncode=0), '["a"]'),
        (completed(stdout="plain failure", returncode=2), "plain failure"),
        (completed(stdout='{"ok": true}', stderr="denied", returncode=4), "denied"),
    ],
)
def test_an_unusable_answer_gives_the_last_line_of_its_output_as_the_reason(no_ecu, result, reason):
    make_executable(no_ecu / ".local/bin/ecu")
    with mock.patch.object(ecu_status.subprocess, "run", return_value=result):
        document, why = ecu_status.read_status()
    assert document is None and why == reason
