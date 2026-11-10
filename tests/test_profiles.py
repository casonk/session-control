from __future__ import annotations

import json
from pathlib import Path

from session_control.profile_cli import main as profile_main
from session_control.profiles import (
    ProfileEntry,
    ProfileRegistry,
    ProfileRouteRule,
    get_usage_summary,
    harvest_usage,
    init_usage_db,
    load_profiles,
    resolve_profile_for_context,
    save_profiles,
)
from session_control.scanner import SessionScanner
from tests.helpers import seed_agy, write_jsonl


def test_load_and_save_profiles(tmp_path: Path):
    cfg_path = tmp_path / "profiles.json"
    registry = load_profiles(cfg_path)
    assert "default" in registry.profiles
    assert registry.active_profile == "default"

    # Add a new profile
    registry.profiles["work"] = ProfileEntry(
        name="work",
        email="dev@work.com",
        gemini_dir=str(tmp_path / "profiles" / "work"),
        description="Work Profile",
    )
    registry.routes.append(ProfileRouteRule(pattern="*/work-repos/*", profile="work"))
    save_profiles(registry, cfg_path)

    # Reload and assert
    reloaded = load_profiles(cfg_path)
    assert "work" in reloaded.profiles
    assert reloaded.profiles["work"].email == "dev@work.com"
    assert any(r.pattern == "*/work-repos/*" for r in reloaded.routes)


def test_resolve_profile_for_context(tmp_path: Path):
    registry = ProfileRegistry(
        default_profile="default",
        active_profile="default",
        profiles={
            "default": ProfileEntry(name="default", email="personal@gmail.com", enabled=True),
            "academic": ProfileEntry(name="academic", email="student@umich.edu", enabled=True),
            "client_a": ProfileEntry(name="client_a", email="client@corp.com", enabled=True),
        },
        routes=[
            ProfileRouteRule(pattern="*university-coursework*", profile="academic"),
            ProfileRouteRule(pattern="*/clients/corp/*", profile="client_a"),
        ],
    )

    # Route matching directory
    res_academic = resolve_profile_for_context(
        registry, cwd=Path("/mnt/data/university-coursework/cs101")
    )
    assert res_academic.name == "academic"

    # Explicit override
    res_explicit = resolve_profile_for_context(
        registry, cwd=Path("/mnt/data/university-coursework/cs101"), requested_profile="client_a"
    )
    assert res_explicit.name == "client_a"

    # Default fallback
    res_default = resolve_profile_for_context(registry, cwd=Path("/mnt/data/personal-repo"))
    assert res_default.name == "default"

    # Failover exclusion
    res_failover = resolve_profile_for_context(
        registry,
        cwd=Path("/mnt/data/university-coursework/cs101"),
        failover_exclude={"academic"},
    )
    assert res_failover.name == "default"


def test_harvest_usage_and_summary(tmp_path: Path):
    prof_dir = tmp_path / "profiles" / "user1"
    agy_root = prof_dir / "antigravity-cli"
    seed_agy(agy_root, session_id="test-session-123")

    transcript_file = (
        agy_root / "brain" / "test-session-123" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    write_jsonl(
        transcript_file,
        [
            {
                "step_index": 0,
                "created_at": "2026-11-08T10:00:00Z",
                "input_tokens": 1500,
                "output_tokens": 200,
                "cache_read_tokens": 5000,
            },
            {
                "step_index": 1,
                "created_at": "2026-11-08T10:05:00Z",
                "input_tokens": 3000,
                "output_tokens": 400,
                "cache_read_tokens": 10000,
            },
        ],
    )

    registry = ProfileRegistry(
        default_profile="user1",
        active_profile="user1",
        profiles={"user1": ProfileEntry(name="user1", gemini_dir=str(prof_dir), enabled=True)},
    )

    db_path = tmp_path / "test_usage.sqlite"
    init_usage_db(db_path)

    stats = harvest_usage(registry, db_path=db_path)
    assert stats["conversations_processed"] == 1
    assert stats["new_steps"] == 2

    # Verify summary
    summary = get_usage_summary(db_path=db_path)
    assert len(summary["profiles"]) == 1
    p_stat = summary["profiles"][0]
    assert p_stat["profile"] == "user1"
    assert p_stat["total_input"] == 4500
    assert p_stat["total_output"] == 600
    assert p_stat["total_cache"] == 15000
    assert p_stat["total_tokens"] == 20100

    # Incremental harvest should skip unchanged transcripts
    stats2 = harvest_usage(registry, db_path=db_path)
    assert stats2["conversations_processed"] == 0
    assert stats2["new_steps"] == 0


def test_profile_cli_commands(tmp_path: Path, monkeypatch, capsys):
    cfg_path = tmp_path / "profiles.json"
    db_path = tmp_path / "usage.sqlite"
    monkeypatch.setenv("SESSION_CONTROL_PROFILES_PATH", str(cfg_path))
    monkeypatch.setenv("SESSION_CONTROL_USAGE_DB", str(db_path))

    # Add profile
    rc = profile_main(["add", "team_a", "--email", "team@example.com"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Profile 'team_a' added successfully." in out

    # List profiles
    rc = profile_main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "team_a" in out
    assert "team@example.com" in out

    # Switch profile
    rc = profile_main(["switch", "team_a"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Switched active profile to 'team_a'." in out

    # Route management
    rc = profile_main(["route", "add", "*/team-a/*", "team_a"])
    assert rc == 0
    rc = profile_main(["route", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "*/team-a/*" in out
    assert "team_a" in out

    # Current profile
    rc = profile_main(["current"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Active default: team_a" in out

    # Usage report
    rc = profile_main(["usage", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "profiles" in parsed


def test_execute_passes_gemini_dir_for_the_selected_profile(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "profiles.json"
    db_path = tmp_path / "usage.sqlite"
    profile_home = tmp_path / "team_a"
    monkeypatch.setenv("SESSION_CONTROL_PROFILES_PATH", str(cfg_path))
    monkeypatch.setenv("SESSION_CONTROL_USAGE_DB", str(db_path))
    seen = {}

    def fake_run(cmd, env, cwd):
        seen["cmd"] = cmd
        seen["env_dir"] = env.get("GEMINI_DIR")
        return type("Proc", (), {"returncode": 0})()

    monkeypatch.setattr("session_control.profiles.subprocess.run", fake_run)
    profile_main(["add", "team_a", "--email", "team@example.com", "--dir", str(profile_home)])

    rc = profile_main(["run", "--profile", "team_a", "--print", "hello"])

    assert rc == 0
    assert seen["cmd"] == ["agy", "--gemini_dir", str(profile_home), "--print", "hello"]
    assert seen["env_dir"] == str(profile_home.resolve())


def test_scanner_discovers_sessions_in_multiple_profiles(app_config, tmp_path: Path):
    # Setup custom profile with its own conversation
    prof_gemini = tmp_path / "gemini_team_b"
    prof_agy = prof_gemini / "antigravity-cli"
    seed_agy(prof_agy, session_id="session-team-b-999")

    # In app_config.agy_root's parent, create profiles.json
    cfg_path = app_config.agy_root.parent / "profiles.json"
    registry = ProfileRegistry(
        default_profile="default",
        active_profile="default",
        profiles={
            "default": ProfileEntry(name="default", gemini_dir=str(app_config.agy_root.parent)),
            "team_b": ProfileEntry(name="team_b", gemini_dir=str(prof_gemini)),
        },
    )
    save_profiles(registry, cfg_path)

    seed_agy(app_config.agy_root, session_id="session-default-001")

    scanner = SessionScanner(app_config)
    report = scanner.scan(providers=("agy",))

    session_ids = {s.session_id for s in report.sessions}
    assert "session-default-001" in session_ids
    assert "session-team-b-999" in session_ids

    by_id = {s.session_id: s for s in report.sessions}
    assert by_id["session-default-001"].metadata.get("profile") == "default"
    assert by_id["session-team-b-999"].metadata.get("profile") == "team_b"
    assert "agy-profile" in by_id["session-team-b-999"].resume_command
