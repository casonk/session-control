from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from session_control.actions import SessionActionError, SessionActionService
from session_control.scanner import SessionScanner
from tests.helpers import seed_codex, seed_continue, seed_copilot, write_json


def test_delete_moves_continue_session_to_trash_and_updates_index(app_config):
    session_path = seed_continue(app_config.continue_root)
    scanner = SessionScanner(app_config)
    session = scanner.scan(providers=("continue",)).sessions[0]

    result = SessionActionService(app_config, scanner).delete(session.public_id)

    assert result.moved_count == 1
    assert not session_path.exists()
    assert (result.moved_to / session_path.name).exists()
    index = json.loads((app_config.continue_root / "sessions" / "sessions.json").read_text())
    assert index == []


def test_open_creates_and_selects_persistent_tmux_window(app_config, monkeypatch):
    seed_continue(app_config.continue_root)
    scanner = SessionScanner(app_config)
    session = scanner.scan(providers=("continue",)).sessions[0]
    calls = []

    def fake_run(args, capture_output):
        calls.append(args)
        if args[:2] == ["tmux", "new-window"]:
            return subprocess.CompletedProcess(args, 0, stdout=b"@12\n", stderr=b"")
        if args[:2] == ["tmux", "select-window"]:
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr("session_control.actions.subprocess.run", fake_run)

    result = SessionActionService(app_config, scanner).open_in_webterm(session.public_id)

    assert result.session == session
    assert calls[0][:7] == ["tmux", "new-window", "-d", "-P", "-F", "#{window_id}", "-t"]
    assert calls[0][7] == app_config.tmux_session
    assert calls[0][-1].startswith("bash -lc ")
    assert "[session-control] Resume command exited" in calls[0][-1]
    assert calls[1] == ["tmux", "select-window", "-t", "@12"]


def test_open_can_override_codex_permissions_for_launch(app_config, monkeypatch):
    seed_codex(app_config.codex_root, model="gpt-5.4")
    scanner = SessionScanner(app_config)
    session = scanner.scan(providers=("codex",)).sessions[0]
    calls = []

    def fake_run(args, capture_output):
        calls.append(args)
        if args[:2] == ["tmux", "new-window"]:
            return subprocess.CompletedProcess(args, 0, stdout=b"@12\n", stderr=b"")
        if args[:2] == ["tmux", "select-window"]:
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr("session_control.actions.subprocess.run", fake_run)

    SessionActionService(app_config, scanner).open_in_webterm(
        session.public_id,
        codex_permission_preset="full-auto",
    )

    assert "--sandbox danger-full-access" in calls[0][-1]
    assert "--ask-for-approval never" in calls[0][-1]


def test_delete_refuses_live_copilot_lock(app_config):
    session_dir = seed_copilot(app_config.copilot_root)
    (session_dir / f"inuse.{os.getpid()}.lock").write_text("", encoding="utf-8")
    scanner = SessionScanner(app_config)
    session = scanner.scan(providers=("copilot",)).sessions[0]

    with pytest.raises(SessionActionError, match="active"):
        SessionActionService(app_config, scanner).delete(session.public_id)

    assert Path(session_dir).exists()


def test_prune_dry_run_reports_old_sessions_without_deleting(app_config):
    old_path = _seed_continue_session(
        app_config.continue_root,
        session_id="11111111-1111-4111-8111-111111111111",
        title="Old Continue session",
        mtime=datetime(2025, 10, 1, tzinfo=timezone.utc),
    )

    result = SessionActionService(app_config).prune(
        timedelta(days=180),
        providers=("continue",),
        dry_run=True,
        now=datetime(2026, 5, 3, tzinfo=timezone.utc),
    )

    assert [session.session_id for session in result.eligible] == [
        "11111111-1111-4111-8111-111111111111"
    ]
    assert result.deleted == ()
    assert old_path.exists()
    index = json.loads((app_config.continue_root / "sessions" / "sessions.json").read_text())
    assert [item["sessionId"] for item in index] == ["11111111-1111-4111-8111-111111111111"]


def test_prune_deletes_only_sessions_older_than_cutoff(app_config):
    old_path = _seed_continue_session(
        app_config.continue_root,
        session_id="22222222-2222-4222-8222-222222222222",
        title="Old Continue session",
        mtime=datetime(2025, 10, 1, tzinfo=timezone.utc),
    )
    recent_path = _seed_continue_session(
        app_config.continue_root,
        session_id="33333333-3333-4333-8333-333333333333",
        title="Recent Continue session",
        mtime=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )

    result = SessionActionService(app_config).prune(
        timedelta(days=180),
        providers=("continue",),
        now=datetime(2026, 5, 3, tzinfo=timezone.utc),
    )

    assert [deleted.session.session_id for deleted in result.deleted] == [
        "22222222-2222-4222-8222-222222222222"
    ]
    assert not old_path.exists()
    assert recent_path.exists()
    assert (result.deleted[0].moved_to / old_path.name).exists()
    index = json.loads((app_config.continue_root / "sessions" / "sessions.json").read_text())
    assert [item["sessionId"] for item in index] == ["33333333-3333-4333-8333-333333333333"]


def test_prune_skips_old_live_copilot_sessions(app_config):
    session_dir = seed_copilot(app_config.copilot_root)
    (session_dir / f"inuse.{os.getpid()}.lock").write_text("", encoding="utf-8")

    result = SessionActionService(app_config).prune(
        timedelta(days=180),
        providers=("copilot",),
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert result.eligible == ()
    assert [(item.session.session_id, item.reason) for item in result.skipped] == [
        ("3e6d83f8-5629-47d2-9a64-2779c509c808", "active")
    ]
    assert session_dir.exists()


def _seed_continue_session(
    root: Path,
    *,
    session_id: str,
    title: str,
    mtime: datetime,
) -> Path:
    sessions_root = root / "sessions"
    index_path = sessions_root / "sessions.json"
    existing = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    existing.append(
        {
            "sessionId": session_id,
            "title": title,
            "dateCreated": "1759276800000",
            "workspaceDirectory": "/workspace/session-control",
            "messageCount": 1,
        }
    )
    write_json(index_path, existing)
    session_path = sessions_root / f"{session_id}.json"
    write_json(
        session_path,
        {
            "sessionId": session_id,
            "title": title,
            "workspaceDirectory": "/workspace/session-control",
            "dateCreated": "1759276800000",
            "history": [{"message": {"role": "user", "content": "Review old sessions"}}],
        },
    )
    timestamp = mtime.timestamp()
    os.utime(session_path, (timestamp, timestamp))
    os.utime(index_path, (timestamp, timestamp))
    return session_path
