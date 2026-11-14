"""Shared agent memory: condensation before removal, search and redaction."""

from __future__ import annotations

import json
import stat
import urllib.error
from datetime import datetime, timedelta, timezone
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
    outcome = (
        f"Wrote src/crew/service.py. Token {SECRET}" if secret else "Wrote src/crew/service.py"
    )
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
    with (
        mock.patch.object(MemoryStore, "condense", side_effect=OSError("disk full")),
        pytest.raises(SessionActionError, match="condense"),
    ):
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
    response = client.post(f"/sessions/{session.public_id}/condense", data={"csrf_token": "tok"})
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


def test_ollama_condenser_drops_reasoning_blocks(app_config):
    session = _claude_session(app_config)
    condenser = OllamaCondenser("http://127.0.0.1:11434", "tiny")
    reply = "<think>plan the answer\nsecretly</think>\n## Goal\n- ship it"
    with mock.patch.object(condenser, "_generate", return_value=reply):
        entry = MemoryStore(app_config.trash_dir.parent / "memory").condense(
            session, condenser, reason="manual"
        )
    assert "plan the answer" not in entry.body
    assert "\n### Goal" in entry.body and "\n## Goal" not in entry.body
    unfinished = "<think>never closes"
    with mock.patch.object(condenser, "_generate", return_value=unfinished):
        entry = MemoryStore(app_config.trash_dir.parent / "memory").condense(
            session, condenser, reason="manual"
        )
    assert entry.meta["backend"] == "extractive"


def test_cli_model_flag_selects_the_ollama_condenser_for_one_run(app_config, capsys):
    _claude_session(app_config)
    seen = []

    def fake_generate(self, prompt):
        seen.append(self.model)
        return "## Goal\n- done"

    with (
        mock.patch.object(cli.AppConfig, "from_env", return_value=app_config),
        mock.patch.object(OllamaCondenser, "_generate", fake_generate),
    ):
        assert cli.main(["condense", "--all", "--model", "qwen3:14b"]) == 0
    assert seen == ["qwen3:14b"]
    entry = SessionActionService(app_config).memory_store.entries()[0]
    assert entry.meta["backend"] == "ollama:qwen3:14b"


# ------------------------------------------------- deferred model upgrade, stale, retention


def test_background_condense_writes_fast_note_then_upgrades_it(app_config):
    from dataclasses import replace

    config = replace(
        app_config,
        condenser="ollama",
        condenser_url="http://127.0.0.1:11434",
        condenser_model="tiny",
    )
    session = _claude_session(config)
    service = SessionActionService(config, background_condense=True)
    jobs = []
    service._submit_background = jobs.append  # run the upgrade by hand, deterministically

    with mock.patch.object(OllamaCondenser, "_generate", return_value="## Goal\n- upgraded"):
        service.delete(session.public_id)
        entry_id = f"claude-{session.public_id}"
        before = service.memory_store.get(entry_id)
        assert before.meta["backend"] == "extractive" and "Opening request" in before.body
        assert not session.primary_path.exists()  # removal did not wait for the model
        assert len(jobs) == 1
        jobs[0]()
    after = service.memory_store.get(entry_id)
    assert after.meta["backend"] == "ollama:tiny" and "upgraded" in after.body


def test_background_upgrade_failure_keeps_the_fast_note(app_config):
    from dataclasses import replace

    config = replace(
        app_config,
        condenser="ollama",
        condenser_url="http://127.0.0.1:11434",
        condenser_model="tiny",
    )
    session = _claude_session(config)
    service = SessionActionService(config, background_condense=True)
    jobs = []
    service._submit_background = jobs.append
    with mock.patch.object(OllamaCondenser, "_generate", side_effect=urllib.error.URLError("x")):
        service.delete(session.public_id)
        jobs[0]()
    assert service.memory_store.entries()[0].meta["backend"] == "extractive"


def test_a_real_background_worker_finishes(app_config):
    from dataclasses import replace

    config = replace(
        app_config,
        condenser="ollama",
        condenser_url="http://127.0.0.1:11434",
        condenser_model="tiny",
    )
    session = _claude_session(config)
    service = SessionActionService(config, background_condense=True)
    with mock.patch.object(OllamaCondenser, "_generate", return_value="- done"):
        service.delete(session.public_id)
        service._executor.shutdown(wait=True)
    assert service.memory_store.entries()[0].meta["backend"] == "ollama:tiny"


def test_condense_all_stale_rewrites_only_changed_sessions(app_config):
    session = _claude_session(app_config)
    service = SessionActionService(app_config)
    service.condense_all()
    assert service.condense_all(stale_only=True)[0] == []  # note is newer than the session

    store = service.memory_store
    entry = store.entries()[0]
    old = entry.meta | {"condensed_at": "2020-01-01T00:00:00+00:00"}
    entry.path.write_text(memory._render(old, entry.body), encoding="utf-8")
    assert service.condense_all(stale_only=True)[0] == [f"claude-{session.public_id}"]


def _note(store, name, repo, condensed_at):
    meta = {
        "provider": "claude",
        "session_id": name,
        "title": name,
        "repo": repo,
        "condensed_at": condensed_at,
    }
    path = store.entries_dir / f"claude-{name}.md"
    store._write_private(path, memory._render(meta, f"# {name}\n"))


def test_rotate_by_age_and_per_repo_cap_with_dry_run(app_config):
    from datetime import timedelta

    store = MemoryStore(app_config.trash_dir.parent / "memory")
    _note(store, "old", "a", "2020-01-01T00:00:00+00:00")
    _note(store, "a1", "a", "2999-01-03T00:00:00+00:00")
    _note(store, "a2", "a", "2999-01-02T00:00:00+00:00")
    _note(store, "b1", "b", "2999-01-01T00:00:00+00:00")

    assert {e.entry_id for e in store.rotate(older_than=timedelta(days=30), dry_run=True)} == {
        "claude-old"
    }
    assert len(store.entries()) == 4  # dry run deletes nothing

    removed = store.rotate(max_per_repo=1)
    assert {e.entry_id for e in removed} == {"claude-old", "claude-a2"}
    assert {e.entry_id for e in store.entries()} == {"claude-a1", "claude-b1"}
    assert "claude-a2" not in store.index_path.read_text()


def test_cli_memory_prune_and_stale(app_config, capsys):
    session = _claude_session(app_config)
    with mock.patch.object(cli.AppConfig, "from_env", return_value=app_config):
        assert cli.main(["condense", "--all", "--stale"]) == 0
        assert cli.main(["memory", "prune"]) == 2
        assert cli.main(["memory", "prune", "--max-per-repo", "0"]) == 2
        assert cli.main(["memory", "prune", "--older-than", "1d", "--dry-run"]) == 0
        assert "Would delete 0" in capsys.readouterr().out.splitlines()[-1]
        assert cli.main(["memory", "prune", "--max-per-repo", "1"]) == 0
    assert len(SessionActionService(app_config).memory_store.entries()) == 1
    assert session.provider == "claude"


def test_web_note_page_renders_markdown_and_blocks_markup(app_config):
    session = _claude_session(app_config)
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    entry = store.condense(session, ExtractiveCondenser(), reason="manual")
    entry.path.write_text(
        entry.path.read_text() + "\n- <script>alert(1)</script> **bold**\n", encoding="utf-8"
    )
    page = _web(app_config).get(f"/memory/{entry.entry_id}").get_data(as_text=True)
    assert "<strong>bold</strong>" in page and "<h3>Summary</h3>" in page
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


def test_ollama_asks_for_no_reasoning_and_retries_for_models_without_it():
    condenser = OllamaCondenser("http://127.0.0.1:11434", "plain")
    sent = []

    def post(prompt, *, think):
        sent.append(think)
        if think is False:
            raise urllib.error.HTTPError("u", 400, "no thinking", {}, None)
        return "ok"

    with mock.patch.object(condenser, "_post", post):
        assert condenser._generate("p") == "ok"
    assert sent == [False, None]

    def server_error(prompt, *, think):
        raise urllib.error.HTTPError("u", 500, "boom", {}, None)

    with mock.patch.object(condenser, "_post", server_error), pytest.raises(urllib.error.HTTPError):
        condenser._generate("p")


def test_model_failure_is_logged_and_cleared_on_success(app_config):
    session = _claude_session(app_config)
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    condenser = OllamaCondenser("http://127.0.0.1:1", "tiny")
    entry_id = f"claude-{session.public_id}"
    with mock.patch.object(condenser, "_generate", side_effect=urllib.error.URLError("down")):
        store.condense(session, condenser, reason="manual")
        store.condense(session, condenser, reason="manual")
    failure = store.failures()[entry_id]
    assert failure["attempts"] == 2 and "down" in failure["error"]
    assert stat.S_IMODE(store.failures_path.stat().st_mode) == 0o600
    with mock.patch.object(condenser, "_generate", return_value="- ok"):
        store.condense(session, condenser, reason="manual")
    assert store.failures() == {}


def test_failure_log_redacts_and_empty_response_counts(app_config):
    session = _claude_session(app_config)
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    condenser = OllamaCondenser("http://127.0.0.1:1", "tiny")
    with mock.patch.object(condenser, "_generate", return_value="<think>x</think>"):
        store.condense(session, condenser, reason="manual")
    assert "empty" in next(iter(store.failures().values()))["error"]
    store.record_failure("claude-x", f"boom {SECRET}")
    assert SECRET not in store.failures_path.read_text()


def test_deferred_upgrade_failure_is_logged(app_config):
    session = _claude_session(app_config)
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    slow = OllamaCondenser("http://127.0.0.1:1", "tiny")
    jobs = []
    with mock.patch.object(slow, "_generate", side_effect=OSError("timed out")):
        store.condense_deferred(
            session, ExtractiveCondenser(), slow, reason="manual", submit=jobs.append
        )
        assert store.failures() == {}
        jobs[0]()
    assert "timed out" in store.failures()[f"claude-{session.public_id}"]["error"]


def test_condense_failed_retries_only_logged_sessions(app_config, capsys):
    from dataclasses import replace

    session = _claude_session(app_config)
    config = replace(
        app_config,
        condenser="ollama",
        condenser_url="http://127.0.0.1:1",
        condenser_model="tiny",
    )
    service = SessionActionService(config)
    entry_id = f"claude-{session.public_id}"
    with mock.patch.object(OllamaCondenser, "_generate", side_effect=OSError("down")):
        service.condense_all()
    assert entry_id in service.memory_store.failures()
    service.memory_store.record_failure("claude-gone", "old")

    with mock.patch.object(OllamaCondenser, "_generate", return_value="- fixed"):
        written, _, errors = service.condense_all(failed_only=True)
    assert written == [entry_id] and errors == []
    assert service.memory_store.failures() == {}  # fixed and the vanished session dropped
    assert service.condense_all(failed_only=True)[0] == []  # nothing left to retry


def test_cli_memory_failures_and_failed_flag(app_config, capsys):
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    with mock.patch.object(cli.AppConfig, "from_env", return_value=app_config):
        assert cli.main(["memory", "failures"]) == 0
        assert "No failed condensations" in capsys.readouterr().out
        store.record_failure("claude-abc", "URLError: down")
        assert cli.main(["memory", "failures"]) == 0
        assert "claude-abc" in capsys.readouterr().out
        assert cli.main(["condense", "--failed"]) == 0
        assert "Condensed 0 session(s)" in capsys.readouterr().out


def test_removal_and_model_fallback_are_logged(app_config, caplog):
    caplog.set_level("INFO", logger="session_control")
    session = _claude_session(app_config)
    service = SessionActionService(app_config)
    store = service.memory_store
    condenser = OllamaCondenser("http://127.0.0.1:1", "tiny")
    with mock.patch.object(condenser, "_generate", side_effect=OSError("down")):
        store.condense(session, condenser, reason="manual")
    service.delete(session.public_id)
    text = caplog.text
    assert "condenser fell back to extraction" in text and "down" in text
    assert f"removed session claude-{session.public_id} reason=delete" in text


def _aged_note(store, name, workspace, updated_at, summary="Goal - ship it"):
    meta = {
        "provider": "claude",
        "session_id": name,
        "title": f"Task {name}",
        "repo": workspace.rsplit("/", 1)[-1],
        "workspace": workspace,
        "updated_at": updated_at,
        "condensed_at": "2026-11-13T00:00:00+00:00",
        "messages": 3,
    }
    body = f"# Task {name}\n\n## Summary\n\n### Session Notes\n\n{summary}\n\n## Files mentioned\n\n- `src/a.py`\n"
    store._write_private(store.entries_dir / f"claude-{name}.md", memory._render(meta, body))


def test_merge_stale_folds_old_notes_into_a_rollup_and_archives_originals(app_config):
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    for i in range(5):
        _aged_note(store, f"old{i}", "/w/proj", f"2026-01-0{i + 1}T00:00:00Z")
    _aged_note(store, "fresh", "/w/proj", "2999-01-01T00:00:00Z")
    _aged_note(store, "other", "/w/lonely", "2026-01-01T00:00:00Z")

    dry = store.merge_stale(older_than=timedelta(days=30), min_notes=5, dry_run=True)
    assert len(dry) == 1 and len(dry[0].merged) == 5
    assert len(store.entries()) == 7  # a dry run changes nothing

    [result] = store.merge_stale(older_than=timedelta(days=30), min_notes=5)
    ids = {e.entry_id for e in store.entries()}
    assert ids == {"claude-fresh", "claude-other", result.rollup_id}
    assert store.merged_ids() == {f"claude-old{i}" for i in range(5)}
    rollup = store.get(result.rollup_id)
    assert rollup.meta["provider"] == "rollup" and rollup.meta["updated_at"] == "2026-01-05"
    assert "Task old4" in rollup.body and "Goal - ship it" in rollup.body
    assert "Session Notes" not in rollup.body and "`src/a.py`" in rollup.body
    assert stat.S_IMODE(store.archive_dir.stat().st_mode) == 0o700
    assert (store.archive_dir / "claude-old0.md").is_file()
    assert store.search("ship")  # rollups are searchable
    assert store.merge_stale(older_than=timedelta(days=30), min_notes=5) == []


def test_merge_stale_adds_newly_stale_notes_to_the_existing_rollup(app_config):
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    for i in range(3):
        _aged_note(store, f"a{i}", "/w/proj", "2026-01-01T00:00:00Z")
    [first] = store.merge_stale(older_than=timedelta(days=30), min_notes=3)
    for i in range(3):
        _aged_note(store, f"b{i}", "/w/proj", "2026-02-01T00:00:00Z")
    [second] = store.merge_stale(older_than=timedelta(days=30), min_notes=3)
    assert second.rollup_id == first.rollup_id
    rollup = store.get(first.rollup_id)
    assert len(rollup.meta["items"]) == 6 and "6 earlier sessions" in rollup.title
    assert rollup.meta["updated_at"] == "2026-02-01"


def test_merge_stale_redacts_and_caps_items(app_config):
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    _aged_note(store, "s", "/w/p", "2026-01-01T00:00:00Z", summary=f"token {SECRET} leaked")
    store.merge_stale(older_than=timedelta(days=1), min_notes=1)
    text = next(store.entries_dir.glob("rollup-*.md")).read_text()
    assert SECRET not in text
    with mock.patch.object(memory, "MAX_ROLLUP_ITEMS", 2):
        for i in range(4):
            _aged_note(store, f"c{i}", "/w/p", f"2026-02-0{i + 1}T00:00:00Z")
        store.merge_stale(older_than=timedelta(days=1), min_notes=1)
    rollup = next(e for e in store.entries() if e.meta["provider"] == "rollup")
    assert len(rollup.meta["items"]) == 2 and "omitted" in rollup.body


def test_backfill_does_not_resurrect_merged_notes(app_config):
    session = _claude_session(app_config)
    service = SessionActionService(app_config)
    service.condense_all()
    store = service.memory_store
    entry_id = f"claude-{session.public_id}"
    store.merge_stale(
        older_than=timedelta(seconds=0),
        min_notes=1,
        now=datetime.now(timezone.utc) + timedelta(days=9999),
    )
    assert entry_id in store.merged_ids() and entry_id not in store.ids()
    assert service.condense_all(refresh=True)[0] == []
    assert entry_id not in store.ids()


def test_cli_memory_merge(app_config, capsys):
    store = MemoryStore(app_config.trash_dir.parent / "memory")
    for i in range(2):
        _aged_note(store, f"x{i}", "/w/proj", "2026-01-01T00:00:00Z")
    with mock.patch.object(cli.AppConfig, "from_env", return_value=app_config):
        assert cli.main(["memory", "merge", "--min-notes", "2", "--dry-run"]) == 0
        assert "Would merge 2 note(s) in /w/proj" in capsys.readouterr().out
        assert cli.main(["memory", "merge", "--min-notes", "2"]) == 0
        assert "Merged 2" in capsys.readouterr().out
        assert cli.main(["memory", "merge", "--min-notes", "2"]) == 0
        assert "Nothing to merge" in capsys.readouterr().out
