"""The scanner re-parses only transcript files that changed since the previous scan."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest import mock

import pytest

from session_control import scanner as scanner_module
from session_control.scanner import SessionScanner
from tests.helpers import seed_claude, seed_codex, seed_copilot, write_jsonl


@pytest.fixture
def counted(monkeypatch):
    calls = {"codex": 0, "claude": 0, "copilot": 0}

    for name, attribute in (
        ("codex", "_codex_record"),
        ("claude", "_claude_record"),
        ("copilot", "_copilot_record"),
    ):
        original = getattr(scanner_module, attribute)

        def wrapper(*args, _original=original, _name=name, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(scanner_module, attribute, wrapper)
    return calls


@pytest.fixture
def scanner(app_config):
    seed_codex(app_config.codex_root)
    seed_claude(app_config.claude_root)
    seed_copilot(app_config.copilot_root)
    return SessionScanner(replace(app_config, scan_cache_ttl_seconds=0))


def test_unchanged_files_are_not_parsed_again(scanner, counted):
    first = scanner.scan()
    second = scanner.scan()
    assert counted == {"codex": 1, "claude": 1, "copilot": 1}
    assert [s.public_id for s in first.sessions] == [s.public_id for s in second.sessions]
    assert not second.errors


def test_a_changed_transcript_is_parsed_again_and_the_others_are_not(scanner, counted, app_config):
    scanner.scan()
    path = next((app_config.claude_root / "projects").rglob("*.jsonl"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-04-02T11:00:00Z",
                    "sessionId": "c7df09f0-b9f2-4563-853e-f64fd095128f",
                    "message": {"role": "user", "content": "one more"},
                }
            )
            + "\n"
        )
    scanner.scan()
    assert counted == {"codex": 1, "claude": 2, "copilot": 1}


def test_a_codex_index_change_refreshes_the_title(scanner, counted, app_config):
    before = next(s for s in scanner.scan().sessions if s.provider == "codex")
    write_jsonl(
        app_config.codex_root / "session_index.jsonl",
        [
            {
                "id": "019d016b-30c2-7992-970a-b6082c1a2723",
                "thread_name": "Renamed thread",
                "updated_at": "2026-04-01T12:30:00Z",
            }
        ],
    )
    after = next(s for s in scanner.scan().sessions if s.provider == "codex")
    assert before.title != after.title == "Renamed thread"
    assert counted["codex"] == 2


def test_a_copilot_workspace_change_is_picked_up(scanner, counted, app_config):
    scanner.scan()
    workspace = next((app_config.copilot_root / "session-state").glob("*/workspace.yaml"))
    workspace.write_text(
        workspace.read_text(encoding="utf-8") + "summary: changed\n", encoding="utf-8"
    )
    scanner.scan()
    assert counted["copilot"] == 2


def test_deleted_transcripts_leave_the_results_and_the_cache(scanner, app_config):
    assert any(s.provider == "claude" for s in scanner.scan().sessions)
    next((app_config.claude_root / "projects").rglob("*.jsonl")).unlink()
    report = scanner.scan()
    assert not any(s.provider == "claude" for s in report.sessions)
    assert not [key for key in scanner._file_cache if key[0] == "claude"]


def test_a_malformed_file_keeps_reporting_its_error_from_the_cache(app_config, counted):
    seed_codex(app_config.codex_root)
    bad = app_config.codex_root / "sessions" / "2026" / "04" / "01" / "rollout-bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    cached = SessionScanner(replace(app_config, scan_cache_ttl_seconds=0))
    first = cached.scan(providers=("codex",))
    second = cached.scan(providers=("codex",))
    assert first.errors and [e.message for e in first.errors] == [e.message for e in second.errors]


def test_warm_up_fills_the_file_cache_so_the_first_scan_parses_nothing(scanner, counted):
    scanner.warm_in_background().join(timeout=30)
    assert counted == {"codex": 1, "claude": 1, "copilot": 1}
    scanner.scan()
    assert counted == {"codex": 1, "claude": 1, "copilot": 1}


def test_a_failed_warm_up_is_logged_and_never_raises(scanner, caplog):
    with (
        mock.patch.object(scanner, "scan", side_effect=RuntimeError("boom")),
        caplog.at_level("WARNING"),
    ):
        scanner.warm_in_background().join(timeout=10)
    assert "scan warm-up failed" in caplog.text


def test_concurrent_scans_share_one_scan(app_config):
    import threading
    import time

    seed_claude(app_config.claude_root)
    shared = SessionScanner(replace(app_config, scan_cache_ttl_seconds=30))
    calls = []
    original = shared._do_scan

    def slow_scan(providers=None):
        calls.append(providers)
        time.sleep(0.3)
        return original(providers)

    shared._do_scan = slow_scan
    reports = []
    threads = [threading.Thread(target=lambda: reports.append(shared.scan())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(calls) == 1
    assert len(reports) == 4 and all(report is reports[0] for report in reports)


def test_allow_stale_serves_the_old_report_and_refreshes_in_the_background(monkeypatch):
    from types import SimpleNamespace

    config = SimpleNamespace(scan_cache_ttl_seconds=5.0, scan_max_stale_seconds=60.0)
    scanner = SessionScanner(config)
    reports = iter(["first", "second"])
    monkeypatch.setattr(scanner, "_do_scan", lambda providers: next(reports))

    class Inline:
        def __init__(self, target, args=(), daemon=None):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    monkeypatch.setattr(scanner_module.threading, "Thread", Inline)

    assert scanner.scan() == "first"
    stamp, report = scanner._cache[None]
    scanner._cache[None] = (stamp - 10, report)  # past the TTL, inside the stale window
    assert scanner.scan(allow_stale=True) == "first"  # no waiting
    assert scanner.scan(allow_stale=True) == "second"  # the refresh landed

    scanner._cache[None] = (scanner._cache[None][0] - 120, "second")  # too old to serve
    monkeypatch.setattr(scanner, "_do_scan", lambda providers: "third")
    assert scanner.scan(allow_stale=True) == "third"


def test_a_plain_scan_never_serves_a_stale_report(monkeypatch):
    from types import SimpleNamespace

    scanner = SessionScanner(
        SimpleNamespace(scan_cache_ttl_seconds=5.0, scan_max_stale_seconds=60.0)
    )
    answers = iter(["old", "new"])
    monkeypatch.setattr(scanner, "_do_scan", lambda providers: next(answers))
    assert scanner.scan() == "old"
    stamp, report = scanner._cache[None]
    scanner._cache[None] = (stamp - 10, report)
    assert scanner.scan() == "new"
