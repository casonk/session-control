from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from session_control.actions import SessionActionError, SessionActionService
from session_control.scanner import SessionScanner
from tests.helpers import seed_continue, seed_copilot


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


def test_delete_refuses_live_copilot_lock(app_config):
    session_dir = seed_copilot(app_config.copilot_root)
    (session_dir / f"inuse.{os.getpid()}.lock").write_text("", encoding="utf-8")
    scanner = SessionScanner(app_config)
    session = scanner.scan(providers=("copilot",)).sessions[0]

    with pytest.raises(SessionActionError, match="active"):
        SessionActionService(app_config, scanner).delete(session.public_id)

    assert Path(session_dir).exists()
