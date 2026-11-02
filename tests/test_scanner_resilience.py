"""The scanner reads files written by other programs; none of them may break a scan.

A truncated, unrecognised or hostile session file must become an error line (or be
skipped), never an exception that hides every other session. And an in-use session
must read as in use: that lock check is what stops prune and delete from tearing down
a session someone is working in, so ``os.kill`` is replaced here and no signal is ever
sent to a real process.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import pytest

from session_control import scanner as sc
from session_control.scanner import SessionScanner
from tests.helpers import (
    seed_claude,
    seed_codex,
    seed_continue,
    seed_copilot,
    write_json,
    write_jsonl,
)

# --------------------------------------------------------------------------- scanning


def test_results_are_cached_for_the_ttl_and_can_be_invalidated(app_config):
    seed_continue(app_config.continue_root)
    config = type(app_config)(**{**app_config.__dict__, "scan_cache_ttl_seconds": 60.0})
    scanner = SessionScanner(config)
    with mock.patch.object(scanner, "_do_scan", wraps=scanner._do_scan) as scan:
        first = scanner.scan(("continue",))
        again = scanner.scan(("continue",))
        scanner.scan(("codex",))
        assert again is first and scan.call_count == 2
        scanner.invalidate()
        scanner.scan(("continue",))
        assert scan.call_count == 3


def test_a_zero_ttl_always_rescans(app_config):
    scanner = SessionScanner(
        type(app_config)(**{**app_config.__dict__, "scan_cache_ttl_seconds": 0.0})
    )
    with mock.patch.object(scanner, "_do_scan", wraps=scanner._do_scan) as scan:
        scanner.scan()
        scanner.scan()
    assert scan.call_count == 2


def test_an_unknown_provider_is_an_error_and_the_rest_still_scan(app_config):
    seed_continue(app_config.continue_root)
    report = SessionScanner(app_config).scan(("nonsense", "continue"))
    assert [e.message for e in report.errors] == ["Unknown provider."]
    assert len(report.sessions) == 1


@pytest.mark.parametrize("failure", [OSError("disk gone"), ValueError("bad value")])
def test_a_provider_that_raises_becomes_an_error_not_a_crash(app_config, failure):
    seed_continue(app_config.continue_root)
    scanner = SessionScanner(app_config)
    with mock.patch.object(scanner, "_scan_codex", side_effect=failure):
        report = scanner.scan()
    assert any(e.provider == "codex" and str(failure) in e.message for e in report.errors)
    assert len(report.sessions) == 1


def test_sessions_are_listed_newest_first(app_config):
    seed_codex(app_config.codex_root, last_event_time="2026-04-01T12:05:00Z")
    seed_continue(app_config.continue_root)
    sessions = SessionScanner(app_config).scan().sessions
    stamps = [s.updated_at or s.created_at for s in sessions]
    assert stamps == sorted(stamps, reverse=True)


def test_a_codex_file_in_an_unrecognised_format_is_reported_and_skipped(app_config):
    seed_codex(app_config.codex_root)
    other = "019d016b-30c2-7992-970a-b6082c1a9999"
    bad = app_config.codex_root / "sessions" / "2026" / "04" / "01" / f"rollout-{other}.jsonl"
    bad.write_text('{"unrelated": true}\nnot json\n', encoding="utf-8")
    report = SessionScanner(app_config).scan(("codex",))
    assert len(report.sessions) == 1
    assert any("rollout-" in e.message and "Unsupported" in e.message for e in report.errors)


def test_a_claude_file_in_an_unrecognised_format_is_reported_and_skipped(app_config):
    seed_claude(app_config.claude_root)
    project = next((app_config.claude_root / "projects").iterdir())
    (project / "11111111-1111-4111-8111-111111111111.jsonl").write_text(
        "[1, 2]\n", encoding="utf-8"
    )
    report = SessionScanner(app_config).scan(("claude",))
    assert len(report.sessions) == 1
    assert any(e.provider == "claude" for e in report.errors)


def test_a_copilot_directory_without_a_workspace_file_is_reported(app_config):
    seed_copilot(app_config.copilot_root)
    stray = app_config.copilot_root / "session-state" / "stray-dir"
    stray.mkdir(parents=True)
    report = SessionScanner(app_config).scan(("copilot",))
    assert len(report.sessions) == 1
    assert any(
        "Unsupported Copilot session schema in stray-dir" in e.message for e in report.errors
    )


def test_a_copilot_event_log_in_an_unrecognised_format_is_reported(app_config):
    seed_copilot(app_config.copilot_root)
    session_dir = next((app_config.copilot_root / "session-state").iterdir())
    (session_dir / "events.jsonl").write_text('{"nothing": "useful"}\n', encoding="utf-8")
    report = SessionScanner(app_config).scan(("copilot",))
    assert report.sessions == ()
    assert any("events" in e.message.lower() or "Unsupported" in e.message for e in report.errors)


# --------------------------------------------------------------------------- readers


def test_jsonl_reading_skips_bad_lines_and_survives_a_missing_file(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text('{"a": 1}\n\nnot json\n{"b": 2}\n', encoding="utf-8")
    assert list(sc._iter_jsonl(path)) == [{"a": 1}, {"b": 2}]
    assert list(sc._iter_jsonl(tmp_path / "absent.jsonl")) == []


def test_json_reading_returns_none_for_missing_or_corrupt_files(tmp_path):
    good = tmp_path / "g.json"
    write_json(good, {"a": 1})
    assert sc._read_json(good) == {"a": 1}
    assert sc._read_json(tmp_path / "absent.json") is None
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    assert sc._read_json(tmp_path / "bad.json") is None


def test_simple_yaml_keeps_only_flat_key_value_lines(tmp_path):
    path = tmp_path / "w.yaml"
    path.write_text(
        "# comment\n\nid: \"abc\"\ncwd: '/w/app'\nno colon here\nname: a: b\n", encoding="utf-8"
    )
    assert sc._read_simple_yaml(path) == {"id": "abc", "cwd": "/w/app", "name": "a: b"}
    assert sc._read_simple_yaml(tmp_path / "absent.yaml") == {}


def test_a_probe_distinguishes_valid_malformed_and_unreadable_files(tmp_path):
    def probe(path):
        return sc._probe_jsonl(
            path, description="session", supported=lambda i: "ok" in i, expectation="ok"
        )

    valid = tmp_path / "v.jsonl"
    valid.write_text('\n{"ok": 1}\n', encoding="utf-8")
    assert probe(valid) is None
    mixed = tmp_path / "m.jsonl"
    mixed.write_text('not json\n{"ok": 1}\n', encoding="utf-8")
    assert probe(mixed) is None
    malformed = tmp_path / "x.jsonl"
    malformed.write_text("not json\n", encoding="utf-8")
    assert "invalid JSONL" in probe(malformed)
    wrong = tmp_path / "w.jsonl"
    wrong.write_text('{"other": 1}\n', encoding="utf-8")
    assert "expected ok" in probe(wrong)
    assert "Could not read session" in probe(tmp_path / "absent.jsonl")


def test_the_codex_index_is_read_by_id_and_tolerates_junk(tmp_path):
    index = tmp_path / "session_index.jsonl"
    write_jsonl(index, [{"id": "a", "thread_name": "A"}, {"id": ""}, {"no": "id"}])
    index.write_text(index.read_text() + "garbage\n", encoding="utf-8")
    assert set(sc._read_codex_index(index)) == {"a"}
    assert sc._read_codex_index(tmp_path / "absent") == {}


def test_the_crew_chief_registry_is_optional_and_tolerates_junk(tmp_path):
    assert sc._read_crew_chief_registry(None) == {}
    assert sc._read_crew_chief_registry(tmp_path / "absent.jsonl") == {}
    registry = tmp_path / "r.jsonl"
    registry.write_text(
        'not json\n[1]\n"text"\n42\nnull\n{"session_id": "s1", "model": "m"}\n', encoding="utf-8"
    )
    assert sc._read_crew_chief_registry(registry) == {
        "s1": {"backend": "ollama", "base_url": "", "model": "m"}
    }


# --------------------------------------------------------------------------- text and values


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        ("plain", "plain"),
        ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "a\nb"),
        ({"text": "t"}, "t"),
        ([], ""),
        ([{"type": "image"}], ""),
        (5, ""),
    ],
)
def test_extracted_text(value, text):
    assert sc._extract_text(value).strip() == text


def test_a_stray_non_object_line_does_not_abort_a_scan(app_config):
    seed_codex(app_config.codex_root)
    rollout = next(app_config.codex_root.rglob("rollout-*.jsonl"))
    rollout.write_text(
        '[1, 2]\n42\n"text"\n' + rollout.read_text(encoding="utf-8"), encoding="utf-8"
    )
    report = SessionScanner(app_config).scan(("codex",))
    assert len(report.sessions) == 1 and report.errors == ()


def test_an_injected_environment_block_is_not_taken_as_the_title():
    texts = ["<environment_context><cwd>/w</cwd></environment_context>", "Fix the downloader"]
    assert sc._best_user_text(texts) == "Fix the downloader"
    assert sc._best_user_text(["# AGENTS.md instructions for /w", "real"]) == "real"
    assert (
        sc._best_user_text(["<environment_context>only this</environment_context>"]) == "only this"
    )


def test_the_best_user_text_skips_bootstrap_text_but_falls_back_to_the_first():
    assert (
        sc._best_user_text(["<environment_context> x", "# AGENTS.md instructions", "real question"])
        == "real question"
    )
    assert sc._best_user_text(["<environment_context> x"]) == "x"
    assert sc._best_user_text([]) == ""


def test_titles_are_cleaned_stripped_of_markup_and_cut():
    assert sc._title_from_text("") == "" and sc._title_from_text("<b></b>") == ""
    # markup and leading markers go; the title is the cleaned text, cut to 72 characters
    assert sc._title_from_text("## > - Fix the <b>thing</b>") == "Fix the thing"
    long = sc._title_from_text("w" * 200)
    assert len(long) == 74 and long.endswith("...")  # 71 characters kept, then an ellipsis
    assert sc._truncate("short", 10) == "short"
    assert sc._truncate("abcdefghij", 5) == "abcd..."


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("garbage", ""),
        ("2026-04-01T12:00:00Z", "2026-04-01T12:00:00Z"),
        ("2026-04-01T12:00:00", "2026-04-01T12:00:00Z"),
        (" 2026-04-01T14:00:00+02:00 ", "2026-04-01T12:00:00Z"),
    ],
)
def test_datetimes_normalise_to_utc_or_blank(value, expected):
    assert sc._normalize_datetime(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1774494757505", "2026-03-26T03:12:37Z"), (None, ""), ("soon", ""), ("", "")],
)
def test_millisecond_timestamps(value, expected):
    assert (
        sc._millis_to_iso(value).startswith(expected[:10])
        if expected
        else sc._millis_to_iso(value) == ""
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "1970-01-01T00:00:00Z"),
        ("86400", "1970-01-02T00:00:00Z"),
        (None, ""),
        ("x", ""),
        (10**30, ""),
    ],
)
def test_epoch_seconds(value, expected):
    assert sc._epoch_to_iso(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("3", 3), (3.9, 3), (None, None), ("x", None), ([1], None)]
)
def test_int_or_none(value, expected):
    assert sc._int_or_none(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("2.5", 2.5), (3, 3.0), (None, None), ("x", None), ({}, None)]
)
def test_float_or_none(value, expected):
    assert sc._float_or_none(value) == expected


def test_metadata_helpers_return_empty_for_non_objects():
    for fn in (sc._rate_limits_metadata, sc._rate_limit_window, sc._int_dict):
        assert fn("x") == {} and fn(None) == {} and fn([1]) == {}
    assert sc._token_usage_metadata("x", "t") == {}


def test_a_file_uri_becomes_a_path_and_anything_else_is_left_alone():
    assert sc._uri_to_path("") == ""
    assert sc._uri_to_path("file:///home/me/my%20project") == "/home/me/my project"
    assert sc._uri_to_path("/plain/path") == "/plain/path"
    assert sc._uri_to_path("https://example.com/x%20y") == "https://example.com/x%20y"


def test_a_claude_workspace_is_recovered_from_its_project_folder_name(tmp_path):
    assert (
        sc._workspace_from_claude_project(tmp_path / "-home-me-app" / "s.jsonl") == "/home/me/app"
    )
    assert sc._workspace_from_claude_project(tmp_path / "plain" / "s.jsonl") == ""


def test_a_command_without_a_workspace_has_no_cd():
    assert sc._command("", ["a", "b c"]) == "a 'b c'"
    assert sc._command("/w x", ["a"]) == "cd '/w x' && a"


def test_uuids_are_found_in_file_names():
    assert (
        sc._uuid_from_text("rollout-019d016b-30c2-7992-970a-b6082c1a2723.jsonl")
        == "019d016b-30c2-7992-970a-b6082c1a2723"
    )
    assert sc._uuid_from_text("nothing here") == ""


def test_sizes_and_modification_times_handle_files_directories_and_the_missing(tmp_path):
    folder = tmp_path / "d"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_bytes(b"12345")
    (folder / "sub" / "b.txt").write_bytes(b"123")
    assert sc._path_size(folder) == 8
    assert sc._path_size(folder / "a.txt") == 5
    assert sc._path_size(tmp_path / "absent") == 0
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT[\d:.]+Z?", sc._mtime_iso(folder).replace("+00:00", "")
    ) or sc._mtime_iso(folder).endswith("Z")
    assert sc._mtime_iso(folder / "a.txt").endswith("Z")
    assert sc._mtime_iso(tmp_path / "absent") == ""


def test_checkpoint_and_markdown_titles(tmp_path):
    table = tmp_path / "index.md"
    table.write_text(
        "| # | Title |\n|---|---|\n| 1 | First checkpoint |\n| 2 | Second |\n", encoding="utf-8"
    )
    assert sc._first_checkpoint_title(table) == "First checkpoint"
    assert sc._first_checkpoint_title(tmp_path / "absent.md") == ""
    empty = tmp_path / "e.md"
    empty.write_text("no table\n", encoding="utf-8")
    assert sc._first_checkpoint_title(empty) == ""
    heading = tmp_path / "h.md"
    heading.write_text("intro\n## The Heading \n", encoding="utf-8")
    assert sc._first_markdown_heading(heading) == "The Heading"
    assert sc._first_markdown_heading(empty) == ""
    assert sc._first_markdown_heading(tmp_path / "absent.md") == ""


# --------------------------------------------------------------------------- liveness


@pytest.mark.parametrize("pid", [0, -1, -4242])
def test_a_non_positive_pid_is_never_a_live_process_and_no_signal_is_sent(pid):
    with mock.patch.object(sc.os, "kill") as kill:
        assert sc._process_exists(pid) is False
    kill.assert_not_called()


def test_signal_zero_is_used_so_nothing_is_delivered():
    with mock.patch.object(sc.os, "kill") as kill:
        assert sc._process_exists(4242) is True
    kill.assert_called_once_with(4242, 0)


@pytest.mark.parametrize(
    ("error", "live"),
    [(ProcessLookupError(), False), (PermissionError(), True), (OSError("odd"), False)],
)
def test_a_process_owned_by_someone_else_still_counts_as_live(error, live):
    with mock.patch.object(sc.os, "kill", side_effect=error):
        assert sc._process_exists(4242) is live


def test_a_session_directory_is_in_use_if_any_lock_names_a_live_process(tmp_path):
    (tmp_path / "inuse.111.lock").write_text("", encoding="utf-8")
    (tmp_path / "inuse.222.lock").write_text("", encoding="utf-8")
    (tmp_path / "inuse.notapid.lock").write_text("", encoding="utf-8")
    (tmp_path / "unrelated.lock").write_text("", encoding="utf-8")
    with mock.patch.object(sc, "_process_exists", side_effect=lambda pid: pid == 222) as exists:
        assert sc._has_live_lock(tmp_path) is True
    # the directory listing order is arbitrary, and the search stops at the first live lock
    assert 222 in [c.args[0] for c in exists.call_args_list]
    assert 111 not in [c.args[0] for c in exists.call_args_list] or exists.call_count == 2
    with mock.patch.object(sc, "_process_exists", return_value=False):
        assert sc._has_live_lock(tmp_path) is False


def test_a_directory_with_no_locks_is_not_in_use(tmp_path):
    with mock.patch.object(sc.os, "kill") as kill:
        assert sc._has_live_lock(tmp_path) is False
    kill.assert_not_called()


def test_a_live_lock_marks_a_copilot_session_active_end_to_end(app_config):
    seed_copilot(app_config.copilot_root)
    session_dir = next((app_config.copilot_root / "session-state").iterdir())
    (session_dir / "inuse.31337.lock").write_text("", encoding="utf-8")
    with mock.patch.object(sc.os, "kill") as kill:
        record = SessionScanner(app_config).scan(("copilot",)).sessions[0]
    kill.assert_called_once_with(31337, 0)
    assert record.active is True
    with mock.patch.object(sc.os, "kill", side_effect=ProcessLookupError()):
        assert SessionScanner(app_config).scan(("copilot",)).sessions[0].active is False


def test_a_bare_path_is_a_path_object_helper_sanity(tmp_path):
    assert isinstance(Path(tmp_path), Path)
