"""Shared agent memory: condensation before removal, search and redaction."""

from __future__ import annotations

import json
import stat
import urllib.error
from datetime import timedelta
from unittest import mock

import pytest

from session_control import cli, memory
from session_control.actions import SessionActionError, SessionActionService
from session_control.memory import (
    ExtractiveCondenser,
    MemoryStore,
    MemoryStoreError,
    OllamaCondenser,
    build_condenser,
)
from session_control.scanner import SessionScanner
from tests.helpers import seed_claude, write_jsonl

SECRET = "ghp_" + "a" * 36


def _claude_session(app_config, *, secret=False):
    path = seed_claude(app_config.claude_root)
    outcome = f"Wrote src/crew/service.py. Token {SECRET}" if secret else "Wrote src/crew/service.py"
    write_jsonl(
        path,
        [
            {
                "type": "user",
                "sessionId": "c7df09f0-b9f2-4563-853e-f64fd095128f",
                "cwd": "/workspace/crew-chief",
                "timestamp": "2026-04-02T10:00:00Z",
                "message": {"role": "user", "content": "Build the local LLM service"},
            },
            {
                "type": "assistant",
                "sessionId": "c7df09f0-b9f2-4563-853e-f64fd095128f",
                "timestamp": "2026-04-02T10:05:00Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": outcome}]},
            },
        ],
    )
    return SessionScanner(app_config).scan().sessions[0]


def test_condense_writes_private_redacted_note_and_index(app_config):
    session = _claude_session(app_config, secret=True)
    store = SessionActionService(app_config).memory_store
    entry = store.condense(session, ExtractiveCondenser(), reason="prune")

    text = entry.path.read_text()
    assert "Build the local LLM service" in text
    assert "src/crew/service.py" in text
    assert SECRET not in text
    assert stat.S_IMODE(entry.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert entry.entry_id in store.index_path.read_text()
    assert store.get(entry.entry_id).meta["reason"] == "prune"


def test_recondensing_refreshes_instead_of_duplicating(app_config):
    session = _claude_session(app_config)
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    store.condense(session, ExtractiveCondenser(), reason="manual")
    store.condense(session, ExtractiveCondenser(), reason="prune")
    assert len(store.entries()) == 1
    assert store.entries()[0].meta["reason"] == "prune"


def test_search_ranks_title_and_body_and_ignores_misses(app_config):
    session = _claude_session(app_config)
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    store.condense(session, ExtractiveCondenser(), reason="manual")
    assert store.search("llm service")[0][1].meta["provider"] == "claude"
    assert store.search("kubernetes") == []
    assert store.search("   ") == []


def test_get_rejects_path_traversal(app_config):
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    assert store.get("../../etc/passwd") is None


def test_delete_condenses_first_and_prune_can_opt_out(app_config):
    session = _claude_session(app_config)
    service = SessionActionService(app_config)
    service.delete(session.public_id)
    assert len(service.memory_store.entries()) == 1
    assert not session.primary_path.exists()


def test_no_condense_skips_the_note(app_config):
    session = _claude_session(app_config)
    service = SessionActionService(app_config)
    service._delete_session(session, condense=False)
    assert service.memory_store.entries() == []


def test_failure_to_condense_blocks_removal(app_config):
    session = _claude_session(app_config)
    service = SessionActionService(app_config)
    with mock.patch.object(MemoryStore, "condense", side_effect=OSError("disk full")):
        with pytest.raises(SessionActionError, match="condense"):
            service.delete(session.public_id)
    assert session.primary_path.exists()


def test_disabled_config_removes_without_a_note(app_config):
    from dataclasses import replace

    config = replace(app_config, condense_on_remove=False)
    session = _claude_session(config)
    service = SessionActionService(config)
    service.delete(session.public_id)
    assert service.memory_store.entries() == []


def test_prune_condenses_unless_told_not_to(app_config):
    session = _claude_session(app_config)
    service = SessionActionService(app_config)
    result = service.prune(timedelta(days=1), condense=False)
    assert len(result.deleted) == 1
    assert service.memory_store.entries() == []
    assert session.provider == "claude"


def test_ollama_condenser_refuses_remote_hosts():
    with pytest.raises(MemoryStoreError):
        OllamaCondenser("http://example.com:11434", "m")
    assert isinstance(build_condenser("ollama", "http://example.com", "m"), ExtractiveCondenser)
    assert isinstance(build_condenser("extractive", None, None), ExtractiveCondenser)
    assert isinstance(build_condenser("ollama", "http://127.0.0.1:11434", "m"), OllamaCondenser)


def test_ollama_condenser_uses_model_and_redacts_output(app_config):
    session = _claude_session(app_config)
    condenser = OllamaCondenser("http://127.0.0.1:11434", "tiny")
    with mock.patch.object(condenser, "_generate", return_value=f"- Goal: ship {SECRET}"):
        store = MemoryStore(app_config.trash_dir.parent / "memory")
        entry = store.condense(session, condenser, reason="manual")
    assert entry.meta["backend"] == "ollama:tiny"
    assert "Goal: ship" in entry.body
    assert SECRET not in entry.path.read_text()


def test_ollama_condenser_falls_back_when_service_is_down(app_config):
    session = _claude_session(app_config)
    condenser = OllamaCondenser("http://127.0.0.1:1", "tiny")
    with mock.patch.object(condenser, "_generate", side_effect=urllib.error.URLError("down")):
        store = MemoryStore(app_config.trash_dir.parent / "memory")
        entry = store.condense(session, condenser, reason="manual")
    assert entry.meta["backend"] == "extractive"


def test_prompt_is_bounded(app_config):
    session = _claude_session(app_config)
    turns = [memory.Turn("User", "x" * 5000) for _ in range(50)]
    assert len(memory._llm_prompt(session, turns)) < memory.MAX_LLM_INPUT_CHARS + 6000


def test_cli_condense_and_memory_commands(app_config, capsys):
    session = _claude_session(app_config)
    with mock.patch.object(cli.AppConfig, "from_env", return_value=app_config):
        assert cli.main(["condense", session.public_id]) == 0
        assert cli.main(["memory", "list"]) == 0
        assert "crew-chief" in capsys.readouterr().out
        assert cli.main(["memory", "search", "llm", "--json"]) == 0
        hits = json.loads(capsys.readouterr().out)
        assert hits[0]["provider"] == "claude"
        assert cli.main(["memory", "show", f"claude-{session.public_id}"]) == 0
        assert "Opening request" in capsys.readouterr().out
        assert cli.main(["memory", "show", "missing"]) == 1
        assert cli.main(["memory", "index"]) == 0


# ---------------------------------------------------------------------- web UI


def _web(app_config):
    from session_control.web import create_app

    app = create_app(app_config)
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["csrf_token"] = "tok"
    return client


def test_web_memory_pages_list_search_and_show(app_config):
    session = _claude_session(app_config, secret=True)
    client = _web(app_config)
    assert "No condensed notes yet" in client.get("/memory").get_data(as_text=True)

    entry_id = f"claude-{session.public_id}"
    response = client.post(
        f"/sessions/{session.public_id}/condense", data={"csrf_token": "tok"}
    )
    assert response.status_code == 302 and response.headers["Location"].endswith(entry_id)

    listing = client.get("/memory").get_data(as_text=True)
    assert entry_id in listing and "Build the local LLM service" in listing
    assert entry_id in client.get("/memory?q=llm").get_data(as_text=True)
    assert entry_id not in client.get("/memory?q=kubernetes").get_data(as_text=True)

    page = client.get(f"/memory/{entry_id}").get_data(as_text=True)
    assert "Opening request" in page and SECRET not in page
    assert client.get("/memory/nope").status_code == 404
    assert client.get("/memory/..%2Fx").status_code == 404

    assert client.get("/api/memory?q=llm").get_json()[0]["provider"] == "claude"
    assert "memory note" in client.get("/").get_data(as_text=True)
    assert "View memory note" in client.get(f"/sessions/{session.public_id}").get_data(as_text=True)


def test_web_memory_delete_requires_csrf_and_removes_note(app_config):
    session = _claude_session(app_config)
    client = _web(app_config)
    client.post(f"/sessions/{session.public_id}/condense", data={"csrf_token": "tok"})
    entry_id = f"claude-{session.public_id}"
    assert client.post(f"/memory/{entry_id}/delete").status_code == 403
    assert client.get(f"/memory/{entry_id}").status_code == 200
    client.post(f"/memory/{entry_id}/delete", data={"csrf_token": "tok"})
    assert client.get(f"/memory/{entry_id}").status_code == 404


def test_web_delete_session_leaves_a_note(app_config):
    session = _claude_session(app_config)
    client = _web(app_config)
    client.post(f"/sessions/{session.public_id}/delete", data={"csrf_token": "tok"})
    assert f"claude-{session.public_id}" in client.get("/memory").get_data(as_text=True)


def test_condense_all_backfills_once_and_skips_existing(app_config):
    session = _claude_session(app_config)
    service = SessionActionService(app_config)
    written, skipped, errors = service.condense_all()
    assert written == [f"claude-{session.public_id}"] and errors == []
    assert session.primary_path.exists()
    written, skipped, _ = service.condense_all()
    assert written == [] and skipped == 1
    assert service.condense_all(refresh=True)[0] == [f"claude-{session.public_id}"]
    assert service.condense_all(older_than=timedelta(days=36500), refresh=True)[0] == []


def test_cli_condense_all(app_config, capsys):
    _claude_session(app_config)
    with mock.patch.object(cli.AppConfig, "from_env", return_value=app_config):
        assert cli.main(["condense", "--all"]) == 0
        assert "Condensed 1 session(s)" in capsys.readouterr().out
        with pytest.raises(SystemExit):
            cli.main(["condense"])
