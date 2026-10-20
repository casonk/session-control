"""Read-only, metadata-only inventory exchange between trusted machines."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from session_control.models import ScanReport, SessionRecord

SCHEMA_VERSION = 1
NODE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
MAX_TITLE_CHARS = 240
MAX_LABEL_CHARS = 120


def build_inventory(report: ScanReport, *, node_id: str, node_label: str) -> dict[str, Any]:
    """Build the deliberately restricted payload safe to share with a peer."""
    node_id = validate_node_id(node_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "node": {"id": node_id, "label": _clean_text(node_label, MAX_LABEL_CHARS) or node_id},
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sessions": [_session_summary(session, node_id) for session in report.sessions],
        "scan_error_count": len(report.errors),
    }


def validate_node_id(value: str) -> str:
    candidate = str(value or "").strip().lower()
    if not NODE_ID_RE.fullmatch(candidate):
        raise ValueError("node id must contain lowercase letters, numbers, and hyphens only")
    return candidate


def validate_inventory(payload: Any, *, expected_node_id: str | None = None) -> dict[str, Any]:
    """Validate and normalize an untrusted incoming inventory payload."""
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported inventory schema")
    node = payload.get("node")
    if not isinstance(node, dict):
        raise ValueError("inventory node is missing")
    node_id = validate_node_id(str(node.get("id") or ""))
    if expected_node_id and node_id != validate_node_id(expected_node_id):
        raise ValueError("inventory node id does not match request path")
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("inventory sessions must be a list")
    if len(sessions) > 10000:
        raise ValueError("inventory has too many sessions")
    normalized = [_validate_session(item) for item in sessions]
    error_count = payload.get("scan_error_count", 0)
    if not isinstance(error_count, int) or error_count < 0:
        raise ValueError("inventory scan error count is invalid")
    generated_at = _clean_text(payload.get("generated_at"), 64)
    if not generated_at:
        raise ValueError("inventory generated_at is missing")
    return {
        "schema_version": SCHEMA_VERSION,
        "node": {
            "id": node_id,
            "label": _clean_text(node.get("label"), MAX_LABEL_CHARS) or node_id,
        },
        "generated_at": generated_at,
        "sessions": normalized,
        "scan_error_count": error_count,
    }


def load_inventories(directory: Path | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Load previously validated remote snapshots; never follow symlinks."""
    if directory is None or not directory.exists():
        return [], []
    inventories: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return [], ["Could not list remote inventories."]
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("not a regular file")
            payload = json.loads(path.read_text(encoding="utf-8"))
            inventories.append(validate_inventory(payload))
        except (OSError, ValueError, json.JSONDecodeError):
            errors.append(f"Ignored invalid remote inventory {path.name}.")
    inventories.sort(key=lambda item: str(item["node"]["label"]).lower())
    return inventories, errors


def store_inventory(directory: Path, payload: Any, *, expected_node_id: str) -> dict[str, Any]:
    """Atomically store one validated remote snapshot with restrictive permissions."""
    inventory = validate_inventory(payload, expected_node_id=expected_node_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    destination = directory / f"{inventory['node']['id']}.json"
    temporary = directory / f".{inventory['node']['id']}.json.tmp"
    encoded = (json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return inventory


def _session_summary(session: SessionRecord, node_id: str) -> dict[str, Any]:
    """Omit transcript, prompt preview, paths, delete targets, and resume command."""
    return {
        "id": _public_session_id(node_id, session),
        "provider": _clean_text(session.provider, 32),
        "title": _clean_text(session.title, MAX_TITLE_CHARS) or "Untitled session",
        "workspace_label": _workspace_label(session.workspace),
        "created_at": _clean_text(session.created_at, 64),
        "updated_at": _clean_text(session.updated_at, 64),
        "message_count": max(0, int(session.message_count)),
        "size_bytes": max(0, int(session.size_bytes)),
        "active": bool(session.active),
        "is_subagent": session.is_subagent,
        "is_short_subagent": session.is_short_subagent,
        "model": _clean_text(session.metadata.get("model"), 120),
    }


def _validate_session(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("inventory session is invalid")
    session_id = _clean_text(value.get("id"), 64)
    provider = _clean_text(value.get("provider"), 32)
    if not session_id or not provider:
        raise ValueError("inventory session identifier is missing")
    return {
        "id": session_id,
        "provider": provider,
        "title": _clean_text(value.get("title"), MAX_TITLE_CHARS) or "Untitled session",
        "workspace_label": _clean_text(value.get("workspace_label"), MAX_LABEL_CHARS),
        "created_at": _clean_text(value.get("created_at"), 64),
        "updated_at": _clean_text(value.get("updated_at"), 64),
        "message_count": _nonnegative_int(value.get("message_count")),
        "size_bytes": _nonnegative_int(value.get("size_bytes")),
        "active": bool(value.get("active")),
        "is_subagent": value.get("is_subagent") is True,
        "is_short_subagent": value.get("is_short_subagent") is True,
        "model": _clean_text(value.get("model"), 120),
    }


def _public_session_id(node_id: str, session: SessionRecord) -> str:
    raw = f"{node_id}\0{session.provider}\0{session.session_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _workspace_label(value: str) -> str:
    path = Path(str(value or "").rstrip("/"))
    return _clean_text(path.name, MAX_LABEL_CHARS)


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
