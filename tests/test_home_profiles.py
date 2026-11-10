from __future__ import annotations

import base64
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from session_control.actions import _launch_command
from session_control.cli import main as session_control_main
from session_control.home_profiles import (
    SPECS,
    HomeProfile,
    HomeProfileRegistry,
    HomeRoute,
    default_home,
    discover_account_label,
    execute_with_profile,
    get_usage_summary,
    harvest_usage,
    load_profiles,
    owning_account_home,
    save_profiles,
)
from session_control.provider_profile_cli import codex_main
from session_control.scanner import SessionScanner
from tests.helpers import (
    seed_claude,
    seed_codex,
    seed_continue,
    seed_copilot,
    write_json,
    write_jsonl,
)


def _jwt(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


@pytest.fixture(autouse=True)
def isolated_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Keep profile tests off the real account directories."""
    paths: dict[str, Path] = {}
    for provider, filename, payload in (
        (
            "codex",
            "auth.json",
            {
                "tokens": {
                    "refresh_token": "FAKE-TOKEN",
                    "id_token": _jwt("decoy-codex@example.com"),
                }
            },
        ),
        ("claude", ".credentials.json", {"claudeAiOauth": {"accessToken": "FAKE-TOKEN"}}),
        (
            "continue",
            "auth.json",
            {"accessToken": "FAKE-TOKEN", "userEmail": "decoy-continue@example.com"},
        ),
        ("copilot", "config.json", {"lastLoggedInUser": {"login": "decoyuser"}}),
    ):
        decoy = tmp_path / f"decoy-{provider}"
        write_json(decoy / filename, payload)
        registry = tmp_path / provider / "profiles.json"
        database = tmp_path / provider / "profile_usage.sqlite"
        token = provider.upper()
        monkeypatch.setenv(f"SESSION_CONTROL_{token}_HOME", str(decoy))
        monkeypatch.setenv(f"SESSION_CONTROL_{token}_PROFILES_PATH", str(registry))
        monkeypatch.setenv(f"SESSION_CONTROL_{token}_USAGE_DB", str(database))
        monkeypatch.setenv(SPECS[provider].env_var, str(tmp_path / f"cli-home-{provider}"))
        paths[f"{provider}_cfg"] = registry
        paths[f"{provider}_db"] = database
        paths[f"{provider}_decoy"] = decoy
        paths[f"{provider}_registry_dir"] = registry.parent
    return paths


def test_cli_home_variable_does_not_move_the_registry(isolated_homes: dict[str, Path]):
    spec = SPECS["codex"]
    assert default_home(spec) == isolated_homes["codex_decoy"].resolve()
    assert os.environ["CODEX_HOME"] != str(default_home(spec))


def test_corrupt_registry_does_not_read_the_other_home(isolated_homes: dict[str, Path]):
    path = isolated_homes["codex_cfg"]
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    registry = load_profiles(SPECS["codex"], path, create=False)

    assert registry.config_path == str(path)
    assert registry.profiles["default"].email == ""
    assert not path.with_suffix(".tmp").exists()


def test_account_labels_come_from_auth_files_not_tokens(tmp_path: Path):
    codex_home = tmp_path / "codex"
    write_json(
        codex_home / "auth.json",
        {"tokens": {"refresh_token": "FAKE-TOKEN", "id_token": _jwt("codex@example.com")}},
    )
    continue_home = tmp_path / "continue"
    write_json(
        continue_home / "auth.json",
        {"accessToken": "FAKE-TOKEN", "userEmail": "continue@example.com"},
    )
    copilot_home = tmp_path / "copilot"
    (copilot_home).mkdir()
    (copilot_home / "config.json").write_text(
        '// managed\n{"lastLoggedInUser": {"host": "github.com", "login": "octocat"}}\n',
        encoding="utf-8",
    )
    claude_home = tmp_path / "claude"
    write_json(claude_home / ".credentials.json", {"claudeAiOauth": {"accessToken": "FAKE-TOKEN"}})

    assert discover_account_label(SPECS["codex"], codex_home) == "codex@example.com"
    assert discover_account_label(SPECS["continue"], continue_home) == "continue@example.com"
    assert discover_account_label(SPECS["copilot"], copilot_home) == "octocat"
    assert discover_account_label(SPECS["claude"], claude_home) == ""
    assert "FAKE-TOKEN" not in discover_account_label(SPECS["codex"], codex_home)


def test_added_account_home_stays_next_to_the_registry(isolated_homes: dict[str, Path]):
    spec = SPECS["claude"]
    registry = load_profiles(spec, isolated_homes["claude_cfg"])
    registry.profiles["work"] = HomeProfile(name="work", email="work@example.com")
    registry.routes.append(HomeRoute(pattern="*work-repos*", profile="work"))
    save_profiles(registry, isolated_homes["claude_cfg"])

    reloaded = load_profiles(spec, isolated_homes["claude_cfg"], create=False)
    config = isolated_homes["claude_cfg"]
    assert reloaded.profiles["work"].home(config) == config.parent / "profiles" / "work"
    assert reloaded.profiles["work"].home(config) != isolated_homes["claude_decoy"]
    assert reloaded.profiles["default"].home(config) == config.parent


def test_codex_profile_commands_round_trip(
    isolated_homes: dict[str, Path], capsys: pytest.CaptureFixture[str]
):
    assert codex_main(["add", "team_a", "--email", "team@example.com"]) == 0
    added = capsys.readouterr().out
    assert "Sign in with: codex-profile run --profile team_a login" in added
    home = isolated_homes["codex_registry_dir"] / "profiles" / "team_a"
    assert home.is_dir()
    assert not (home / "auth.json").exists()

    assert codex_main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "team@example.com" in listed
    assert "decoy-codex@example.com" not in listed

    assert session_control_main(["codex-profile", "switch", "team_a"]) == 0
    assert "Switched active profile to 'team_a'." in capsys.readouterr().out
    assert codex_main(["remove", "default"]) == 1
    assert "Cannot remove" in capsys.readouterr().err
    assert codex_main(["remove", "team_a"]) == 0
    assert home.is_dir()


def test_passthrough_selects_profile_without_launching(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def fake_execute(spec, args, requested_profile=None, auto_failover=True, **_kwargs) -> int:
        seen["args"] = args
        seen["profile"] = requested_profile
        seen["failover"] = auto_failover
        seen["spec"] = spec
        return 0

    monkeypatch.setattr("session_control.provider_profile_cli.execute_with_profile", fake_execute)
    assert codex_main(["--profile", "work", "--no-failover", "resume", "abc"]) == 0
    assert seen["args"] == ["resume", "abc"]
    assert seen["profile"] == "work"
    assert seen["failover"] is False
    assert getattr(seen["spec"], "provider", "") == "codex"


def test_execute_sets_home_and_strips_token_env(
    isolated_homes: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = SPECS["codex"]
    registry = HomeProfileRegistry(
        spec_name="codex",
        profiles={"default": HomeProfile(name="default"), "work": HomeProfile(name="work")},
        routes=[HomeRoute(pattern="*work-repos*", profile="work")],
    )
    save_profiles(registry, isolated_homes["codex_cfg"])
    monkeypatch.setenv("OPENAI_API_KEY", "FAKE-KEY")
    marker = tmp_path / "homes.txt"
    script = (
        "import os, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "with path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(os.environ['CODEX_HOME'] + ' ' + str('OPENAI_API_KEY' in os.environ) + '\\n')\n"
        "raise SystemExit(int(sys.argv[2]))\n"
    )
    work_home = isolated_homes["codex_registry_dir"] / "profiles" / "work"
    cwd = tmp_path / "work-repos" / "client"
    cwd.mkdir(parents=True)

    code = execute_with_profile(
        spec,
        ["-c", script, str(marker), "0"],
        cwd=cwd,
        auto_failover=False,
        config_path=isolated_homes["codex_cfg"],
        executable=sys.executable,
    )

    assert code == 0
    assert marker.read_text(encoding="utf-8").splitlines() == [f"{work_home} False"]
    assert stat.S_IMODE(work_home.stat().st_mode) == 0o700


def test_harvest_reads_each_provider_without_following_links(
    isolated_homes: dict[str, Path], tmp_path: Path
):
    codex_home = tmp_path / "codex-account"
    session = "019d016b-30c2-7992-970a-b6082c1a2723"
    path = codex_home / "sessions" / "2026" / "04" / "01" / f"rollout-{session}.jsonl"
    write_jsonl(
        path,
        [
            {
                "timestamp": "2026-04-01T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": session},
            },
            {
                "timestamp": "2026-04-01T12:01:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "total_tokens": 10,
                            "input_tokens": 4,
                            "output_tokens": 1,
                            "cached_input_tokens": 5,
                        }
                    },
                },
            },
            {
                "timestamp": "2026-04-01T12:02:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "total_tokens": 25,
                            "input_tokens": 8,
                            "output_tokens": 2,
                            "cached_input_tokens": 15,
                        }
                    },
                },
            },
        ],
    )
    outside = tmp_path / "outside"
    write_jsonl(
        outside / "leak.jsonl",
        [
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 999}},
                },
            }
        ],
    )
    link_parent = codex_home / "sessions" / "linked"
    link_parent.mkdir(parents=True)
    (link_parent / "leak.jsonl").symlink_to(outside / "leak.jsonl")

    claude_home = tmp_path / "claude-account"
    claude_id = "c7df09f0-b9f2-4563-853e-f64fd095128f"
    write_jsonl(
        claude_home / "projects" / "-workspace" / f"{claude_id}.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": "2026-04-02T10:03:00Z",
                "sessionId": claude_id,
                "message": {
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 34,
                        "cache_read_input_tokens": 200,
                        "cache_creation_input_tokens": 100,
                    }
                },
            }
        ],
    )
    continue_home = tmp_path / "continue-account"
    continue_id = "9f4b464d-495f-432d-8d16-31aa4e7ac7ea"
    write_json(
        continue_home / "sessions" / f"{continue_id}.json",
        {
            "sessionId": continue_id,
            "history": [
                {
                    "message": {
                        "role": "assistant",
                        "usage": {"promptTokens": 6, "completionTokens": 3, "cacheReadTokens": 1},
                    }
                }
            ],
        },
    )
    copilot_home = tmp_path / "copilot-account"
    copilot_id = "3e6d83f8-5629-47d2-9a64-2779c509c808"
    write_jsonl(
        copilot_home / "session-state" / copilot_id / "events.jsonl",
        [
            {
                "type": "session.usage_record",
                "timestamp": "2026-03-21T14:47:00Z",
                "data": {
                    "inputTokens": 5,
                    "outputTokens": 1,
                    "cacheReadTokens": 2,
                    "cacheWriteTokens": 0,
                },
            },
            {
                "type": "session.usage_record",
                "timestamp": "2026-03-21T14:48:00Z",
                "data": {
                    "inputTokens": 7,
                    "outputTokens": 2,
                    "cacheReadTokens": 1,
                    "cacheWriteTokens": 1,
                },
            },
        ],
    )

    homes = {
        "codex": codex_home,
        "claude": claude_home,
        "continue": continue_home,
        "copilot": copilot_home,
    }
    expected = {
        "codex": (8, 2, 15, 25),
        "claude": (12, 34, 300, 346),
        "continue": (6, 3, 1, 10),
        "copilot": (12, 3, 4, 19),
    }
    for provider, home in homes.items():
        registry = HomeProfileRegistry(
            spec_name=provider,
            profiles={provider: HomeProfile(name=provider, home_dir=str(home))},
            config_path=str(isolated_homes[f"{provider}_cfg"]),
        )
        result = harvest_usage(registry, db_path=isolated_homes[f"{provider}_db"])
        assert result["sessions_seen"] == 1
        summary = get_usage_summary(SPECS[provider], db_path=isolated_homes[f"{provider}_db"])
        row = summary["profiles"][0]
        assert (
            row["total_input"],
            row["total_output"],
            row["total_cache"],
            row["total_tokens"],
        ) == expected[provider]


def test_scanner_resumes_added_accounts_through_their_launchers(app_config, tmp_path: Path):
    ids = {
        "codex": "019d016b-30c2-7992-970a-b6082c1a2723",
        "claude": "c7df09f0-b9f2-4563-853e-f64fd095128f",
        "continue": "9f4b464d-495f-432d-8d16-31aa4e7ac7ea",
        "copilot": "3e6d83f8-5629-47d2-9a64-2779c509c808",
    }
    work_ids = {
        "codex": "11111111-1111-4111-8111-111111111111",
        "claude": "22222222-2222-4222-8222-222222222222",
        "continue": "33333333-3333-4333-8333-333333333333",
        "copilot": "44444444-4444-4444-8444-444444444444",
    }
    roots = {
        "codex": app_config.codex_root,
        "claude": app_config.claude_root,
        "continue": app_config.continue_root,
        "copilot": app_config.copilot_root,
    }
    seeders = {
        "codex": seed_codex,
        "claude": seed_claude,
        "continue": seed_continue,
        "copilot": seed_copilot,
    }
    for provider, root in roots.items():
        write_json(
            root / "profiles.json",
            {
                "profiles": {
                    "default": {"name": "default", "home": "", "enabled": True},
                    "work": {"name": "work", "home": "", "enabled": True},
                }
            },
        )
        seeders[provider](root, session_id=ids[provider])
        seeders[provider](root / "profiles" / "work", session_id=work_ids[provider])

    report = SessionScanner(app_config).scan(("codex", "claude", "continue", "copilot"))
    assert not report.errors
    by_id = {session.session_id: session for session in report.sessions}
    assert "codex-profile run --profile work resume" in by_id[work_ids["codex"]].resume_command
    assert by_id[ids["codex"]].resume_command.endswith(f"codex resume {ids['codex']}")
    assert "claude-profile run --profile work --resume" in by_id[work_ids["claude"]].resume_command
    assert (
        "continue-profile run --profile work --fork" in by_id[work_ids["continue"]].resume_command
    )
    assert (
        f"copilot-profile run --profile work --resume={work_ids['copilot']}"
        in by_id[work_ids["copilot"]].resume_command
    )
    launched = _launch_command(by_id[work_ids["codex"]], codex_permission_preset="default")
    assert "codex-profile run --profile work resume" in launched
    plain = _launch_command(by_id[ids["codex"]], codex_permission_preset="default")
    assert "codex-profile" not in plain
    assert plain.endswith(f"codex resume {ids['codex']}")


def test_owning_account_home_picks_the_profile_directory(tmp_path: Path):
    root = tmp_path / "codex"
    profile_session = root / "profiles" / "work" / "sessions" / "day" / "session.jsonl"
    default_session = root / "sessions" / "day" / "session.jsonl"
    profile_session.parent.mkdir(parents=True)
    default_session.parent.mkdir(parents=True)
    profile_session.write_text("", encoding="utf-8")
    default_session.write_text("", encoding="utf-8")

    assert owning_account_home(root, profile_session) == (root / "profiles" / "work").resolve()
    assert owning_account_home(root, default_session) == root.resolve()
