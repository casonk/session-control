from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from session_control.cli import main as session_control_main
from session_control.grok_profile_cli import main as grok_profile_main
from session_control.grok_profiles import (
    GrokProfile,
    GrokProfileRegistry,
    GrokRoute,
    discover_email,
    execute_with_profile,
    get_usage_summary,
    harvest_usage,
    load_profiles,
    resolve_profile,
    save_profiles,
)
from session_control.scanner import SessionScanner
from tests.helpers import seed_grok, write_json


@pytest.fixture(autouse=True)
def isolated_grok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Keep profile tests off the real ~/.grok directory and its auth file."""
    registry_dir = tmp_path / "registry"
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    (decoy / "auth.json").write_text(
        json.dumps({"refresh_token": "FAKE-TOKEN", "email": "decoy@example.com"}) + "\n",
        encoding="utf-8",
    )
    paths = {
        "cfg": registry_dir / "profiles.json",
        "db": tmp_path / "profile_usage.sqlite",
        "decoy": decoy,
        "registry_dir": registry_dir,
    }
    monkeypatch.setenv("SESSION_CONTROL_GROK_PROFILES_PATH", str(paths["cfg"]))
    monkeypatch.setenv("SESSION_CONTROL_GROK_USAGE_DB", str(paths["db"]))
    monkeypatch.setenv("SESSION_CONTROL_GROK_HOME", str(decoy))
    return paths


def _usage_payload(session_id: str, input_tokens: int, output_tokens: int = 4) -> dict:
    return {
        "sessionId": session_id,
        "updatedAt": "2026-11-08T12:00:00Z",
        "session": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cachedReadTokens": 2,
            "reasoningTokens": 1,
            "totalTokens": input_tokens + output_tokens + 2,
            "costUsdTicks": 0,
        },
    }


def test_added_account_home_stays_next_to_the_registry(isolated_grok: dict[str, Path]):
    registry = load_profiles(isolated_grok["cfg"])
    registry.profiles["work"] = GrokProfile(name="work", email="work@example.com")
    registry.routes.append(GrokRoute(pattern="*work-repos*", profile="work"))
    save_profiles(registry, isolated_grok["cfg"])

    reloaded = load_profiles(isolated_grok["cfg"], create=False)
    work = reloaded.profiles["work"]
    assert work.email == "work@example.com"
    assert work.home(isolated_grok["cfg"]) == (isolated_grok["registry_dir"] / "profiles" / "work")
    assert work.home(isolated_grok["cfg"]) != isolated_grok["decoy"]
    assert reloaded.profiles["default"].home(isolated_grok["cfg"]) == isolated_grok["registry_dir"]
    assert any(route.pattern == "*work-repos*" for route in reloaded.routes)


def test_corrupt_registry_does_not_read_another_home(isolated_grok: dict[str, Path]):
    isolated_grok["cfg"].parent.mkdir(parents=True)
    isolated_grok["cfg"].write_text("{", encoding="utf-8")

    registry = load_profiles(isolated_grok["cfg"], create=False)

    assert registry.config_path == str(isolated_grok["cfg"])
    assert registry.profiles["default"].email == ""
    assert not isolated_grok["cfg"].with_suffix(".tmp").exists()


def test_discover_email_reads_only_the_email_field(tmp_path: Path):
    home = tmp_path / "account"
    write_json(
        home / "auth.json",
        {
            "https://auth.x.ai::00000000-0000-0000-0000-000000000000": {
                "email": "work@example.com",
                "refresh_token": "FAKE-TOKEN",
            }
        },
    )

    assert discover_email(home) == "work@example.com"


def test_resolve_profile_honors_routes_and_failover(tmp_path: Path):
    registry = GrokProfileRegistry(
        profiles={
            "default": GrokProfile(name="default", email="personal@example.com"),
            "academic": GrokProfile(name="academic", email="student@example.edu"),
            "client_a": GrokProfile(name="client_a", email="client@example.com"),
        },
        routes=[
            GrokRoute(pattern="*university-coursework*", profile="academic"),
            GrokRoute(pattern="*/clients/corp/*", profile="client_a"),
        ],
    )
    coursework = tmp_path / "university-coursework" / "cs101"

    assert resolve_profile(registry, cwd=coursework).name == "academic"
    assert (
        resolve_profile(registry, cwd=coursework, requested_profile="client_a").name == "client_a"
    )
    assert resolve_profile(registry, cwd=tmp_path / "personal-repo").name == "default"
    assert (
        resolve_profile(registry, cwd=coursework, failover_exclude={"academic"}).name == "default"
    )


def test_harvest_replaces_session_snapshot(isolated_grok: dict[str, Path], tmp_path: Path):
    home = tmp_path / "account"
    session_id = "sess-100"
    usage_path = home / "sessions" / "encoded" / session_id / "usage.json"
    write_json(usage_path, _usage_payload(session_id, 10))
    outside = tmp_path / "outside"
    write_json(outside / "usage.json", _usage_payload("escaped", 999))
    link = home / "sessions" / "encoded" / "escaped"
    link.symlink_to(outside, target_is_directory=True)
    registry = GrokProfileRegistry(
        profiles={"work": GrokProfile(name="work", grok_home=str(home))},
        config_path=str(isolated_grok["cfg"]),
    )

    first = harvest_usage(registry, db_path=isolated_grok["db"])
    assert first == {"sessions_seen": 1, "sessions_updated": 1}
    summary = get_usage_summary(db_path=isolated_grok["db"])
    assert summary["profiles"][0]["profile"] == "work"
    assert summary["profiles"][0]["total_input"] == 10
    assert summary["profiles"][0]["total_output"] == 4
    assert summary["profiles"][0]["total_tokens"] == 16

    write_json(
        usage_path,
        {
            "sessionId": session_id,
            "updatedAt": "2026-11-08T13:00:00Z",
            "session": {"inputTokens": True, "outputTokens": 5, "totalTokens": 5},
        },
    )
    second = harvest_usage(registry, db_path=isolated_grok["db"])
    assert second["sessions_seen"] == 1
    replaced = get_usage_summary(db_path=isolated_grok["db"])
    assert replaced["profiles"][0]["session_count"] == 1
    assert replaced["profiles"][0]["total_input"] == 0
    assert replaced["profiles"][0]["total_output"] == 5
    assert replaced["recent_sessions"][0]["conversation_id"] == session_id


def test_profile_cli_commands(isolated_grok: dict[str, Path], capsys: pytest.CaptureFixture[str]):
    added = grok_profile_main(["add", "team_a", "--email", "team@example.com"])
    added_out = capsys.readouterr().out
    assert added == 0
    assert "Profile 'team_a' added." in added_out
    assert "Sign in with: grok-profile run --profile team_a login" in added_out
    home = isolated_grok["registry_dir"] / "profiles" / "team_a"
    assert home.is_dir()
    assert not (home / "auth.json").exists()

    assert grok_profile_main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "team_a" in listed
    assert "team@example.com" in listed
    assert "decoy@example.com" not in listed

    assert grok_profile_main(["switch", "team_a"]) == 0
    assert "Switched active profile to 'team_a'." in capsys.readouterr().out
    assert grok_profile_main(["route", "add", "*team-a*", "team_a"]) == 0
    assert grok_profile_main(["route", "list"]) == 0
    routed = capsys.readouterr().out
    assert "*team-a*" in routed
    assert "team_a" in routed

    assert grok_profile_main(["current"]) == 0
    assert "Active default: team_a" in capsys.readouterr().out
    assert grok_profile_main(["usage", "--json"]) == 0
    assert "profiles" in json.loads(capsys.readouterr().out)

    assert grok_profile_main(["remove", "default"]) == 1
    assert "Cannot remove" in capsys.readouterr().err
    assert grok_profile_main(["remove", "team_a"]) == 0
    assert home.is_dir()
    assert "team_a" not in load_profiles(isolated_grok["cfg"]).profiles


def test_session_control_dispatches_grok_profile(capsys: pytest.CaptureFixture[str]):
    assert session_control_main(["grok-profile", "list"]) == 0
    assert "Grok accounts:" in capsys.readouterr().out


def test_passthrough_selects_profile_without_launching_grok(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def fake_execute(
        args: list[str],
        requested_profile: str | None = None,
        cwd: Path | None = None,
        auto_failover: bool = True,
        config_path: Path | None = None,
        executable: str = "grok",
    ) -> int:
        seen["args"] = args
        seen["profile"] = requested_profile
        seen["failover"] = auto_failover
        seen["executable"] = executable
        return 0

    monkeypatch.setattr("session_control.grok_profile_cli.execute_with_profile", fake_execute)

    assert grok_profile_main(["--profile", "work", "--no-failover", "--resume", "abc"]) == 0
    assert seen["args"] == ["--resume", "abc"]
    assert seen["profile"] == "work"
    assert seen["failover"] is False
    assert grok_profile_main(["run", "--profile", "work", "login"]) == 0
    assert seen["args"] == ["login"]
    assert seen["profile"] == "work"


def test_execute_sets_grok_home_and_fails_over(isolated_grok: dict[str, Path], tmp_path: Path):
    registry = GrokProfileRegistry(
        profiles={
            "default": GrokProfile(name="default"),
            "work": GrokProfile(name="work"),
        },
        routes=[GrokRoute(pattern="*work-repos*", profile="work")],
    )
    save_profiles(registry, isolated_grok["cfg"])
    marker = tmp_path / "homes.txt"
    script = (
        "import os, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "with path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(os.environ['GROK_HOME'] + '\\n')\n"
        "raise SystemExit(int(sys.argv[2]))\n"
    )
    work_home = isolated_grok["registry_dir"] / "profiles" / "work"
    default_home = isolated_grok["registry_dir"]
    cwd = tmp_path / "work-repos" / "client"
    cwd.mkdir(parents=True)

    code = execute_with_profile(
        ["-c", script, str(marker), "0"],
        cwd=cwd,
        auto_failover=False,
        config_path=isolated_grok["cfg"],
        executable=sys.executable,
    )

    assert code == 0
    assert marker.read_text(encoding="utf-8").splitlines() == [str(work_home)]
    assert stat.S_IMODE(work_home.stat().st_mode) == 0o700
    assert work_home != isolated_grok["decoy"]

    marker.write_text("", encoding="utf-8")
    failed = execute_with_profile(
        ["-c", script, str(marker), "1"],
        cwd=cwd,
        auto_failover=True,
        config_path=isolated_grok["cfg"],
        executable=sys.executable,
    )
    assert failed == 1
    assert marker.read_text(encoding="utf-8").splitlines() == [str(work_home), str(default_home)]


def test_scanner_discovers_sessions_in_multiple_grok_accounts(app_config, tmp_path: Path):
    default_id = "default-session-111"
    work_id = "work-session-222"
    seed_grok(app_config.grok_root, session_id=default_id)
    work_home = app_config.grok_root / "profiles" / "work"
    seed_grok(work_home, session_id=work_id)
    (work_home / "active_sessions.json").write_text("{broken", encoding="utf-8")
    disabled_home = tmp_path / "disabled-home"
    seed_grok(disabled_home, session_id="disabled-session-333")
    registry = GrokProfileRegistry(
        profiles={
            "default": GrokProfile(name="default"),
            "work": GrokProfile(name="work", email="work@example.com"),
            "off": GrokProfile(name="off", grok_home=str(disabled_home), enabled=False),
        }
    )
    save_profiles(registry, app_config.grok_root / "profiles.json")

    report = SessionScanner(app_config).scan(providers=("grok",))
    by_id = {session.session_id: session for session in report.sessions}

    assert default_id in by_id
    assert work_id in by_id
    assert "disabled-session-333" not in by_id
    assert by_id[default_id].metadata.get("profile") == "default"
    assert by_id[default_id].active is False
    assert by_id[default_id].resume_command.endswith(f"grok --resume {default_id}")
    assert by_id[work_id].metadata.get("profile") == "work"
    assert by_id[work_id].active is True
    assert f"grok-profile run --profile work --resume {work_id}" in by_id[work_id].resume_command
    assert report.errors


def test_scanner_ignores_a_corrupt_profile_registry(app_config):
    session_id = "01a19190-eee0-7bc0-ba72-faf1ec22edd8"
    seed_grok(app_config.grok_root, session_id=session_id)
    (app_config.grok_root / "profiles.json").write_text("{", encoding="utf-8")

    report = SessionScanner(app_config).scan(providers=("grok",))

    assert [session.session_id for session in report.sessions] == [session_id]
    assert report.sessions[0].resume_command.endswith(f"grok --resume {session_id}")
    assert report.sessions[0].metadata.get("profile") == "default"
