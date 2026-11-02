"""Each ``session-control`` command, driven through ``main()``.

This tool moves and deletes real AI session files, so nothing here may reach them:
the environment file is pointed at a path that does not exist, every provider root and
the trash live in ``tmp_path``, and the scanner, the action service and the web app are
replaced where the CLI imports them. The tests pin what the CLI decides: exit statuses,
what a dry run leaves alone, what it refuses (remote binds, unsafe token files, plain
HTTP) and what it prints.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from session_control import cli
from session_control.actions import (
    DeduplicateResult,
    DeleteResult,
    MergeResult,
    PruneError,
    PruneResult,
    PruneSkipped,
    RestoreCandidate,
    RestoreResult,
    SessionActionError,
)
from session_control.models import ScanError, ScanReport, SessionRecord

CUTOFF = datetime(2026, 5, 1, tzinfo=timezone.utc)


def record(provider="codex", session_id="0123456789abcdef", title="Fix the thing", **kwargs):
    base = {
        "workspace": "/w",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-02T10:00:00Z",
        "primary_path": Path("/sessions/x.jsonl"),
        "delete_targets": (),
        "size_bytes": 10,
        "message_count": 2,
        "preview": "",
        "resume_command": "resume",
    }
    return SessionRecord(provider, session_id, title, **{**base, **kwargs})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A configuration that cannot reach the real home directory or env file."""
    for name in list(__import__("os").environ):
        if name.startswith("SESSION_CONTROL_") or name == "CODEX_THREAD_ID":
            monkeypatch.delenv(name)
    monkeypatch.setenv("SESSION_CONTROL_ENV_FILE", str(tmp_path / "absent.env"))
    for provider in ("CODEX", "CLAUDE", "CONTINUE", "COPILOT"):
        monkeypatch.setenv(f"SESSION_CONTROL_{provider}_ROOT", str(tmp_path / provider.lower()))
    monkeypatch.setenv("SESSION_CONTROL_TRASH_DIR", str(tmp_path / "trash"))
    monkeypatch.setenv("SESSION_CONTROL_SECRET_KEY", "test-secret")
    return tmp_path


@pytest.fixture
def service(env):
    with mock.patch.object(cli, "SessionActionService") as cls:
        yield cls.return_value


@pytest.fixture
def scanner(env):
    with mock.patch.object(cli, "SessionScanner") as cls:
        yield cls.return_value


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        (" localhost ", True),
        ("LOCALHOST", True),
        ("127.5.5.5", True),
        ("0.0.0.0", False),
        ("192.168.1.2", False),
        ("example.com", False),
        ("", False),
        ("   ", False),
    ],
)
def test_loopback_hosts(host, loopback):
    assert cli._is_loopback_host(host) is loopback


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        (" YES ", True),
        ("on", True),
        ("0", False),
        ("", False),
        (None, False),
    ],
)
def test_truthy(value, expected):
    assert cli._truthy(value) is expected


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(days=180), "180d"),
        (timedelta(hours=5), "5h"),
        (timedelta(hours=36), "36h"),
        (timedelta(seconds=90), "90s"),
        (timedelta(days=2), "2d"),
    ],
)
def test_duration_days(delta, text):
    assert cli._duration_days(delta) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", timedelta(days=5)),
        (" 2 weeks ", timedelta(weeks=2)),
        ("3 Days", timedelta(days=3)),
        ("12hours", timedelta(hours=12)),
    ],
)
def test_parse_duration_forms(text, expected):
    assert cli._parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "-5d", "0d", "5 d d", "1.5d"])
def test_parse_duration_rejects_nonsense(text):
    with pytest.raises(cli.argparse.ArgumentTypeError):
        cli._parse_duration(text)


# --------------------------------------------------------------------------- env file


def test_env_file_values_are_loaded_unquoted_and_never_override_the_environment(
    tmp_path, monkeypatch
):
    path = tmp_path / "x.env"
    path.write_text(
        "# comment\n\nNOEQUALS\n=novalue\nCC_A=plain\nCC_B=\"quoted\"\nCC_C='single'\n"
        'CC_D = spaced \nCC_E="unbalanced\nCC_KEEP=from-file\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CC_KEEP", "from-env")
    for name in ("CC_A", "CC_B", "CC_C", "CC_D", "CC_E"):
        monkeypatch.delenv(name, raising=False)
    cli._load_env_file(path)
    import os

    assert os.environ["CC_A"] == "plain"
    assert os.environ["CC_B"] == "quoted"
    assert os.environ["CC_C"] == "single"
    assert os.environ["CC_D"] == "spaced"
    assert os.environ["CC_E"] == '"unbalanced'
    assert os.environ["CC_KEEP"] == "from-env"
    monkeypatch.undo()
    for name in ("CC_A", "CC_B", "CC_C", "CC_D", "CC_E", "CC_KEEP"):
        os.environ.pop(name, None)


def test_a_missing_env_file_is_fine(tmp_path):
    cli._load_env_file(tmp_path / "absent.env")


def test_the_env_file_can_be_chosen_by_variable(tmp_path, monkeypatch):
    path = tmp_path / "chosen.env"
    path.write_text("CC_CHOSEN=yes\n", encoding="utf-8")
    monkeypatch.setenv("SESSION_CONTROL_ENV_FILE", str(path))
    monkeypatch.delenv("CC_CHOSEN", raising=False)
    cli._load_default_env()
    import os

    assert os.environ["CC_CHOSEN"] == "yes"
    os.environ.pop("CC_CHOSEN")


def test_the_default_env_file_is_the_repo_local_one():
    path = cli._default_env_file()
    assert path.name == "session-control.env.local"
    assert path.parent.name == "config"


# --------------------------------------------------------------------------- web


def test_web_is_the_default_command_and_binds_to_loopback(env):
    with mock.patch.object(cli, "create_app") as create:
        assert cli.main([]) == 0
    create.return_value.run.assert_called_once_with(host="127.0.0.1", port=5420, debug=False)


def test_web_takes_host_port_and_debug_from_flags(env):
    with mock.patch.object(cli, "create_app") as create:
        cli.main(["web", "--host", "localhost", "--port", "6000", "--debug"])
    create.return_value.run.assert_called_once_with(host="localhost", port=6000, debug=True)


def test_web_defaults_come_from_the_environment(env, monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_PORT", "7001")
    with mock.patch.object(cli, "create_app") as create:
        cli.main(["web"])
    assert create.return_value.run.call_args.kwargs["port"] == 7001
    with mock.patch.object(cli, "create_app") as create:
        cli.main([])
    assert create.return_value.run.call_args.kwargs["port"] == 7001


def test_web_refuses_a_remote_bind_before_creating_the_app(env):
    with (
        mock.patch.object(cli, "create_app") as create,
        pytest.raises(SystemExit, match="non-loopback"),
    ):
        cli.main(["web", "--host", "0.0.0.0"])
    create.assert_not_called()


def test_web_allows_a_remote_bind_only_when_opted_in(env, monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_ALLOW_REMOTE", "yes")
    with mock.patch.object(cli, "create_app") as create:
        cli.main(["web", "--host", "0.0.0.0"])
    assert create.return_value.run.call_args.kwargs["host"] == "0.0.0.0"


# --------------------------------------------------------------------------- scan


def test_scan_prints_one_line_per_session_and_a_warning_per_error(scanner, capsys):
    scanner.scan.return_value = ScanReport(
        sessions=(record(title="Fix the thing"), record("claude", title="Other")),
        errors=(ScanError("copilot", "unreadable"),),
    )
    assert cli.main(["scan"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("codex") and "Fix the thing" in lines[0]
    assert lines[1].startswith("claude")
    assert lines[2] == "WARN copilot: unreadable"


def test_scan_json_and_provider_filter(scanner, capsys):
    scanner.scan.return_value = ScanReport(sessions=(record(),))
    cli.main(["scan", "--json", "--provider", "codex", "--provider", "claude"])
    assert scanner.scan.call_args.kwargs == {"providers": ("codex", "claude")}
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["session_id"] == "0123456789abcdef"
    cli.main(["scan"])
    assert scanner.scan.call_args.kwargs == {"providers": None}


# --------------------------------------------------------------------------- inventory


def test_inventory_prints_json_to_stdout_by_default(env, scanner, capsys):
    with mock.patch.object(cli, "build_inventory", return_value={"sessions": [], "node": "n"}):
        assert cli.main(["inventory"]) == 0
    assert json.loads(capsys.readouterr().out) == {"sessions": [], "node": "n"}


def test_inventory_written_to_a_file_is_owner_only_and_not_echoed(env, scanner, capsys):
    target = env / "private" / "inv.json"
    with mock.patch.object(cli, "build_inventory", return_value={"sessions": []}):
        cli.main(["inventory", "--output", str(target)])
    assert capsys.readouterr().out == ""
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert json.loads(target.read_text()) == {"sessions": []}


def test_inventory_json_flag_also_echoes_when_writing_a_file(env, scanner, capsys):
    target = env / "inv.json"
    with mock.patch.object(cli, "build_inventory", return_value={"sessions": []}):
        cli.main(["inventory", "--output", str(target), "--json"])
    assert json.loads(capsys.readouterr().out) == {"sessions": []}


def test_rewriting_an_inventory_file_keeps_it_owner_only(env):
    target = env / "inv.json"
    target.write_text("old contents that are longer than the new ones", encoding="utf-8")
    target.chmod(0o644)
    cli._write_owner_only(target, "new")
    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_inventory_carries_the_configured_node_identity(env, scanner, monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_NODE_ID", "workstation")
    with mock.patch.object(cli, "build_inventory", return_value={}) as build:
        cli.main(["inventory", "--provider", "codex"])
    assert scanner.scan.call_args.kwargs == {"providers": ("codex",)}
    assert "node_id" in build.call_args.kwargs and "node_label" in build.call_args.kwargs


# --------------------------------------------------------------------------- publish


def token_file(tmp_path, content="s3cret\n", mode=0o600):
    path = tmp_path / "token"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def publish_args(tmp_path, **overrides):
    args = {
        "url": "https://receiver.example/inventory",
        "token_file": token_file(tmp_path),
        "ca_file": None,
        "cert_file": None,
        "key_file": None,
    }
    args.update(overrides)
    return SimpleNamespace(**args)


class FakeResponse:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_publish_posts_json_with_a_bearer_token(tmp_path):
    with (
        mock.patch.object(cli, "urlopen", return_value=FakeResponse(201)) as urlopen,
        mock.patch.object(cli.ssl, "create_default_context") as ctx,
    ):
        cli._publish_inventory({"sessions": [1]}, publish_args(tmp_path))
    request = urlopen.call_args.args[0]
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer s3cret"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"sessions": [1]}
    assert urlopen.call_args.kwargs["context"] is ctx.return_value
    assert urlopen.call_args.kwargs["timeout"] == 15


@pytest.mark.parametrize(
    "url",
    [
        "http://receiver.example/x",
        "ftp://receiver.example/x",
        "https://user:pw@receiver.example/x",
        "https://user@receiver.example/x",
        "https:///nohost",
        "receiver.example/x",
    ],
)
def test_a_publish_refuses_plain_http_and_embedded_credentials(tmp_path, url):
    with mock.patch.object(cli, "urlopen") as urlopen, pytest.raises(ValueError, match="HTTPS"):
        cli._publish_inventory({}, publish_args(tmp_path, url=url))
    urlopen.assert_not_called()


def test_the_client_certificate_and_key_come_together_or_not_at_all(tmp_path):
    for one in ({"cert_file": Path("c.pem")}, {"key_file": Path("k.pem")}):
        with (
            mock.patch.object(cli, "urlopen") as urlopen,
            mock.patch.object(cli.ssl, "create_default_context"),
            pytest.raises(ValueError, match="together"),
        ):
            cli._publish_inventory({}, publish_args(tmp_path, **one))
        urlopen.assert_not_called()


def test_a_client_certificate_pair_is_loaded_into_the_context(tmp_path):
    with (
        mock.patch.object(cli, "urlopen", return_value=FakeResponse()),
        mock.patch.object(cli.ssl, "create_default_context") as ctx,
    ):
        cli._publish_inventory(
            {},
            publish_args(
                tmp_path, cert_file=Path("c.pem"), key_file=Path("k.pem"), ca_file=Path("ca.pem")
            ),
        )
    ctx.assert_called_once_with(cafile="ca.pem")
    ctx.return_value.load_cert_chain.assert_called_once_with("c.pem", "k.pem")


@pytest.mark.parametrize("status", [301, 400, 500])
def test_an_unexpected_receiver_status_is_an_error(tmp_path, status):
    with (
        mock.patch.object(cli, "urlopen", return_value=FakeResponse(status)),
        mock.patch.object(cli.ssl, "create_default_context"),
        pytest.raises(ValueError, match=str(status)),
    ):
        cli._publish_inventory({}, publish_args(tmp_path))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o777])
def test_a_token_readable_by_others_is_refused(tmp_path, mode):
    with pytest.raises(ValueError, match="owner-only"):
        cli._read_owner_only_token(token_file(tmp_path, mode=mode))


def test_a_token_that_is_not_a_regular_file_or_is_empty_is_refused(tmp_path):
    directory = tmp_path / "dir"
    directory.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="regular"):
        cli._read_owner_only_token(directory)
    with pytest.raises(ValueError, match="empty"):
        cli._read_owner_only_token(token_file(tmp_path, content="  \n"))


def test_a_good_token_is_returned_trimmed(tmp_path):
    assert cli._read_owner_only_token(token_file(tmp_path, content=" abc \n", mode=0o400)) == "abc"


def test_publishing_reports_success_with_the_record_count(env, scanner, tmp_path, capsys):
    with (
        mock.patch.object(cli, "build_inventory", return_value={"sessions": [1, 2, 3]}),
        mock.patch.object(cli, "_publish_inventory") as publish,
    ):
        status = cli.main(
            [
                "publish-inventory",
                "--url",
                "https://r.example/i",
                "--token-file",
                str(tmp_path / "t"),
            ]
        )
    assert status == 0
    assert "Published 3 session summary record(s)." in capsys.readouterr().out
    assert publish.call_args.args[0] == {"sessions": [1, 2, 3]}


@pytest.mark.parametrize(
    "error",
    [
        OSError("no route"),
        ValueError("bad token"),
        cli.URLError("refused"),
        cli.HTTPError("u", 500, "x", {}, None),
    ],
)
def test_publishing_failures_are_reported_with_exit_status_1(env, scanner, tmp_path, capsys, error):
    with (
        mock.patch.object(cli, "build_inventory", return_value={"sessions": []}),
        mock.patch.object(cli, "_publish_inventory", side_effect=error),
    ):
        status = cli.main(
            ["publish-inventory", "--url", "https://r.example/i", "--token-file", "t"]
        )
    assert status == 1
    assert "ERROR: Could not publish inventory" in capsys.readouterr().out


def test_the_bearer_token_never_appears_in_the_error_output(env, scanner, tmp_path, capsys):
    path = token_file(tmp_path, content="TOPSECRET\n")
    with (
        mock.patch.object(cli, "build_inventory", return_value={"sessions": []}),
        mock.patch.object(cli, "urlopen", side_effect=cli.URLError("connection refused")),
        mock.patch.object(cli.ssl, "create_default_context"),
    ):
        cli.main(["publish-inventory", "--url", "https://r.example/i", "--token-file", str(path)])
    captured = capsys.readouterr()
    assert "TOPSECRET" not in captured.out + captured.err


# --------------------------------------------------------------------------- deduplicate


def dedup_result(dry_run=True, errors=()):
    first, second = (
        record(session_id="aaaaaaaa11", title="Same"),
        record(session_id="bbbbbbbb22", title="Same"),
    )
    merged = (MergeResult(first, (second,), Path("/trash/x")),) if not dry_run else ()
    return DeduplicateResult(
        min_age=timedelta(days=50),
        max_age=timedelta(days=100),
        dry_run=dry_run,
        scanned_count=2,
        groups=((first, second),),
        merged=merged,
        errors=tuple(errors),
    )


def test_deduplicate_passes_its_window_similarity_and_dry_run(service):
    service.deduplicate.return_value = dedup_result()
    cli.main(
        [
            "deduplicate",
            "--min-age",
            "10d",
            "--max-age",
            "2w",
            "--similarity",
            "0.9",
            "--dry-run",
            "--provider",
            "codex",
        ]
    )
    call = service.deduplicate.call_args
    assert call.args == (timedelta(days=10), timedelta(weeks=2))
    assert call.kwargs == {"similarity": 0.9, "providers": ("codex",), "dry_run": True}


def test_deduplicate_defaults_come_from_the_environment(service, monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_DEDUP_MIN_AGE", "7d")
    monkeypatch.setenv("SESSION_CONTROL_DEDUP_MAX_AGE", "30d")
    monkeypatch.setenv("SESSION_CONTROL_DEDUP_SIMILARITY", "0.5")
    service.deduplicate.return_value = dedup_result()
    cli.main(["deduplicate"])
    assert service.deduplicate.call_args.args == (timedelta(days=7), timedelta(days=30))
    assert service.deduplicate.call_args.kwargs["similarity"] == 0.5


def test_a_dry_run_lists_the_groups_without_claiming_to_have_merged(service, capsys):
    service.deduplicate.return_value = dedup_result(dry_run=True)
    assert cli.main(["deduplicate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would merge 1 duplicate group(s) in age window 50d–100d." in out
    assert "GROUP (2 sessions, provider=codex)" in out
    assert "MERGED" not in out


def test_a_real_run_reports_what_was_kept_and_absorbed(service, capsys):
    service.deduplicate.return_value = dedup_result(dry_run=False)
    assert cli.main(["deduplicate"]) == 0
    out = capsys.readouterr().out
    assert "Merged 1 duplicate group(s)" in out
    assert "kept=aaaaaaaa  absorbed=[bbbbbbbb]  -> /trash/x" in out


def test_deduplicate_errors_make_the_exit_status_1_and_are_listed(service, capsys):
    service.deduplicate.return_value = dedup_result(errors=["disk full"])
    assert cli.main(["deduplicate", "--dry-run"]) == 1
    assert "ERROR   disk full" in capsys.readouterr().out


def test_deduplicate_json_has_the_counts_and_the_sessions(service, capsys):
    service.deduplicate.return_value = dedup_result(dry_run=False, errors=["x"])
    status = cli.main(["deduplicate", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert status == 1
    assert payload["min_age"] == "50d" and payload["max_age"] == "100d"
    assert (payload["group_count"], payload["merged_count"], payload["error_count"]) == (1, 1, 1)
    assert payload["merged"][0]["moved_to"] == "/trash/x"
    assert payload["merged"][0]["absorbed"][0]["session_id"] == "bbbbbbbb22"


# --------------------------------------------------------------------------- prune


def prune_result(dry_run=True, short=False, skipped=False, errors=False, deleted=False):
    session = record(session_id="ccccccccdd", title="Old one")
    return PruneResult(
        cutoff=CUTOFF,
        dry_run=dry_run,
        scanned_count=5,
        eligible=(session,),
        deleted=(DeleteResult(session, Path("/trash/y"), 2),) if deleted else (),
        skipped=(PruneSkipped(session, "still active"),) if skipped else (),
        errors=(
            (PruneError("codex", "boom", session), PruneError("claude", "no session"))
            if errors
            else ()
        ),
        short_subagent_cutoff=CUTOFF + timedelta(days=135) if short else None,
    )


def test_prune_defaults_to_180_days_and_rotates_after_a_real_run(service, capsys):
    service.prune.return_value = prune_result(dry_run=False, deleted=True)
    service.rotate.return_value = (3, 4)
    assert cli.main(["prune"]) == 0
    assert service.prune.call_args.args == (timedelta(days=180),)
    assert service.prune.call_args.kwargs["dry_run"] is False
    service.rotate.assert_called_once_with()
    out = capsys.readouterr().out
    assert "Deleted 1 session(s) older than 180d" in out
    assert "DELETED codex" in out and "-> /trash/y (2 target(s))" in out
    assert "Expired 3 trash entries and 4 digest records." in out


def test_a_prune_dry_run_never_rotates_or_deletes(service, capsys):
    service.prune.return_value = prune_result(dry_run=True)
    assert cli.main(["prune", "--dry-run", "--older-than", "30d"]) == 0
    service.rotate.assert_not_called()
    assert service.prune.call_args.args == (timedelta(days=30),)
    out = capsys.readouterr().out
    assert "Would delete 1 session(s) older than 30d" in out
    assert "DRYRUN  codex" in out and "Old one" in out
    assert "Expired" not in out


def test_prune_json_replaces_the_text_and_the_expiry_line(service, capsys):
    service.prune.return_value = prune_result(
        dry_run=False, deleted=True, skipped=True, errors=True
    )
    service.rotate.return_value = (0, 0)
    status = cli.main(["prune", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert status == 1
    assert (payload["eligible_count"], payload["deleted_count"], payload["skipped_count"]) == (
        1,
        1,
        1,
    )
    assert payload["error_count"] == 2
    assert payload["cutoff"] == "2026-05-01T00:00:00Z"
    assert payload["short_subagent_cutoff"] is None
    assert payload["errors"][1]["session"] is None and payload["errors"][0]["session"]["session_id"]
    assert payload["deleted"][0]["moved_to"] == "/trash/y"
    service.rotate.assert_called_once_with()


def test_prune_text_lists_skips_and_errors_and_exits_1(service, capsys):
    service.prune.return_value = prune_result(dry_run=True, skipped=True, errors=True)
    assert cli.main(["prune", "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "Skipped 1 old active session(s)." in out
    assert "SKIP    codex    ccccccccdd (still active)" in out
    assert "ERROR   codex    ccccccccdd: boom" in out
    assert "ERROR   claude  : no session" in out


def test_short_subagents_are_reported_with_their_own_retention(service, capsys):
    service.prune.return_value = prune_result(dry_run=True, short=True)
    cli.main(["prune", "--dry-run", "--short-subagent-percent", "25%"])
    assert service.prune.call_args.kwargs["short_subagent_age"] == timedelta(days=45)
    out = capsys.readouterr().out
    assert "Standard retention: 180d (cutoff 2026-05-01T00:00:00Z)." in out
    assert "Short subagents: 45d (cutoff 2026-09-13T00:00:00Z)." in out


def test_a_multiplier_and_the_subagent_only_flag_are_passed_through(service):
    service.prune.return_value = prune_result()
    cli.main(["prune", "--dry-run", "--short-subagent-multiplier", "4x", "--short-subagents-only"])
    kwargs = service.prune.call_args.kwargs
    assert kwargs["short_subagent_age"] == timedelta(days=45)
    assert kwargs["short_subagents_only"] is True


def test_the_short_subagent_factor_can_come_from_the_environment(service, monkeypatch):
    service.prune.return_value = prune_result()
    monkeypatch.setenv("SESSION_CONTROL_PRUNE_SHORT_SUBAGENT_PERCENT", "50%")
    cli.main(["prune", "--dry-run"])
    assert service.prune.call_args.kwargs["short_subagent_age"] == timedelta(days=90)
    # an explicit flag wins over the environment
    cli.main(["prune", "--dry-run", "--short-subagent-multiplier", "10x"])
    assert service.prune.call_args.kwargs["short_subagent_age"] == timedelta(days=18)


def test_an_invalid_factor_is_a_usage_error_and_prunes_nothing(service, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["prune", "--short-subagent-percent", "150%"])
    assert raised.value.code == 2
    assert "at most 100%" in capsys.readouterr().err
    service.prune.assert_not_called()


def test_the_two_factor_flags_exclude_each_other(service):
    with pytest.raises(SystemExit) as raised:
        cli.main(["prune", "--short-subagent-percent", "25%", "--short-subagent-multiplier", "4x"])
    assert raised.value.code == 2
    service.prune.assert_not_called()


def test_the_default_retention_can_come_from_the_environment(env, monkeypatch):
    monkeypatch.setenv("SESSION_CONTROL_PRUNE_OLDER_THAN", "9d")
    assert cli._build_parser().parse_args(["prune"]).older_than == timedelta(days=9)


# --------------------------------------------------------------------------- terminate / rotate / restore


def test_terminate_current_uses_the_codex_thread_and_says_the_chat_is_still_open(
    service, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-123")
    service.terminate_current.return_value = SimpleNamespace(moved_count=3)
    assert cli.main(["terminate-current"]) == 0
    service.terminate_current.assert_called_once_with("thread-123")
    out = capsys.readouterr().out
    assert "Moved current Codex session to trash: 3 file(s)." in out
    assert "still open" in out


def test_terminate_current_without_a_thread_is_an_error(service, capsys):
    service.terminate_current.side_effect = SessionActionError("CODEX_THREAD_ID is not set")
    assert cli.main(["terminate-current"]) == 1
    service.terminate_current.assert_called_once_with("")
    assert "ERROR: CODEX_THREAD_ID is not set" in capsys.readouterr().out


@pytest.mark.parametrize(("flag", "verb"), [([], "Expired"), (["--dry-run"], "Would expire")])
def test_rotate_reports_what_it_expired_or_would_expire(service, capsys, flag, verb):
    service.rotate.return_value = (2, 5)
    assert cli.main(["rotate", *flag]) == 0
    service.rotate.assert_called_once_with(dry_run=bool(flag))
    assert f"{verb} 2 trash entries and 5 digest records." in capsys.readouterr().out


CANDIDATE = RestoreCandidate("2026/codex/abc", "codex", "abc", "2026-04-01T00:00:00Z", 2)


def test_restore_list_prints_entries_or_says_there_are_none(service, capsys):
    service.list_trash.return_value = ()
    assert cli.main(["restore", "--list"]) == 0
    assert "No restorable sessions in local trash." in capsys.readouterr().out
    service.list_trash.return_value = (CANDIDATE,)
    cli.main(["restore", "--list"])
    assert "2026/codex/abc  codex    abc (2 target(s))" in capsys.readouterr().out


def test_restore_list_json(service, capsys):
    service.list_trash.return_value = (CANDIDATE,)
    cli.main(["restore", "--list", "--json"])
    assert json.loads(capsys.readouterr().out) == [
        {
            "trash_id": "2026/codex/abc",
            "provider": "codex",
            "session_id": "abc",
            "moved_at": "2026-04-01T00:00:00Z",
            "moved_count": 2,
        }
    ]


def test_restore_needs_an_entry_or_the_list_flag(service, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["restore"])
    assert raised.value.code == 2
    assert "restore requires a trash entry or --list" in capsys.readouterr().err
    service.restore.assert_not_called()


def test_restore_reports_where_the_files_went(service, capsys):
    service.restore.return_value = RestoreResult(CANDIDATE, (Path("/a/one"), Path("/a/two")))
    assert cli.main(["restore", "2026/codex/abc"]) == 0
    service.restore.assert_called_once_with("2026/codex/abc")
    out = capsys.readouterr().out
    assert "RESTORED codex    abc from 2026/codex/abc" in out
    assert "  -> /a/one" in out and "  -> /a/two" in out


def test_restore_json(service, capsys):
    service.restore.return_value = RestoreResult(CANDIDATE, (Path("/a/one"),))
    cli.main(["restore", "x", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["restored_to"] == ["/a/one"]
    assert payload["candidate"]["session_id"] == "abc"


def test_a_failed_restore_is_exit_status_1(service, capsys):
    service.restore.side_effect = SessionActionError("destination exists")
    assert cli.main(["restore", "x"]) == 1
    assert "ERROR: destination exists" in capsys.readouterr().out


def test_an_inventory_file_is_never_world_readable_even_for_an_instant(env):
    # the file must be created owner-only, not created open and tightened afterwards
    import os

    previous = os.umask(0)
    try:
        with mock.patch.object(cli.os, "chmod"):
            cli._write_owner_only(env / "created.json", "x")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((env / "created.json").stat().st_mode) == 0o600


def test_a_multiplier_below_one_is_refused_rather_than_lengthening_retention(service, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["prune", "--short-subagent-multiplier", "0.5x"])
    assert raised.value.code == 2
    assert "at least 1x" in capsys.readouterr().err
    service.prune.assert_not_called()


def test_prune_json_reports_the_short_subagent_cutoff(service, capsys):
    service.prune.return_value = prune_result(dry_run=True, short=True)
    cli.main(["prune", "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["short_subagent_cutoff"] == "2026-09-13T00:00:00Z"
