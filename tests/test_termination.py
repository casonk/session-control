from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from session_control.actions import SessionActionError, SessionActionService
from session_control.cli import main
from session_control.config import AppConfig
from session_control.scanner import SessionScanner
from session_control.web import create_app
from tests.helpers import seed_codex


def test_terminate_current_requires_exact_codex_id_and_records_private_coverage(
    app_config, monkeypatch
):
    path = seed_codex(app_config.codex_root)
    service = SessionActionService(app_config, SessionScanner(app_config))
    session = service.scanner.scan(providers=("codex",)).sessions[0]

    with pytest.raises(SessionActionError, match="required"):
        service.terminate_current("")
    with pytest.raises(SessionActionError, match="uniquely"):
        service.terminate_current("other-thread")
    assert path.exists()

    def fail_if_killed(*args):
        raise AssertionError("self termination must not kill its own tmux window")

    monkeypatch.setattr("session_control.actions._kill_tmux_windows_for_session", fail_if_killed)

    result = service.terminate_current(session.session_id)
    assert not path.exists()
    assert (result.moved_to / path.name).exists()
    records = service.digest_store.records()
    assert len(records) == 1
    assert records[0]["repo"] == "example-project"
    assert records[0]["reason"] == "self-terminate"
    digest_text = next(service.digest_store.root.glob("*.json")).read_text()
    assert "Fix statement downloader" not in digest_text
    assert "/workspace/" not in digest_text
    assert session.session_id not in digest_text


def test_rotate_expires_trash_and_digest_separately(app_config):
    path = seed_codex(app_config.codex_root)
    service = SessionActionService(app_config)
    session = service.scanner.scan(providers=("codex",)).sessions[0]
    result = service.delete(session.public_id)
    manifest = result.moved_to / ".session-control-restore.json"
    old_timestamp = "20200101T000000Z"
    old_batch = result.moved_to.parent.parent.parent / old_timestamp / "codex" / session.session_id
    old_batch.parent.mkdir(parents=True)
    result.moved_to.rename(old_batch)
    old_manifest = old_batch / manifest.name
    metadata = json.loads(old_manifest.read_text())
    metadata["moved_at"] = old_timestamp
    old_manifest.write_text(json.dumps(metadata))
    digest = next(service.digest_store.root.glob("*.json"))
    old_time = (datetime.now(timezone.utc) - timedelta(days=400)).timestamp()
    os.utime(digest, (old_time, old_time))

    assert service.rotate(dry_run=True) == (1, 1)
    assert path.name in {item.name for item in old_batch.iterdir()}
    assert digest.exists()
    assert service.rotate() == (1, 1)
    assert not old_batch.exists()
    assert not digest.exists()


def test_coverage_dashboard_shows_only_category_and_repo_counts(app_config):
    seed_codex(app_config.codex_root)
    service = SessionActionService(app_config)
    session = service.scanner.scan(providers=("codex",)).sessions[0]
    service.delete(session.public_id)

    response = create_app(app_config).test_client().get("/coverage")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "example-project" in html
    assert "Session coverage" in html
    assert "Fix statement downloader" not in html
    assert session.session_id not in html


def test_terminate_current_cli_uses_codex_thread_id(app_config, monkeypatch, capsys):
    path = seed_codex(app_config.codex_root)
    session = SessionScanner(app_config).scan(providers=("codex",)).sessions[0]
    monkeypatch.setattr(AppConfig, "from_env", classmethod(lambda cls: app_config))
    monkeypatch.setenv("CODEX_THREAD_ID", session.session_id)

    assert main(["terminate-current"]) == 0
    assert not path.exists()
    assert "Moved current Codex session to trash" in capsys.readouterr().out
