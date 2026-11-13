"""Opening sessions in tmux, closing their windows on delete, and index maintenance.

``tmux`` is the user's real terminal multiplexer, so a fake replaces ``subprocess.run`` in
the actions module for every test here: nothing starts, selects or kills a window. What
is pinned is the failure handling (a refused command is an error naming tmux's reason,
never silent), the bulk behaviour (one bad session does not stop the rest), and that
only the windows tagged for the session being deleted are closed.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from session_control import actions
from session_control.actions import (
    SessionActionError,
    SessionActionService,
    _command,
    _kill_tmux_windows_for_session,
    _remove_codex_index_entry,
    _remove_continue_index_entry,
    _scope_prefix,
)
from session_control.scanner import SessionScanner
from tests.helpers import seed_codex, seed_continue


class FakeTmux:
    """Scripted ``tmux``: each subcommand answers (returncode, stdout, stderr)."""

    def __init__(self, **answers):
        self.calls: list[list[str]] = []
        self.answers = {
            "has-session": (0, "", ""),
            "new-session": (0, "", ""),
            "new-window": (0, "@7\t7\n", ""),
            "set-option": (0, "", ""),
            "select-window": (0, "", ""),
            "list-windows": (0, "", ""),
            "kill-window": (0, "", ""),
        }
        for name, value in answers.items():
            self.answers[name.replace("_", "-")] = value

    def __call__(self, args, capture_output=False, **kwargs):
        self.calls.append(list(args))
        assert args[0] == "tmux", f"only tmux may be run here, not {args}"
        answer = self.answers[args[1]]
        if isinstance(answer, Exception):
            raise answer
        code, out, err = answer
        return subprocess.CompletedProcess(args, code, stdout=out.encode(), stderr=err.encode())

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]


@pytest.fixture
def tmux(monkeypatch):
    def install(**answers) -> FakeTmux:
        fake = FakeTmux(**answers)
        monkeypatch.setattr(actions.subprocess, "run", fake)
        return fake

    return install


@pytest.fixture
def service_and_session(app_config):
    seed_continue(app_config.continue_root)
    scanner = SessionScanner(app_config)
    session = scanner.scan(providers=("continue",)).sessions[0]
    return SessionActionService(app_config, scanner), session


# --------------------------------------------------------------------------- open one


def test_a_missing_tmux_session_is_created_before_the_window(service_and_session, tmux):
    service, session = service_and_session
    fake = tmux(has_session=(1, "", "no server"))
    result = service.open_in_webterm(session.public_id)
    assert fake.verbs()[:3] == ["has-session", "new-session", "new-window"]
    assert fake.calls[1] == ["tmux", "new-session", "-d", "-s", service.config.tmux_session]
    assert result.window_index == 7


def test_a_tmux_session_that_cannot_be_created_is_an_error_with_tmuxs_reason(
    service_and_session, tmux
):
    service, session = service_and_session
    fake = tmux(has_session=(1, "", ""), new_session=(1, "", "server exited unexpectedly"))
    with pytest.raises(SessionActionError, match="Could not create tmux session: server exited"):
        service.open_in_webterm(session.public_id)
    assert "new-window" not in fake.verbs()


def test_a_window_tmux_refuses_is_an_error_and_is_never_tagged_or_selected(
    service_and_session, tmux
):
    service, session = service_and_session
    fake = tmux(new_window=(1, "", "no space for new pane"))
    with pytest.raises(SessionActionError, match="Could not open tmux window: no space"):
        service.open_in_webterm(session.public_id)
    assert "set-option" not in fake.verbs() and "select-window" not in fake.verbs()


def test_a_window_that_cannot_be_selected_is_an_error(service_and_session, tmux):
    service, session = service_and_session
    tmux(select_window=(1, "", "can't find window"))
    with pytest.raises(SessionActionError, match="Could not select tmux window: can't find"):
        service.open_in_webterm(session.public_id)


def test_a_window_that_cannot_be_tagged_is_an_error_and_names_the_failure(
    service_and_session, tmux
):
    service, session = service_and_session
    fake = tmux(set_option=(1, "", "bad option"))
    with pytest.raises(SessionActionError, match="Could not tag tmux window: bad option"):
        service.open_in_webterm(session.public_id)
    assert "select-window" not in fake.verbs()


def test_every_tag_is_written_to_the_new_window(service_and_session, tmux):
    service, session = service_and_session
    fake = tmux()
    service.open_in_webterm(session.public_id)
    tags = {c[5]: c[6] for c in fake.calls if c[1] == "set-option"}
    assert tags == {
        "@session_control_public_id": session.public_id,
        "@session_control_provider": session.provider,
        "@session_control_session_id": session.session_id,
    }
    assert all(
        c[:5] == ["tmux", "set-option", "-w", "-t", "@7"]
        for c in fake.calls
        if c[1] == "set-option"
    )


@pytest.mark.parametrize(
    ("stdout", "index"),
    [
        ("@7\t7\n", 7),
        ("@7\tnot-a-number\n", None),
        ("@7\n", None),
        ("", None),
        ("noise\n@9\t3\n", 3),
    ],
)
def test_the_window_index_is_read_from_the_last_line_when_there_is_one(
    service_and_session, tmux, stdout, index
):
    service, session = service_and_session
    tmux(new_window=(0, stdout, ""))
    assert service.open_in_webterm(session.public_id).window_index == index


def test_the_window_is_named_after_the_title_cut_to_twenty_characters(app_config, tmux):
    seed_codex(app_config.codex_root)
    scanner = SessionScanner(app_config)
    session = scanner.scan(providers=("codex",)).sessions[0]
    fake = tmux()
    SessionActionService(app_config, scanner).open_in_webterm(session.public_id)
    new_window = next(c for c in fake.calls if c[1] == "new-window")
    assert new_window[new_window.index("-n") + 1] == session.title[:20].strip()


def test_an_unknown_session_is_not_found_and_tmux_is_untouched(service_and_session, tmux):
    service, _ = service_and_session
    fake = tmux()
    with pytest.raises(SessionActionError, match="Session was not found"):
        service.open_in_webterm("0" * 20)
    assert fake.calls == []


# --------------------------------------------------------------------------- open many


def test_bulk_open_opens_each_once_and_reports_the_unknown_ones(service_and_session, tmux):
    service, session = service_and_session
    fake = tmux()
    result = service.open_many_in_webterm((session.public_id, session.public_id, "f" * 20))
    assert [o.session.public_id for o in result.opened] == [session.public_id]
    assert result.errors == (f"{'f' * 20}: Session was not found. Refresh and try again.",)
    assert fake.verbs().count("new-window") == 1


def test_one_failing_window_does_not_stop_the_others(app_config, tmux):
    seed_continue(app_config.continue_root)
    seed_continue(app_config.continue_root, session_id="11111111-495f-432d-8d16-31aa4e7ac7ea")
    scanner = SessionScanner(app_config)
    sessions = scanner.scan(providers=("continue",)).sessions
    ids = tuple(s.public_id for s in sessions)
    answers = iter([(1, "", "boom"), (0, "@8\t8\n", "")])
    fake = tmux()
    original = fake.answers

    class Sequenced(dict):
        def __getitem__(self, key):
            return next(answers) if key == "new-window" else original[key]

    fake.answers = Sequenced()
    result = SessionActionService(app_config, scanner).open_many_in_webterm(ids)
    assert len(result.opened) == 1 and result.opened[0].window_index == 8
    assert len(result.errors) == 1 and "Could not open tmux window: boom" in result.errors[0]


def test_bulk_open_reports_a_select_or_tag_failure_per_session(service_and_session, tmux):
    service, session = service_and_session
    tmux(select_window=(1, "", "gone"))
    result = service.open_many_in_webterm((session.public_id,))
    assert result.opened == ()
    assert result.errors == (f"{session.public_id}: Could not select tmux window: gone",)


def test_bulk_open_with_a_dead_tmux_server_reports_it_for_the_session(service_and_session, tmux):
    service, session = service_and_session
    tmux(has_session=(1, "", ""), new_session=(1, "", "no tmux"))
    result = service.open_many_in_webterm((session.public_id,))
    assert "Could not create tmux session: no tmux" in result.errors[0]


def test_bulk_open_with_no_ids_does_nothing(service_and_session, tmux):
    service, _ = service_and_session
    fake = tmux()
    result = service.open_many_in_webterm(())
    assert (result.opened, result.errors, fake.calls) == ((), (), [])


# --------------------------------------------------------------------------- closing windows


def listing(*rows: tuple[str, str, str, str]) -> str:
    return "\n".join("\t".join(row) for row in rows) + "\n"


def test_no_tmux_binary_or_no_session_closes_nothing(service_and_session, tmux):
    service, session = service_and_session
    tmux(has_session=FileNotFoundError("tmux"))
    assert _kill_tmux_windows_for_session("main", session) == 0
    fake = tmux(has_session=(1, "", "no server"))
    assert _kill_tmux_windows_for_session("main", session) == 0
    assert "list-windows" not in fake.verbs()


def test_only_the_windows_tagged_for_that_session_are_closed(service_and_session, tmux):
    _, session = service_and_session
    fake = tmux(
        list_windows=(
            0,
            listing(
                ("@1", session.public_id, session.provider, session.session_id),
                ("@2", "other-id", "continue", "someone-else"),
                ("@3", "", session.provider, session.session_id),
                ("@4", "", "codex", session.session_id),
                ("@5", "", "", ""),
                ("@6",),
            ),
            "",
        )
    )
    assert _kill_tmux_windows_for_session("main", session) == 2
    killed = [c[3] for c in fake.calls if c[1] == "kill-window"]
    assert killed == ["@1", "@3"]


def test_a_window_that_cannot_be_inspected_or_closed_is_an_error(service_and_session, tmux):
    _, session = service_and_session
    tmux(list_windows=(1, "", "no such session"))
    with pytest.raises(SessionActionError, match="Could not inspect tmux windows: no such"):
        _kill_tmux_windows_for_session("main", session)
    tmux(
        list_windows=(
            0,
            listing(("@1", session.public_id, session.provider, session.session_id)),
            "",
        ),
        kill_window=(1, "", "window busy"),
    )
    with pytest.raises(SessionActionError, match="Could not close tmux window @1: window busy"):
        _kill_tmux_windows_for_session("main", session)


def test_deleting_a_session_closes_its_windows_and_moves_its_files(
    service_and_session, tmux, app_config
):
    service, session = service_and_session
    fake = tmux(
        list_windows=(
            0,
            listing(("@1", session.public_id, session.provider, session.session_id)),
            "",
        )
    )
    result = service.delete(session.public_id)
    assert result.killed_window_count == 1
    assert not session.primary_path.exists()
    assert any(c[1] == "kill-window" for c in fake.calls)
    assert any(app_config.trash_dir.rglob(session.primary_path.name))


# --------------------------------------------------------------------------- shell command


def test_the_launch_command_quotes_the_workspace_and_may_omit_it():
    assert _command("/my dir/it's", ["codex", "resume", "a b"]) == (
        "cd '/my dir/it'\"'\"'s' && codex resume 'a b'"
    )
    assert _command("", ["codex", "resume", "x"]) == "codex resume x"


# --------------------------------------------------------------------------- index entries


def test_a_codex_index_entry_is_removed_and_unparseable_lines_are_kept(tmp_path):
    index = tmp_path / "session_index.jsonl"
    index.write_text(
        '{"id": "keep"}\nnot json\n{"id": "drop"}\n{"id": "also-keep"}\n', encoding="utf-8"
    )
    _remove_codex_index_entry(index, "drop")
    assert index.read_text(encoding="utf-8").splitlines() == [
        '{"id": "keep"}',
        "not json",
        '{"id": "also-keep"}',
    ]


def test_a_missing_codex_index_is_left_missing(tmp_path):
    _remove_codex_index_entry(tmp_path / "absent.jsonl", "x")
    assert not (tmp_path / "absent.jsonl").exists()


def test_an_unwritable_codex_index_is_an_error(tmp_path, monkeypatch):
    index = tmp_path / "session_index.jsonl"
    index.write_text('{"id": "a"}\n', encoding="utf-8")
    monkeypatch.setattr(
        type(index), "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
    )
    with pytest.raises(SessionActionError, match="Could not update Codex session index"):
        _remove_codex_index_entry(index, "a")


def test_a_continue_index_entry_is_removed_and_other_shapes_survive(tmp_path):
    index = tmp_path / "sessions.json"
    index.write_text(
        json.dumps([{"sessionId": "keep"}, "odd", {"sessionId": "drop"}]), encoding="utf-8"
    )
    _remove_continue_index_entry(index, "drop")
    assert json.loads(index.read_text()) == [{"sessionId": "keep"}, "odd"]


def test_a_continue_index_that_is_not_a_list_is_left_alone(tmp_path):
    index = tmp_path / "sessions.json"
    index.write_text('{"not": "a list"}', encoding="utf-8")
    _remove_continue_index_entry(index, "x")
    assert json.loads(index.read_text()) == {"not": "a list"}


def test_a_missing_continue_index_is_ignored_and_a_corrupt_one_is_an_error(tmp_path):
    _remove_continue_index_entry(tmp_path / "absent.json", "x")
    corrupt = tmp_path / "sessions.json"
    corrupt.write_text("{broken", encoding="utf-8")
    with pytest.raises(SessionActionError, match="Could not read Continue session index"):
        _remove_continue_index_entry(corrupt, "x")
    assert corrupt.read_text() == "{broken"


# ------------------------------------------------------------ tmux server scope


def test_a_new_tmux_server_is_started_in_its_own_systemd_scope(monkeypatch, app_config):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(actions.shutil, "which", lambda name: "/usr/bin/systemd-run")
    assert _scope_prefix()[:3] == ["systemd-run", "--user", "--scope"]

    calls: list[list[str]] = []

    def run(args, capture_output=False, **kwargs):
        calls.append(list(args))
        code = 1 if args[:2] == ["tmux", "has-session"] else 0
        return subprocess.CompletedProcess(args, code, stdout=b"", stderr=b"")

    monkeypatch.setattr(actions.subprocess, "run", run)
    monkeypatch.setattr(actions, "_scope_prefix", _scope_prefix)
    SessionActionService(app_config)._ensure_tmux_session()
    assert calls[1][:3] == ["systemd-run", "--user", "--scope"]
    assert calls[1][-5:] == ["tmux", "new-session", "-d", "-s", app_config.tmux_session]


def test_scope_is_skipped_without_a_user_manager(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert _scope_prefix() == []
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(actions.shutil, "which", lambda name: None)
    assert _scope_prefix() == []


def test_a_failed_scope_falls_back_to_plain_tmux(monkeypatch, app_config):
    calls: list[list[str]] = []

    def run(args, capture_output=False, **kwargs):
        calls.append(list(args))
        failed = args[0] == "systemd-run" or args[:2] == ["tmux", "has-session"]
        return subprocess.CompletedProcess(args, 1 if failed else 0, stdout=b"", stderr=b"x")

    monkeypatch.setattr(actions.subprocess, "run", run)
    monkeypatch.setattr(actions, "_scope_prefix", lambda: ["systemd-run", "--user", "--scope"])
    SessionActionService(app_config)._ensure_tmux_session()
    assert calls[-1] == ["tmux", "new-session", "-d", "-s", app_config.tmux_session]
