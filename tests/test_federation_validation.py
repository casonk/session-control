"""Inventory exchange between machines: what is shared, and what is refused on the way in.

An inventory crosses a trust boundary in both directions. Going out it must carry no
transcript, path or command; coming in it is untrusted, so every field is validated and
normalised and a stored snapshot is written owner-only, atomically, and never read back
through a symlink.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from session_control import federation
from session_control.federation import (
    build_inventory,
    load_inventories,
    store_inventory,
    validate_inventory,
    validate_node_id,
)
from session_control.models import ScanError, ScanReport, SessionRecord


def session(**kwargs) -> SessionRecord:
    base = {
        "provider": "codex",
        "session_id": "abc",
        "title": "Fix   the\nthing",
        "workspace": "/home/me/projects/example/",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-02T00:00:00Z",
        "primary_path": Path("/secret/path.jsonl"),
        "delete_targets": (Path("/secret/other"),),
        "size_bytes": 10,
        "message_count": 3,
        "preview": "a private prompt",
        "resume_command": "codex resume abc --secret",
        "metadata": {"model": "gpt-x"},
    }
    return SessionRecord(**{**base, **kwargs})


def valid_payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "node": {"id": "air", "label": "Air"},
        "generated_at": "2026-04-02T00:00:00Z",
        "sessions": [
            {"id": "s1", "provider": "codex", "title": "T", "message_count": 1, "size_bytes": 2}
        ],
        "scan_error_count": 0,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- node ids


@pytest.mark.parametrize("value", ["air", "mac-book-1", "A1", " air ", "0", "a" * 63])
def test_valid_node_ids_are_lower_cased_and_trimmed(value):
    assert validate_node_id(value) == value.strip().lower()


@pytest.mark.parametrize(
    "value", ["", "   ", None, "-air", "air_1", "air.local", "a b", "../x", "air/x", "a" * 64, "é"]
)
def test_invalid_node_ids_are_refused(value):
    with pytest.raises(ValueError, match="node id"):
        validate_node_id(value)


# --------------------------------------------------------------------------- outgoing


def test_the_outgoing_inventory_carries_only_summary_fields():
    report = ScanReport(
        sessions=(session(),), errors=(ScanError("claude", "x"), ScanError("c", "y"))
    )
    inventory = build_inventory(report, node_id="Air", node_label="  My   Air  ")
    assert inventory["node"] == {"id": "air", "label": "My Air"}
    assert inventory["scan_error_count"] == 2
    assert inventory["schema_version"] == 1 and inventory["generated_at"].endswith("Z")
    summary = inventory["sessions"][0]
    assert summary["title"] == "Fix the thing"
    assert summary["workspace_label"] == "example"
    assert summary["model"] == "gpt-x"
    assert set(summary) == {
        "id", "provider", "title", "workspace_label", "created_at", "updated_at",
        "message_count", "size_bytes", "active", "is_subagent", "is_short_subagent", "model",
    }  # fmt: skip
    text = json.dumps(inventory)
    for private in ("/secret", "private prompt", "--secret", "/home/me"):
        assert private not in text


def test_the_public_session_id_is_stable_per_node_and_hides_the_real_one():
    report = ScanReport(sessions=(session(),))
    first = build_inventory(report, node_id="air", node_label="x")["sessions"][0]["id"]
    again = build_inventory(report, node_id="air", node_label="x")["sessions"][0]["id"]
    other = build_inventory(report, node_id="pro", node_label="x")["sessions"][0]["id"]
    assert first == again != other
    assert "abc" not in first and len(first) == 24


def test_negative_counts_are_clamped_and_a_blank_title_and_label_get_defaults():
    inventory = build_inventory(
        ScanReport(sessions=(session(title="  ", message_count=-4, size_bytes=-1, workspace=""),)),
        node_id="air",
        node_label="",
    )
    summary = inventory["sessions"][0]
    assert inventory["node"]["label"] == "air"
    assert summary["title"] == "Untitled session"
    assert (summary["message_count"], summary["size_bytes"]) == (0, 0)
    assert summary["workspace_label"] == ""


def test_an_invalid_local_node_id_stops_the_export():
    with pytest.raises(ValueError):
        build_inventory(ScanReport(sessions=()), node_id="bad id", node_label="x")


def test_long_titles_and_labels_are_cut():
    inventory = build_inventory(
        ScanReport(sessions=(session(title="t" * 1000),)), node_id="air", node_label="l" * 1000
    )
    assert len(inventory["sessions"][0]["title"]) == federation.MAX_TITLE_CHARS
    assert len(inventory["node"]["label"]) == federation.MAX_LABEL_CHARS


# --------------------------------------------------------------------------- incoming validation


def test_a_valid_payload_is_normalised():
    result = validate_inventory(valid_payload(node={"id": "AIR", "label": " The   Air "}))
    assert result["node"] == {"id": "air", "label": "The Air"}
    assert result["sessions"][0]["title"] == "T"
    assert result["sessions"][0]["workspace_label"] == ""
    assert result["sessions"][0]["active"] is False and result["scan_error_count"] == 0


def test_unknown_fields_in_a_payload_are_dropped():
    payload = valid_payload(extra="x")
    payload["sessions"][0]["preview"] = "leak"
    payload["sessions"][0]["primary_path"] = "/secret"
    result = validate_inventory(payload)
    assert "extra" not in result
    assert "preview" not in result["sessions"][0] and "primary_path" not in result["sessions"][0]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("text", "schema"),
        (None, "schema"),
        ([], "schema"),
        (valid_payload(schema_version=2), "schema"),
        ({k: v for k, v in valid_payload().items() if k != "schema_version"}, "schema"),
        (valid_payload(node=None), "node is missing"),
        (valid_payload(node="air"), "node is missing"),
        (valid_payload(node={"label": "x"}), "node id"),
        (valid_payload(node={"id": "bad id"}), "node id"),
        (valid_payload(sessions="x"), "sessions must be a list"),
        (valid_payload(sessions=None), "sessions must be a list"),
        (valid_payload(sessions=[{}] * 10001), "too many"),
        (valid_payload(sessions=["x"]), "session is invalid"),
        (valid_payload(sessions=[{"provider": "codex"}]), "identifier is missing"),
        (valid_payload(sessions=[{"id": "s"}]), "identifier is missing"),
        (valid_payload(scan_error_count=-1), "error count"),
        (valid_payload(scan_error_count="2"), "error count"),
        (valid_payload(generated_at=""), "generated_at"),
        (valid_payload(generated_at=None), "generated_at"),
    ],
)
def test_a_bad_payload_is_refused_with_a_reason(payload, message):
    with pytest.raises(ValueError, match=message):
        validate_inventory(payload)


def test_the_node_id_must_match_the_one_the_request_was_authenticated_for():
    assert validate_inventory(valid_payload(), expected_node_id="AIR")["node"]["id"] == "air"
    with pytest.raises(ValueError, match="does not match"):
        validate_inventory(valid_payload(), expected_node_id="pro")


def test_session_fields_are_coerced_defensively():
    payload = valid_payload(
        sessions=[
            {
                "id": "s" * 100,
                "provider": "p" * 100,
                "title": "x" * 500,
                "message_count": "7",
                "size_bytes": "many",
                "active": 1,
                "is_subagent": "yes",
                "is_short_subagent": True,
            }
        ]
    )
    item = validate_inventory(payload)["sessions"][0]
    assert len(item["id"]) == 64 and len(item["provider"]) == 32
    assert len(item["title"]) == federation.MAX_TITLE_CHARS
    assert (item["message_count"], item["size_bytes"]) == (7, 0)
    assert item["active"] is True
    # only a real boolean true counts as a subagent flag
    assert item["is_subagent"] is False and item["is_short_subagent"] is True


# --------------------------------------------------------------------------- storing and loading


def test_a_stored_snapshot_is_owner_only_and_leaves_no_temporary_file(tmp_path):
    store = tmp_path / "fed"
    store_inventory(store, valid_payload(), expected_node_id="air")
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE((store / "air.json").stat().st_mode) == 0o600
    assert sorted(p.name for p in store.iterdir()) == ["air.json"]


def test_storing_again_replaces_the_snapshot(tmp_path):
    store_inventory(tmp_path, valid_payload(generated_at="one"), expected_node_id="air")
    store_inventory(tmp_path, valid_payload(generated_at="two"), expected_node_id="air")
    assert json.loads((tmp_path / "air.json").read_text())["generated_at"] == "two"


def test_a_payload_for_another_node_is_refused_and_nothing_is_written(tmp_path):
    with pytest.raises(ValueError, match="does not match"):
        store_inventory(tmp_path / "fed", valid_payload(), expected_node_id="pro")
    assert not (tmp_path / "fed").exists()


def test_a_failed_replace_leaves_the_old_snapshot_and_removes_the_temporary_file(tmp_path):
    store_inventory(tmp_path, valid_payload(generated_at="old"), expected_node_id="air")
    with (
        mock.patch.object(federation.os, "replace", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        store_inventory(tmp_path, valid_payload(generated_at="new"), expected_node_id="air")
    assert json.loads((tmp_path / "air.json").read_text())["generated_at"] == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["air.json"]


def test_a_directory_that_cannot_be_tightened_does_not_stop_the_store(tmp_path):
    with mock.patch.object(federation.os, "chmod", side_effect=[OSError("no"), None]):
        store_inventory(tmp_path / "fed", valid_payload(), expected_node_id="air")
    assert (tmp_path / "fed" / "air.json").exists()


def test_loading_a_missing_or_unset_directory_is_empty(tmp_path):
    assert load_inventories(None) == ([], [])
    assert load_inventories(tmp_path / "absent") == ([], [])


def test_loading_sorts_by_label_and_reports_bad_files_by_name_only(tmp_path):
    store_inventory(
        tmp_path, valid_payload(node={"id": "zed", "label": "alpha"}), expected_node_id="zed"
    )
    store_inventory(
        tmp_path, valid_payload(node={"id": "amy", "label": "Beta"}), expected_node_id="amy"
    )
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "wrong.json").write_text(json.dumps({"schema_version": 9}), encoding="utf-8")
    (tmp_path / "dir.json").mkdir()
    (tmp_path / "ignored.txt").write_text("x", encoding="utf-8")
    inventories, errors = load_inventories(tmp_path)
    assert [i["node"]["label"] for i in inventories] == ["alpha", "Beta"]
    assert sorted(errors) == [
        "Ignored invalid remote inventory broken.json.",
        "Ignored invalid remote inventory dir.json.",
        "Ignored invalid remote inventory wrong.json.",
    ]


def test_a_symlinked_snapshot_is_never_followed(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(valid_payload()), encoding="utf-8")
    store = tmp_path / "fed"
    store.mkdir()
    (store / "link.json").symlink_to(outside)
    inventories, errors = load_inventories(store)
    assert inventories == [] and errors == ["Ignored invalid remote inventory link.json."]


def test_an_unlistable_directory_is_one_error_not_a_crash(tmp_path):
    with mock.patch.object(Path, "glob", side_effect=OSError("denied")):
        assert load_inventories(tmp_path) == ([], ["Could not list remote inventories."])


def test_the_os_level_file_mode_is_requested_at_creation(tmp_path):
    previous = os.umask(0)
    try:
        with mock.patch.object(federation.os, "chmod"):
            store_inventory(tmp_path / "fed", valid_payload(), expected_node_id="air")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "fed" / "air.json").stat().st_mode) == 0o600
