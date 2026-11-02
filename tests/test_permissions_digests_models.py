"""Codex permission presets, digest records, session models and the module entry point.

The preset decides what sandbox and approval policy a resumed Codex session runs with,
so the mapping is pinned exactly, and an unknown value must fall back to "use Codex's
own config", never to something more permissive. Digest records outlive the session they
describe, so they must hold metadata only and be written owner-only.
"""

from __future__ import annotations

import json
import os
import runpy
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from session_control import digests
from session_control.codex_permissions import (
    CODEX_PERMISSION_PRESET_CHOICES,
    codex_permission_args,
    codex_permission_summary,
    normalize_codex_permission_preset,
)
from session_control.digests import DigestStore, category_for
from session_control.models import SessionRecord


def record(**kwargs) -> SessionRecord:
    base = {
        "provider": "codex",
        "session_id": "abc",
        "title": "Fix the failing test",
        "workspace": "/home/me/projects/example/",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-02T00:00:00Z",
        "primary_path": Path("/s/x.jsonl"),
        "delete_targets": (),
        "size_bytes": 10,
        "message_count": 3,
        "preview": "a private preview about secrets",
        "resume_command": "codex resume abc",
        "metadata": {},
    }
    return SessionRecord(**{**base, **kwargs})


# --------------------------------------------------------------------------- presets


@pytest.mark.parametrize(
    ("value", "preset"),
    [
        (None, "default"), ("", "default"), ("  ", "default"), ("config", "default"),
        ("codex-config", "default"), ("current", "default"), ("DEFAULT", "default"),
        ("session", "recorded"), ("previous", "recorded"), ("recorded", "recorded"),
        ("read-only", "read-only"),
        ("workspace", "auto"), ("workspace-write", "auto"), ("auto", "auto"),
        ("full", "full-auto"), ("danger", "full-auto"), ("danger-full-access", "full-auto"),
        ("yolo", "full-auto"), (" Full-Auto ", "full-auto"),
    ],
)  # fmt: skip
def test_preset_names_and_aliases(value, preset):
    assert normalize_codex_permission_preset(value) == preset


@pytest.mark.parametrize(
    "value", ["bogus", "full-auto-please", "danger!", "sudo", 5, ["full-auto"]]
)
def test_an_unknown_preset_falls_back_to_the_codex_config_never_to_more_access(value):
    assert normalize_codex_permission_preset(value) == "default"
    assert codex_permission_args(value, {}) == []


def test_each_preset_maps_to_exactly_its_flags():
    expected = {
        "default": [],
        "read-only": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
        "auto": ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
        "full-auto": ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
    }
    for preset, args in expected.items():
        assert codex_permission_args(preset, {"sandbox_mode": "danger-full-access"}) == args, preset


def test_the_recorded_preset_replays_only_valid_recorded_values():
    metadata = {"sandbox_mode": "workspace-write", "approval_policy": "on-failure"}
    assert codex_permission_args("recorded", metadata) == [
        "--sandbox", "workspace-write", "--ask-for-approval", "on-failure",
    ]  # fmt: skip
    assert codex_permission_args("recorded", {"sandbox_mode": "read-only"}) == [
        "--sandbox",
        "read-only",
    ]
    assert codex_permission_args("recorded", {"approval_policy": "never"}) == [
        "--ask-for-approval",
        "never",
    ]
    assert codex_permission_args("recorded", {}) == []


@pytest.mark.parametrize("bad", ["full", "danger", "rm -rf /", "", None, 5, "READ-ONLY"])
def test_a_recorded_value_that_is_not_a_known_mode_is_never_replayed(bad):
    assert codex_permission_args("recorded", {"sandbox_mode": bad, "approval_policy": bad}) == []


def test_the_summary_shows_what_was_recorded():
    assert (
        codex_permission_summary({"sandbox_mode": "read-only", "approval_policy": "never"})
        == "read-only / never"
    )
    assert codex_permission_summary({"sandbox_mode": "read-only"}) == "read-only"
    assert codex_permission_summary({"approval_policy": "untrusted"}) == "untrusted"
    assert codex_permission_summary({"sandbox_mode": "bogus", "approval_policy": "bogus"}) == ""
    assert codex_permission_summary({}) == ""


def test_the_offered_choices_are_exactly_the_presets_the_code_understands():
    values = [choice["value"] for choice in CODEX_PERMISSION_PRESET_CHOICES]
    assert values == ["default", "recorded", "read-only", "auto", "full-auto"]
    for value in values:
        assert normalize_codex_permission_preset(value) == value
    assert all(
        choice["label"] and choice["description"] for choice in CODEX_PERMISSION_PRESET_CHOICES
    )


# --------------------------------------------------------------------------- categories and digests


@pytest.mark.parametrize(
    ("title", "preview", "category"),
    [
        ("Implement the feature", "", "code"),
        ("Fix bug and debug the error", "", "debugging"),
        ("Write the README guide", "", "documentation"),
        ("Add pytest verification", "", "testing"),
        ("Deploy the server config", "", "operations"),
        ("Research and compare options", "", "research"),
        ("Hello", "world", "other"),
        ("", "", "other"),
    ],
)
def test_sessions_are_classified_by_keywords(title, preview, category):
    assert category_for(record(title=title, preview=preview)) == category


def test_the_preview_counts_towards_the_category():
    assert (
        category_for(record(title="Chat", preview="debug the error and fix the bug")) == "debugging"
    )


def test_a_digest_holds_metadata_only_and_is_owner_only(tmp_path):
    store = DigestStore(tmp_path / "digests")
    path = store.record(record(), reason="delete")
    payload = json.loads(path.read_text())
    assert set(payload) == {
        "version", "category", "repo", "provider", "reason", "is_subagent",
        "message_count", "size_bytes", "ended_at",
    }  # fmt: skip
    assert payload["repo"] == "example" and payload["reason"] == "delete"
    text = path.read_text()
    for private in ("private preview", "Fix the failing test", "/home/me", "abc"):
        assert private not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_session_with_no_workspace_is_filed_under_unknown(tmp_path):
    path = DigestStore(tmp_path).record(record(workspace=""), reason="merge")
    assert json.loads(path.read_text())["repo"] == "unknown"


def test_two_records_of_one_session_get_distinct_files(tmp_path):
    store = DigestStore(tmp_path)
    first, second = store.record(record(), reason="a"), store.record(record(), reason="b")
    assert first != second and len(store.records()) == 2


def test_records_skip_symlinks_corrupt_files_and_other_versions(tmp_path):
    store = DigestStore(tmp_path)
    store.record(record(), reason="delete")
    outside = tmp_path.parent / "outside.json"
    outside.write_text(
        json.dumps({"version": 1, "category": "code", "repo": "leak"}), encoding="utf-8"
    )
    (tmp_path / "link.json").symlink_to(outside)
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    (tmp_path / "v2.json").write_text(json.dumps({"version": 2}), encoding="utf-8")
    (tmp_path / "list.json").write_text("[1]", encoding="utf-8")
    records = store.records()
    assert len(records) == 1 and records[0]["repo"] == "example"


def test_records_of_a_missing_store_are_empty(tmp_path):
    assert DigestStore(tmp_path / "absent").records() == ()
    assert DigestStore(tmp_path / "absent").coverage()["total"] == 0


def test_coverage_counts_categories_and_repositories(tmp_path):
    store = DigestStore(tmp_path)
    store.record(record(title="fix the bug"), reason="delete")
    store.record(record(title="fix another bug"), reason="delete")
    store.record(record(title="Hello", preview="", workspace="/w/other"), reason="delete")
    coverage = store.coverage()
    assert coverage["total"] == 3
    assert coverage["categories"] == {"debugging": 2, "other": 1}
    assert coverage["repos"] == {"example": 2, "other": 1}


def test_rotation_expires_only_old_regular_files_and_dry_run_removes_nothing(tmp_path):
    store = DigestStore(tmp_path)
    old = store.record(record(), reason="a")
    fresh = store.record(record(session_id="def"), reason="b")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=400)).timestamp()
    os.utime(old, (long_ago, long_ago))
    (tmp_path / "dir.json").mkdir()
    outside = tmp_path.parent / "keep.json"
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    os.utime(link, (long_ago, long_ago), follow_symlinks=False)
    assert store.rotate(timedelta(days=365), dry_run=True) == 1
    assert old.exists()
    assert store.rotate(timedelta(days=365)) == 1
    assert not old.exists() and fresh.exists() and outside.exists() and link.is_symlink()


def test_rotating_a_missing_store_expires_nothing(tmp_path):
    assert DigestStore(tmp_path / "absent").rotate(timedelta(days=1)) == 0


def test_digest_filenames_do_not_reveal_the_session(tmp_path):
    path = DigestStore(tmp_path).record(record(), reason="delete")
    assert "abc" not in path.name and len(path.stem) == 64


# --------------------------------------------------------------------------- models


def test_a_short_subagent_is_a_subagent_with_five_minutes_or_less_of_activity():
    short = record(metadata={"is_subagent": True, "last_event_at": "2026-04-01T00:05:00Z"})
    long = record(metadata={"is_subagent": True, "last_event_at": "2026-04-01T00:05:01Z"})
    not_sub = record(metadata={"is_subagent": False, "last_event_at": "2026-04-01T00:01:00Z"})
    before = record(metadata={"is_subagent": True, "last_event_at": "2026-03-31T00:00:00Z"})
    assert short.is_short_subagent is True and short.is_subagent is True
    assert long.is_short_subagent is False
    assert not_sub.is_short_subagent is False and not_sub.is_subagent is False
    assert before.is_short_subagent is False


@pytest.mark.parametrize(
    "metadata",
    [
        {"is_subagent": True},
        {"is_subagent": True, "last_event_at": "garbage"},
        {"is_subagent": True, "last_event_at": None},
        {"is_subagent": "yes", "last_event_at": "2026-04-01T00:01:00Z"},
    ],
)
def test_missing_or_malformed_timing_is_never_a_short_subagent(metadata):
    assert record(metadata=metadata).is_short_subagent is False


def test_a_public_id_is_stable_distinct_and_does_not_reveal_the_session():
    first = record()
    assert first.public_id == record().public_id and len(first.public_id) == 20
    assert first.public_id != record(session_id="other").public_id
    assert first.public_id != record(primary_path=Path("/elsewhere.jsonl")).public_id
    assert first.public_id != record(provider="claude").public_id
    assert "abc" not in first.public_id


# --------------------------------------------------------------------------- entry point


def test_python_dash_m_runs_the_cli_and_exits_with_its_status():
    with (
        mock.patch("session_control.cli.main", return_value=3) as main,
        pytest.raises(SystemExit) as raised,
    ):
        runpy.run_module("session_control", run_name="__main__")
    main.assert_called_once_with()
    assert raised.value.code == 3


def test_importing_the_module_does_not_run_anything():
    with mock.patch("session_control.cli.main") as main:
        runpy.run_module("session_control.__main__", run_name="not_main")
    main.assert_not_called()


def test_the_digest_module_exposes_its_categories():
    assert set(digests._CATEGORIES) == {
        "code",
        "debugging",
        "documentation",
        "testing",
        "operations",
        "research",
    }
