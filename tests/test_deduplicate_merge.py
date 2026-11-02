"""Merging duplicate sessions: what happens to the files and the indexes.

``deduplicate`` rewrites the newest session of each duplicate group and moves the older ones
to trash, so these tests check the data after the merge, not just that code ran. Every file
lives under a temporary directory; a stub scanner returns exactly the records under test.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_control import actions
from session_control.actions import SessionActionError, SessionActionService
from session_control.models import SessionRecord
from tests.helpers import write_json, write_jsonl

NOW = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
OLD, NEW = "2026-04-05T12:00:00Z", "2026-04-08T12:00:00Z"


class StubScanner:
    """Returns the records it was given, and remembers how it was asked."""

    def __init__(self, sessions):
        self.sessions = tuple(sessions)
        self.scan_calls: list[tuple[str, ...] | None] = []

    def scan(self, providers=None):
        self.scan_calls.append(providers)
        chosen = [s for s in self.sessions if providers is None or s.provider in providers]
        return SimpleNamespace(sessions=tuple(chosen))


def record(
    provider, sid, path, *, updated, targets=None, active=False, preview="Summarize this repo"
):
    return SessionRecord(
        provider=provider,
        session_id=sid,
        title=preview,
        workspace="/w/one",
        created_at=updated,
        updated_at=updated,
        primary_path=path,
        delete_targets=tuple(targets if targets is not None else (path,)),
        size_bytes=1,
        message_count=1,
        preview=preview,
        resume_command="",
        active=active,
    )


def turn(text):
    return {"message": {"role": "user", "content": text}}


def continue_session(config, sid, history, *, updated, active=False, preview="Summarize this repo"):
    sessions = config.continue_root / "sessions"
    path = sessions / f"{sid}.json"
    write_json(path, {"sessionId": sid, "title": preview, "history": history})
    index_path = sessions / "sessions.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else []
    index.append({"sessionId": sid, "title": preview, "messageCount": len(history)})
    write_json(index_path, index)
    return record("continue", sid, path, updated=updated, active=active, preview=preview)


def codex_session(config, sid, lines, *, updated, preview="Fix the downloader"):
    path = config.codex_root / "sessions" / "2026" / "04" / f"rollout-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return record("codex", sid, path, updated=updated, preview=preview)


def service(config, *sessions):
    scanner = StubScanner(sessions)
    return SessionActionService(config, scanner), scanner


def run(svc, **kwargs):
    kwargs.setdefault("min_age", DAY)
    kwargs.setdefault("max_age", 30 * DAY)
    kwargs.setdefault("now", NOW)
    return svc.deduplicate(**kwargs)


class TestArguments:
    @pytest.mark.parametrize(
        ("min_age", "max_age", "message"),
        [
            (timedelta(0), 5 * DAY, "greater than zero"),
            (DAY, timedelta(0), "greater than zero"),
            (-DAY, 5 * DAY, "greater than zero"),
            (5 * DAY, 5 * DAY, "less than max_age"),
            (6 * DAY, 5 * DAY, "less than max_age"),
        ],
    )
    def test_bad_age_bounds_are_refused_before_anything_is_scanned(
        self, app_config, min_age, max_age, message
    ):
        svc, scanner = service(app_config)
        with pytest.raises(SessionActionError, match=message):
            run(svc, min_age=min_age, max_age=max_age)
        assert scanner.scan_calls == []

    def test_the_provider_filter_reaches_the_scanner(self, app_config):
        svc, scanner = service(app_config)
        run(svc, providers=("continue",))
        assert scanner.scan_calls == [("continue",)]

    def test_a_naive_now_is_treated_as_utc(self, app_config):
        a = continue_session(app_config, "a", [turn("one")], updated=OLD)
        b = continue_session(app_config, "b", [turn("two")], updated=NEW)
        svc, _ = service(app_config, a, b)
        result = run(svc, now=NOW.replace(tzinfo=None), dry_run=True)
        assert len(result.groups) == 1


class TestDryRun:
    def test_a_dry_run_reports_the_groups_and_changes_nothing(self, app_config):
        old = continue_session(app_config, "old", [turn("one")], updated=OLD)
        new = continue_session(app_config, "new", [turn("two")], updated=NEW)
        before = {p: p.read_bytes() for p in (old.primary_path, new.primary_path)}
        svc, _ = service(app_config, old, new)

        result = run(svc, dry_run=True)

        assert result.dry_run is True
        assert result.scanned_count == 2
        assert [[s.session_id for s in g] for g in result.groups] == [["old", "new"]]
        assert result.merged == () and result.errors == ()
        assert {p: p.read_bytes() for p in before} == before
        assert not app_config.trash_dir.exists()


class TestContinueMerge:
    def make(self, config):
        old = continue_session(config, "old-session-aaaa", [turn("a1"), turn("a2")], updated=OLD)
        new = continue_session(config, "new-session-bbbb", [turn("b1")], updated=NEW)
        return old, new

    def test_the_newest_session_keeps_the_combined_history_in_order(self, app_config):
        old, new = self.make(app_config)
        svc, _ = service(app_config, new, old)  # the scanner's order must not matter

        result = run(svc)

        (merge,) = result.merged
        assert merge.kept.session_id == "new-session-bbbb"
        assert [s.session_id for s in merge.absorbed] == ["old-session-aaaa"]
        merged = json.loads(new.primary_path.read_text())
        assert [t["message"]["content"] for t in merged["history"]] == ["a1", "a2", "b1"]
        assert merged["messageCount"] == 3
        assert merged["sessionId"] == "new-session-bbbb"  # the newest one is the base

    def test_the_older_session_is_moved_to_trash_not_deleted(self, app_config):
        old, new = self.make(app_config)
        original = old.primary_path.read_bytes()
        svc, _ = service(app_config, old, new)

        (merge,) = run(svc).merged

        assert not old.primary_path.exists()
        moved = merge.moved_to / "old-session-" / old.primary_path.name
        assert moved.read_bytes() == original
        assert app_config.trash_dir in merge.moved_to.parents

    def test_the_index_loses_the_absorbed_session_and_keeps_the_rest(self, app_config):
        old, new = self.make(app_config)
        svc, _ = service(app_config, old, new)

        run(svc)

        index = json.loads((app_config.continue_root / "sessions" / "sessions.json").read_text())
        assert [item["sessionId"] for item in index] == ["new-session-bbbb"]

    def test_each_absorbed_session_leaves_a_digest_recording_the_merge(self, app_config):
        old, new = self.make(app_config)
        svc, _ = service(app_config, old, new)

        run(svc)

        digests = svc.digest_store.records()
        assert [d["reason"] for d in digests] == ["merge"]
        assert digests[0]["provider"] == "continue"

    def test_three_duplicates_collapse_into_the_newest(self, app_config):
        a = continue_session(app_config, "aaaa", [turn("1")], updated="2026-04-04T12:00:00Z")
        b = continue_session(app_config, "bbbb", [turn("2")], updated="2026-04-06T12:00:00Z")
        c = continue_session(app_config, "cccc", [turn("3")], updated="2026-04-08T12:00:00Z")
        svc, _ = service(app_config, b, c, a)

        (merge,) = run(svc).merged

        assert merge.kept.session_id == "cccc"
        assert sorted(s.session_id for s in merge.absorbed) == ["aaaa", "bbbb"]
        merged = json.loads(c.primary_path.read_text())
        assert [t["message"]["content"] for t in merged["history"]] == ["1", "2", "3"]
        assert not a.primary_path.exists() and not b.primary_path.exists()

    def test_dissimilar_sessions_are_left_alone(self, app_config):
        a = continue_session(
            app_config, "aaaa", [turn("1")], updated=OLD, preview="Summarize this repo"
        )
        b = continue_session(
            app_config, "bbbb", [turn("2")], updated=NEW, preview="Plan the garden layout"
        )
        before = (a.primary_path.read_bytes(), b.primary_path.read_bytes())
        svc, _ = service(app_config, a, b)

        result = run(svc)

        assert result.groups == () and result.merged == ()
        assert (a.primary_path.read_bytes(), b.primary_path.read_bytes()) == before


class TestRefusals:
    def test_a_group_with_an_active_session_is_refused_whole_and_reported(self, app_config):
        old = continue_session(app_config, "old-live", [turn("1")], updated=OLD, active=True)
        new = continue_session(app_config, "new-live", [turn("2")], updated=NEW)
        before = (old.primary_path.read_bytes(), new.primary_path.read_bytes())
        svc, _ = service(app_config, old, new)

        result = run(svc)

        assert result.merged == ()
        (error,) = result.errors
        assert "Refusing to merge a group that contains an active session" in error
        assert "old-live" in error[:40] or "new-live" in error[:40]
        assert (old.primary_path.read_bytes(), new.primary_path.read_bytes()) == before

    def test_one_refused_group_does_not_stop_the_other_groups(self, app_config):
        live_a = continue_session(
            app_config, "live-a", [turn("1")], updated=OLD, active=True, preview="Live topic one"
        )
        live_b = continue_session(
            app_config, "live-b", [turn("2")], updated=NEW, preview="Live topic one"
        )
        ok_a = continue_session(
            app_config, "okay-a", [turn("3")], updated=OLD, preview="Quiet topic two"
        )
        ok_b = continue_session(
            app_config, "okay-b", [turn("4")], updated=NEW, preview="Quiet topic two"
        )
        svc, _ = service(app_config, live_a, live_b, ok_a, ok_b)

        result = run(svc)

        assert len(result.errors) == 1 and len(result.merged) == 1
        assert result.merged[0].kept.session_id == "okay-b"
        assert live_a.primary_path.exists() and live_b.primary_path.exists()

    def test_nothing_outside_the_provider_root_is_ever_moved(self, app_config, tmp_path):
        outside = tmp_path / "elsewhere" / "precious.json"
        outside.parent.mkdir()
        outside.write_text("do not touch", encoding="utf-8")
        old = continue_session(app_config, "old-esc", [turn("1")], updated=OLD)
        old = record(
            "continue",
            "old-esc",
            old.primary_path,
            updated=OLD,
            targets=(old.primary_path, outside),
        )
        new = continue_session(app_config, "new-esc", [turn("2")], updated=NEW)
        svc, _ = service(app_config, old, new)

        result = run(svc)

        assert result.merged == ()
        assert "outside provider root" in result.errors[0]
        assert outside.read_text() == "do not touch"

    def test_an_unreadable_session_aborts_the_merge_before_any_file_changes(self, app_config):
        old = continue_session(app_config, "old-bad", [turn("1")], updated=OLD)
        new = continue_session(app_config, "new-bad", [turn("2")], updated=NEW)
        old.primary_path.write_text("{ not json", encoding="utf-8")
        newest_before = new.primary_path.read_bytes()
        svc, _ = service(app_config, old, new)

        result = run(svc)

        assert result.merged == ()
        assert "Could not read" in result.errors[0]
        assert new.primary_path.read_bytes() == newest_before
        assert old.primary_path.exists()

    def test_a_merge_that_cannot_write_the_kept_file_moves_nothing(self, app_config, monkeypatch):
        old = continue_session(app_config, "old-ro", [turn("1")], updated=OLD)
        new = continue_session(app_config, "new-ro", [turn("2")], updated=NEW)
        svc, _ = service(app_config, old, new)

        def refuse(self, *args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(Path, "write_text", refuse)
        result = run(svc)
        monkeypatch.undo()

        assert result.merged == ()
        assert "Could not write merged session" in result.errors[0]
        assert old.primary_path.exists()  # the older one is still where it was


class TestCodexMerge:
    def test_the_transcripts_are_concatenated_oldest_first_and_the_index_is_pruned(
        self, app_config
    ):
        old = codex_session(app_config, "old-codex", ['{"n": 1}', "", '{"n": 2}'], updated=OLD)
        new = codex_session(app_config, "new-codex", ['{"n": 3}'], updated=NEW)
        index = app_config.codex_root / "session_index.jsonl"
        write_jsonl(
            index,
            [{"id": "old-codex", "thread_name": "x"}, {"id": "new-codex", "thread_name": "y"}],
        )
        index.write_text(index.read_text() + "this line is not json\n", encoding="utf-8")
        svc, _ = service(app_config, new, old)

        (merge,) = run(svc).merged

        assert merge.kept.session_id == "new-codex"
        assert (
            new.primary_path.read_text() == '{"n": 1}\n{"n": 2}\n{"n": 3}\n'
        )  # blank line dropped
        assert not old.primary_path.exists()
        kept_index = index.read_text().splitlines()
        assert [json.loads(line)["id"] for line in kept_index if line.startswith("{")] == [
            "new-codex"
        ]
        assert "this line is not json" in kept_index  # an unreadable line is preserved, not lost


class TestCopilotMerge:
    def test_directories_are_not_content_merged_the_older_one_is_just_trashed(self, app_config):
        base = app_config.copilot_root / "session-state"
        old_dir, new_dir = base / "old-cop", base / "new-cop"
        for directory, text in ((old_dir, "old"), (new_dir, "new")):
            directory.mkdir(parents=True)
            (directory / "events.jsonl").write_text(text, encoding="utf-8")
        old = record("copilot", "old-cop", old_dir, updated=OLD)
        new = record("copilot", "new-cop", new_dir, updated=NEW)
        svc, _ = service(app_config, old, new)

        (merge,) = run(svc).merged

        assert (new_dir / "events.jsonl").read_text() == "new"  # untouched
        assert not old_dir.exists()
        assert (merge.moved_to / "old-cop" / "old-cop" / "events.jsonl").read_text() == "old"


class TestMergeHelpers:
    def test_merge_jsonl_reports_an_unreadable_file(self, app_config, tmp_path):
        ghost = record("claude", "ghost", tmp_path / "missing.jsonl", updated=OLD)
        with pytest.raises(SessionActionError, match="Could not read"):
            actions._merge_jsonl([ghost])

    def test_merge_continue_json_skips_non_objects_and_tolerates_no_history(self, app_config):
        base = app_config.continue_root / "sessions"
        listy = base / "list.json"
        write_json(listy, [1, 2, 3])
        bare = base / "bare.json"
        write_json(bare, {"sessionId": "bare", "title": "no history key"})
        merged = actions._merge_continue_json(
            [
                record("continue", "list", listy, updated=OLD),
                record("continue", "bare", bare, updated=NEW),
            ]
        )
        assert merged["history"] == [] and merged["messageCount"] == 0
        assert merged["sessionId"] == "bare"

    def test_merge_continue_json_reports_unparseable_json(self, app_config):
        broken = app_config.continue_root / "sessions" / "broken.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{nope", encoding="utf-8")
        with pytest.raises(SessionActionError, match="Could not read"):
            actions._merge_continue_json([record("continue", "broken", broken, updated=OLD)])


class TestIndexEntryRemoval:
    def test_a_missing_index_is_a_no_op(self, tmp_path):
        actions._remove_codex_index_entry(tmp_path / "none.jsonl", "x")
        actions._remove_continue_index_entry(tmp_path / "none.json", "x")
        assert list(tmp_path.iterdir()) == []

    def test_a_continue_index_that_is_not_a_list_is_left_exactly_as_it_was(self, tmp_path):
        index = tmp_path / "sessions.json"
        index.write_text('{"unexpected": "shape"}', encoding="utf-8")
        actions._remove_continue_index_entry(index, "x")
        assert index.read_text() == '{"unexpected": "shape"}'

    def test_a_corrupt_continue_index_is_reported_not_overwritten(self, tmp_path):
        index = tmp_path / "sessions.json"
        index.write_text("{corrupt", encoding="utf-8")
        with pytest.raises(SessionActionError, match="Could not read Continue session index"):
            actions._remove_continue_index_entry(index, "x")
        assert index.read_text() == "{corrupt"

    def test_a_continue_index_keeps_entries_that_are_not_objects(self, tmp_path):
        index = tmp_path / "sessions.json"
        write_json(index, [{"sessionId": "x"}, "stray", {"sessionId": "y"}])
        actions._remove_continue_index_entry(index, "x")
        assert json.loads(index.read_text()) == ["stray", {"sessionId": "y"}]
