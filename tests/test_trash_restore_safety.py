"""Trash, restore and delete: the refusals that keep a bad manifest or path from doing harm.

Restore moves files out of the trash on the strength of a manifest that sits next to them,
so a damaged or tampered manifest must be refused rather than followed. Delete must refuse
live sessions and anything outside the provider's own folder. Every case also checks that
nothing was moved. All files live under a temporary directory.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_control import actions
from session_control.actions import SessionActionError, SessionActionService
from session_control.models import SessionRecord
from tests.helpers import write_json

MANIFEST = ".session-control-restore.json"
OLD_STAMP = "20260101T000000Z"
RECENT_STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_entry(
    config, *, provider="codex", sid="abc123", stamp=OLD_STAMP, files=None, manifest=None
):
    """A trash batch: payload files plus its manifest. Returns (trash_id, batch_dir)."""
    batch = config.trash_dir / stamp / provider / sid
    files = files if files is not None else {"rollout-abc123.jsonl": '{"n": 1}\n'}
    for name, text in files.items():
        (batch / name).parent.mkdir(parents=True, exist_ok=True)
        (batch / name).write_text(text, encoding="utf-8")
    if manifest is None:
        manifest = {
            "version": 1,
            "provider": provider,
            "session_id": sid,
            "moved_at": stamp,
            "targets": [
                {"trash_path": name, "provider_path": f"sessions/{Path(name).name}"}
                for name in files
            ],
        }
    batch.mkdir(parents=True, exist_ok=True)
    if isinstance(manifest, str):
        (batch / MANIFEST).write_text(manifest, encoding="utf-8")
    else:
        write_json(batch / MANIFEST, manifest)
    return f"{stamp}/{provider}/{sid}", batch


def svc(config, *sessions):
    class Scanner:
        def scan(self, providers=None):
            return SimpleNamespace(sessions=tuple(sessions), errors=())

    return SessionActionService(config, Scanner())


def session(config, sid="s1", *, provider="codex", active=False, name=None, targets=None):
    root = config.provider_root(provider)
    path = root / "sessions" / (name or f"rollout-{sid}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return SessionRecord(
        provider=provider,
        session_id=sid,
        title="t",
        workspace="/w",
        created_at="",
        updated_at="",
        primary_path=path,
        delete_targets=tuple(targets if targets is not None else (path,)),
        size_bytes=1,
        message_count=1,
        preview="p",
        resume_command="",
        active=active,
    )


class TestListTrash:
    def test_an_absent_trash_directory_lists_nothing(self, app_config):
        assert svc(app_config).list_trash() == ()

    def test_valid_entries_are_listed_newest_first(self, app_config):
        make_entry(app_config, sid="old", stamp="20260101T000000Z")
        make_entry(app_config, sid="new", stamp="20260301T000000Z")
        listed = svc(app_config).list_trash()
        assert [c.session_id for c in listed] == ["new", "old"]
        assert listed[0].trash_id == "20260301T000000Z/codex/new"
        assert listed[0].moved_count == 1

    @pytest.mark.parametrize(
        "manifest",
        [
            "{ not json",
            {"provider": "nonsense", "session_id": "bad", "targets": []},
            {"provider": "codex", "session_id": "", "targets": []},
            {"provider": "codex", "session_id": "bad", "targets": "not a list"},
            {"provider": "claude", "session_id": "bad", "targets": []},  # directory says codex
            {
                "provider": "codex",
                "session_id": "someone-else",
                "targets": [],
            },  # directory says bad
        ],
    )
    def test_a_damaged_or_mismatched_manifest_is_skipped_not_raised(self, app_config, manifest):
        make_entry(app_config, sid="bad", manifest=manifest)
        make_entry(app_config, sid="good")
        assert [c.session_id for c in svc(app_config).list_trash()] == ["good"]

    def test_an_entry_filed_under_an_unknown_provider_is_skipped_and_cannot_be_restored(
        self, app_config
    ):
        # The folder and the manifest agree, so only the provider check can refuse it.
        trash_id, _ = make_entry(
            app_config,
            provider="nonsense",
            sid="odd",
            manifest={
                "provider": "nonsense",
                "session_id": "odd",
                "moved_at": OLD_STAMP,
                "targets": [],
            },
        )
        assert svc(app_config).list_trash() == ()
        with pytest.raises(SessionActionError, match="Trash entry is invalid"):  # not a KeyError
            svc(app_config).restore(trash_id)

    def test_a_manifest_at_the_wrong_depth_is_skipped(self, app_config):
        shallow = app_config.trash_dir / "20260101T000000Z" / "codex"
        shallow.mkdir(parents=True)
        write_json(shallow / MANIFEST, {"provider": "codex", "session_id": "x", "targets": []})
        assert svc(app_config).list_trash() == ()

    def test_a_symlinked_manifest_is_ignored(self, app_config, tmp_path):
        real = tmp_path / "elsewhere.json"
        write_json(real, {"provider": "codex", "session_id": "linked", "targets": []})
        batch = app_config.trash_dir / OLD_STAMP / "codex" / "linked"
        batch.mkdir(parents=True)
        (batch / MANIFEST).symlink_to(real)
        assert svc(app_config).list_trash() == ()


class TestRestoreRefusals:
    @pytest.mark.parametrize(
        "trash_id",
        [
            "/abs/path/here",
            "only/two",
            "a/b/c/d",
            "../escape/codex/x",
            "20260101T000000Z/../x",
            "a//b",
            "./a/b",
        ],
    )
    def test_a_trash_id_must_be_a_plain_three_part_relative_path(self, app_config, trash_id):
        with pytest.raises(SessionActionError, match="relative timestamp/provider/session-id"):
            svc(app_config).restore(trash_id)

    def test_an_entry_that_does_not_exist_is_reported_as_not_found(self, app_config):
        with pytest.raises(SessionActionError, match="Trash entry was not found"):
            svc(app_config).restore("20260101T000000Z/codex/missing")

    @pytest.mark.parametrize(
        "manifest",
        ["{ not json", {"provider": "bogus", "session_id": "abc123", "targets": []}],
    )
    def test_an_invalid_manifest_is_refused(self, app_config, manifest):
        trash_id, _ = make_entry(app_config, manifest=manifest)
        with pytest.raises(SessionActionError, match="Trash entry is invalid"):
            svc(app_config).restore(trash_id)

    @pytest.mark.parametrize(
        ("targets", "message"),
        [
            ([], "no restorable files"),
            (["not an object"], "invalid restore target"),
            ([{"trash_path": "/etc/passwd", "provider_path": "x"}], "unsafe restore path"),
            (
                [{"trash_path": "rollout-abc123.jsonl", "provider_path": "/abs/x"}],
                "unsafe restore path",
            ),
            ([{"trash_path": "../up.jsonl", "provider_path": "x"}], "unsafe restore path"),
            (
                [{"trash_path": "rollout-abc123.jsonl", "provider_path": "../../out.jsonl"}],
                "unsafe restore path",
            ),
            ([{"trash_path": "", "provider_path": "x"}], "unsafe restore path"),
            ([{"trash_path": "rollout-abc123.jsonl", "provider_path": ""}], "unsafe restore path"),
        ],
    )
    def test_unsafe_or_empty_restore_targets_are_refused_and_nothing_moves(
        self, app_config, targets, message
    ):
        manifest = {
            "version": 1,
            "provider": "codex",
            "session_id": "abc123",
            "moved_at": OLD_STAMP,
            "targets": targets,
        }
        trash_id, batch = make_entry(app_config, manifest=manifest)
        with pytest.raises(SessionActionError, match=message):
            svc(app_config).restore(trash_id)
        assert (batch / "rollout-abc123.jsonl").exists()
        assert not app_config.codex_root.exists()

    @pytest.mark.parametrize(
        ("trash_path", "provider_path"),
        [
            (
                "sub/../rollout-abc123.jsonl",
                "sessions/f.jsonl",
            ),  # '..' that resolves inside the batch
            (
                "rollout-abc123.jsonl",
                "sessions/../sessions/f.jsonl",
            ),  # '..' that resolves inside the root
            (
                "@BATCH@/rollout-abc123.jsonl",
                "sessions/f.jsonl",
            ),  # absolute, but pointing inside the batch
            (
                "rollout-abc123.jsonl",
                "@ROOT@/sessions/f.jsonl",
            ),  # absolute, but pointing inside the root
        ],
    )
    def test_manifests_must_use_plain_relative_paths_even_when_a_fancy_one_would_be_harmless(
        self, app_config, trash_path, provider_path
    ):
        trash_id, batch = make_entry(app_config)
        trash_path = trash_path.replace("@BATCH@", str(batch))
        provider_path = provider_path.replace("@ROOT@", str(app_config.codex_root))
        manifest = {
            "version": 1,
            "provider": "codex",
            "session_id": "abc123",
            "moved_at": OLD_STAMP,
            "targets": [{"trash_path": trash_path, "provider_path": provider_path}],
        }
        write_json(batch / MANIFEST, manifest)
        (batch / "sub").mkdir(exist_ok=True)
        with pytest.raises(SessionActionError, match="unsafe restore path"):
            svc(app_config).restore(trash_id)
        assert (batch / "rollout-abc123.jsonl").exists()
        assert not (app_config.codex_root / "sessions").exists()

    def test_a_manifest_without_a_targets_object_list_is_refused_directly(self, tmp_path):
        with pytest.raises(SessionActionError, match="no restore targets"):
            actions._restore_targets([], tmp_path, tmp_path)
        with pytest.raises(SessionActionError, match="no restore targets"):
            actions._restore_targets({"targets": "x"}, tmp_path, tmp_path)

    def test_a_destination_that_escapes_through_a_symlink_is_refused(self, app_config, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        app_config.codex_root.mkdir(parents=True)
        (app_config.codex_root / "sessions").symlink_to(outside, target_is_directory=True)
        trash_id, batch = make_entry(app_config)
        with pytest.raises(SessionActionError, match="unsafe restore path"):
            svc(app_config).restore(trash_id)
        assert list(outside.iterdir()) == []
        assert (batch / "rollout-abc123.jsonl").exists()

    def test_a_missing_payload_file_is_reported_by_name(self, app_config):
        trash_id, batch = make_entry(app_config)
        (batch / "rollout-abc123.jsonl").unlink()
        with pytest.raises(
            SessionActionError, match="Trash payload is missing: rollout-abc123.jsonl"
        ):
            svc(app_config).restore(trash_id)

    def test_an_existing_destination_is_never_overwritten(self, app_config):
        trash_id, batch = make_entry(app_config)
        existing = app_config.codex_root / "sessions" / "rollout-abc123.jsonl"
        existing.parent.mkdir(parents=True)
        existing.write_text("the live one", encoding="utf-8")
        with pytest.raises(SessionActionError, match="Refusing to overwrite"):
            svc(app_config).restore(trash_id)
        assert existing.read_text() == "the live one"
        assert (batch / "rollout-abc123.jsonl").read_text() == '{"n": 1}\n'


class TestRestoreSuccess:
    def test_the_files_come_back_and_the_empty_trash_folders_go(self, app_config):
        trash_id, batch = make_entry(app_config)
        result = svc(app_config).restore(trash_id)
        destination = app_config.codex_root / "sessions" / "rollout-abc123.jsonl"
        assert result.restored_to == (destination,)
        assert destination.read_text() == '{"n": 1}\n'
        assert not batch.exists()
        assert not (app_config.trash_dir / OLD_STAMP).exists()
        assert app_config.trash_dir.exists()

    def test_a_trash_folder_shared_with_another_entry_is_kept(self, app_config):
        trash_id, _ = make_entry(app_config, sid="mine")
        other_id, other_batch = make_entry(app_config, sid="theirs")
        svc(app_config).restore(trash_id)
        assert other_batch.exists()
        assert [c.trash_id for c in svc(app_config).list_trash()] == [other_id]

    def test_a_multi_file_session_comes_back_whole(self, app_config):
        files = {"one.jsonl": "1", "two.jsonl": "2"}
        trash_id, _ = make_entry(app_config, files=files)
        result = svc(app_config).restore(trash_id)
        assert sorted(p.name for p in result.restored_to) == ["one.jsonl", "two.jsonl"]


class TestContinueIndexRestore:
    def entry(self, config, sid="cont1"):
        payload = {
            "sessionId": sid,
            "title": "T",
            "dateCreated": "1",
            "workspaceDirectory": "/w",
            "messageCount": 2,
        }
        return make_entry(
            config,
            provider="continue",
            sid=sid,
            files={f"{sid}.json": json.dumps(payload)},
            manifest={
                "version": 1,
                "provider": "continue",
                "session_id": sid,
                "moved_at": OLD_STAMP,
                "targets": [{"trash_path": f"{sid}.json", "provider_path": f"sessions/{sid}.json"}],
            },
        )

    def test_restoring_adds_the_session_back_to_the_index(self, app_config):
        trash_id, _ = self.entry(app_config)
        write_json(
            app_config.continue_root / "sessions" / "sessions.json", [{"sessionId": "other"}]
        )
        svc(app_config).restore(trash_id)
        index = json.loads((app_config.continue_root / "sessions" / "sessions.json").read_text())
        assert [i["sessionId"] for i in index] == ["other", "cont1"]
        assert index[1]["title"] == "T" and index[1]["messageCount"] == 2

    def test_an_index_that_already_lists_it_is_not_duplicated(self, app_config):
        trash_id, _ = self.entry(app_config)
        write_json(
            app_config.continue_root / "sessions" / "sessions.json", [{"sessionId": "cont1"}]
        )
        svc(app_config).restore(trash_id)
        index = json.loads((app_config.continue_root / "sessions" / "sessions.json").read_text())
        assert index == [{"sessionId": "cont1"}]

    def test_an_index_in_an_unexpected_format_is_reported_and_not_overwritten(self, app_config):
        trash_id, _ = self.entry(app_config)
        index_path = app_config.continue_root / "sessions" / "sessions.json"
        write_json(index_path, {"unexpected": True})
        with pytest.raises(SessionActionError, match="unsupported format"):
            svc(app_config).restore(trash_id)
        assert json.loads(index_path.read_text()) == {"unexpected": True}

    def test_a_missing_index_is_created(self, app_config):
        trash_id, _ = self.entry(app_config)
        svc(app_config).restore(trash_id)
        index = json.loads((app_config.continue_root / "sessions" / "sessions.json").read_text())
        assert [i["sessionId"] for i in index] == ["cont1"]

    def test_nothing_restored_means_nothing_to_index(self, tmp_path):
        actions._restore_continue_index_entry(tmp_path / "sessions.json", "x", [])
        assert not (tmp_path / "sessions.json").exists()

    def test_a_restored_file_that_is_not_an_object_leaves_the_index_alone(self, tmp_path):
        restored = tmp_path / "x.json"
        restored.write_text("[1, 2]", encoding="utf-8")
        actions._restore_continue_index_entry(tmp_path / "sessions.json", "x", [restored])
        assert not (tmp_path / "sessions.json").exists()


class TestDeleteRefusals:
    def test_a_session_that_looks_active_is_never_deleted(self, app_config):
        live = session(app_config, "live", active=True)
        with pytest.raises(SessionActionError, match="appears to be active"):
            svc(app_config)._delete_session(live, kill_windows=False)
        assert live.primary_path.exists()

    def test_a_target_outside_the_provider_root_is_refused_before_anything_moves(
        self, app_config, tmp_path
    ):
        outside = tmp_path / "elsewhere" / "precious.txt"
        outside.parent.mkdir()
        outside.write_text("keep", encoding="utf-8")
        mine = session(app_config, "esc")
        crafted = replace(mine, delete_targets=(mine.primary_path, outside))
        with pytest.raises(SessionActionError, match="outside provider root"):
            svc(app_config)._delete_session(crafted, kill_windows=False)
        assert outside.read_text() == "keep"
        assert mine.primary_path.exists()
        assert not app_config.trash_dir.exists()

    def test_a_session_whose_files_are_already_gone_says_so(self, app_config):
        gone = session(app_config, "gone")
        gone.primary_path.unlink()
        with pytest.raises(SessionActionError, match="Session files were not found"):
            svc(app_config)._delete_session(gone, kill_windows=False)

    def test_a_failure_while_moving_is_reported_and_the_cache_is_invalidated(
        self, app_config, monkeypatch
    ):
        target = session(app_config, "stuck")
        calls = []

        class Scanner:
            def scan(self, providers=None):
                return SimpleNamespace(sessions=())

            def invalidate(self):
                calls.append("invalidated")

        service = SessionActionService(app_config, Scanner())
        monkeypatch.setattr(
            actions.shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        )
        with pytest.raises(SessionActionError, match="Could not finish session removal"):
            service._delete_session(target, kill_windows=False)
        assert calls == ["invalidated"]

    def test_bulk_delete_reports_each_failure_and_still_deletes_the_rest(
        self, app_config, monkeypatch
    ):
        monkeypatch.setattr(actions, "_kill_tmux_windows_for_session", lambda *a, **k: 0)
        live = session(app_config, "live-one", active=True)
        fine = session(app_config, "fine-one")
        service = svc(app_config, live, fine)

        result = service.delete_many((live.public_id, fine.public_id, "no-such-id", live.public_id))

        assert [d.session.session_id for d in result.deleted] == ["fine-one"]
        assert len(result.errors) == 2
        assert any("no-such-id" in e and "not found" in e for e in result.errors)
        assert any(live.public_id in e and "appears to be active" in e for e in result.errors)
        assert not fine.primary_path.exists() and live.primary_path.exists()


class TestPruneValidationAndErrors:
    def test_a_non_positive_age_is_refused(self, app_config):
        for age in (timedelta(0), timedelta(days=-1)):
            with pytest.raises(SessionActionError, match="greater than zero"):
                svc(app_config).prune(age)

    @pytest.mark.parametrize("short", [timedelta(0), timedelta(days=-1), timedelta(days=40)])
    def test_the_short_subagent_age_must_be_positive_and_no_longer_than_the_prune_age(
        self, app_config, short
    ):
        with pytest.raises(SessionActionError, match="Short subagent age"):
            svc(app_config).prune(timedelta(days=30), short_subagent_age=short)

    def test_a_session_that_cannot_be_deleted_is_reported_and_pruning_continues(
        self, app_config, monkeypatch
    ):
        monkeypatch.setattr(actions, "_kill_tmux_windows_for_session", lambda *a, **k: 0)
        old = "2026-01-01T00:00:00Z"
        outside = (
            app_config.trash_dir.parent / "outside.txt"
        )  # exists, but not under the provider root
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("x", encoding="utf-8")
        bad = replace(session(app_config, "bad"), updated_at=old, created_at=old)
        bad = replace(bad, delete_targets=(bad.primary_path, outside))
        good = replace(session(app_config, "good"), updated_at=old, created_at=old)
        service = svc(app_config, bad, good)

        result = service.prune(timedelta(days=30), now=datetime(2026, 4, 10, tzinfo=timezone.utc))

        assert [d.session.session_id for d in result.deleted] == ["good"]
        assert len(result.errors) == 1
        assert result.errors[0].session.session_id == "bad"
        assert "outside provider root" in result.errors[0].message
        assert outside.read_text() == "x"


class TestRotate:
    def config(self, app_config, days=30):
        return replace(app_config, trash_retention_days=days)

    def test_only_entries_older_than_the_retention_period_expire(self, app_config):
        config = self.config(app_config)
        _, old_batch = make_entry(config, sid="expired", stamp=OLD_STAMP)
        _, fresh_batch = make_entry(config, sid="fresh", stamp=RECENT_STAMP)

        trash_count, _ = svc(config).rotate()

        assert trash_count == 1
        assert not old_batch.exists() and fresh_batch.exists()
        assert not (config.trash_dir / OLD_STAMP).exists()  # the emptied folder is tidied too

    def test_a_dry_run_counts_but_removes_nothing(self, app_config):
        config = self.config(app_config)
        _, old_batch = make_entry(config, sid="expired", stamp=OLD_STAMP)
        trash_count, _ = svc(config).rotate(dry_run=True)
        assert trash_count == 1 and old_batch.exists()

    def test_an_entry_whose_timestamp_is_not_a_timestamp_is_left_alone(self, app_config):
        config = self.config(app_config)
        _, batch = make_entry(config, sid="odd", stamp="not-a-timestamp")
        assert svc(config).rotate()[0] == 0
        assert batch.exists()

    def rotate_with_candidate_found_by_other_means(self, service, monkeypatch, stamp):
        # Whether list_trash can reach a symlinked folder depends on the Python version
        # (rglob stopped following symlinks in 3.13), so hand rotate the candidate directly:
        # the guard itself must hold whichever way the entry was found.
        candidate = actions.RestoreCandidate(
            trash_id=f"{stamp}/codex/linked",
            provider="codex",
            session_id="linked",
            moved_at=stamp,
            moved_count=1,
        )
        monkeypatch.setattr(service, "list_trash", lambda: (candidate,))
        return service.rotate()

    def test_a_symlinked_folder_that_leaves_the_trash_is_skipped_not_followed_and_not_fatal(
        self, app_config, tmp_path, monkeypatch
    ):
        config = self.config(app_config)
        real = tmp_path / "real-trash"
        make_entry(replace(config, trash_dir=real), sid="linked", stamp=OLD_STAMP)
        config.trash_dir.mkdir(parents=True)
        (config.trash_dir / OLD_STAMP).symlink_to(real / OLD_STAMP, target_is_directory=True)

        trash_count, _ = self.rotate_with_candidate_found_by_other_means(
            svc(config), monkeypatch, OLD_STAMP
        )

        assert trash_count == 0
        assert (real / OLD_STAMP / "codex" / "linked" / "rollout-abc123.jsonl").exists()

    def test_a_symlinked_folder_that_stays_inside_the_trash_is_also_skipped(
        self, app_config, monkeypatch
    ):
        config = self.config(app_config)
        _, real_batch = make_entry(config, sid="linked", stamp="20250101T000000Z")
        (config.trash_dir / OLD_STAMP).symlink_to(
            config.trash_dir / "20250101T000000Z", target_is_directory=True
        )

        trash_count, _ = self.rotate_with_candidate_found_by_other_means(
            svc(config), monkeypatch, OLD_STAMP
        )

        assert trash_count == 0
        assert (real_batch / "rollout-abc123.jsonl").exists()

    def test_one_suspect_entry_does_not_stop_the_others_from_rotating(
        self, app_config, tmp_path, monkeypatch
    ):
        config = self.config(app_config)
        real = tmp_path / "real-trash"
        make_entry(replace(config, trash_dir=real), sid="linked", stamp="20240101T000000Z")
        config.trash_dir.mkdir(parents=True)
        (config.trash_dir / "20240101T000000Z").symlink_to(
            real / "20240101T000000Z", target_is_directory=True
        )
        _, expired = make_entry(config, sid="expired", stamp=OLD_STAMP)
        service = svc(config)
        bad = actions.RestoreCandidate("20240101T000000Z/codex/linked", "codex", "linked", "x", 1)
        good = actions.RestoreCandidate(f"{OLD_STAMP}/codex/expired", "codex", "expired", "x", 1)
        monkeypatch.setattr(service, "list_trash", lambda: (bad, good))

        assert service.rotate()[0] == 1
        assert not expired.exists()
        assert (real / "20240101T000000Z" / "codex" / "linked" / "rollout-abc123.jsonl").exists()


class TestDatetimeHelper:
    @pytest.mark.parametrize("value", ["", "   ", "garbage", "2026-13-45T99:99:99"])
    def test_unreadable_times_are_none(self, value):
        assert actions._parse_datetime(value) is None

    def test_z_offset_and_naive_forms_are_read_as_utc(self):
        want = datetime(2026, 4, 8, 12, tzinfo=timezone.utc)
        assert actions._parse_datetime("2026-04-08T12:00:00Z") == want
        assert actions._parse_datetime("2026-04-08T14:00:00+02:00") == want
        assert actions._parse_datetime("2026-04-08T12:00:00") == want
