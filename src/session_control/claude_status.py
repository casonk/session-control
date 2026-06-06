"""Periodic Claude CLI status polling."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any

LIMIT_KEY_MARKERS = ("limit", "quota", "usage", "remaining", "reset")
SENSITIVE_KEY_MARKERS = (
    "account",
    "credential",
    "email",
    "key",
    "org",
    "secret",
    "token",
)


class ClaudeStatusPoller:
    def __init__(
        self,
        command: tuple[str, ...],
        interval_seconds: int,
        timeout_seconds: int,
    ):
        self.command = command
        self.interval_seconds = max(1, interval_seconds)
        self.timeout_seconds = max(1, timeout_seconds)
        self._lock = threading.Lock()
        self._status: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="claude-status-poller", daemon=True)
        self._thread.start()

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._status) if self._status else None

    def refresh(self) -> dict[str, Any]:
        status = collect_claude_status(self.command, self.timeout_seconds)
        with self._lock:
            self._status = status
        return status

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval_seconds)


def collect_claude_status(command: tuple[str, ...], timeout_seconds: int) -> dict[str, Any]:
    checked_at = _now_iso()
    if not command:
        return {
            "checked_at": checked_at,
            "ok": False,
            "message": "Claude status command is empty.",
            "limits_available": False,
        }
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=max(1, timeout_seconds),
        )
    except OSError as exc:
        return _error_status(checked_at, f"Claude status command failed: {exc}")
    except subprocess.TimeoutExpired:
        return _error_status(checked_at, "Claude status command timed out.")
    if completed.returncode:
        return _error_status(
            checked_at,
            _truncate_line(completed.stderr or completed.stdout or "Claude status command failed."),
        )
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return _error_status(checked_at, "Claude status command did not return JSON.")
    if not isinstance(data, dict):
        return _error_status(checked_at, "Claude status command returned non-object JSON.")
    limits = _extract_limit_sections(data)
    status = {
        "checked_at": checked_at,
        "ok": True,
        "logged_in": bool(data.get("loggedIn")),
        "auth_method": str(data.get("authMethod") or ""),
        "api_provider": str(data.get("apiProvider") or ""),
        "subscription_type": str(data.get("subscriptionType") or ""),
        "limits_available": bool(limits),
        "limits": limits,
        "message": (
            "Claude CLI exposed limit or usage fields."
            if limits
            else "Claude CLI auth status does not expose usage or limit windows."
        ),
    }
    return status


def _extract_limit_sections(data: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in data.items():
        normalized = key.lower()
        if not any(marker in normalized for marker in LIMIT_KEY_MARKERS):
            continue
        sanitized = _sanitize_value(value)
        if sanitized not in ({}, [], "", None):
            result[str(key)] = sanitized
    return result


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, raw in value.items():
            normalized = str(key).lower()
            if any(marker in normalized for marker in SENSITIVE_KEY_MARKERS):
                continue
            sanitized = _sanitize_value(raw)
            if sanitized not in ({}, [], "", None):
                result[str(key)] = sanitized
        return result
    if isinstance(value, list):
        return [item for item in (_sanitize_value(item) for item in value) if item not in ({}, [])]
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:160]
    return None


def _error_status(checked_at: str, message: str) -> dict[str, Any]:
    return {
        "checked_at": checked_at,
        "ok": False,
        "message": message,
        "limits_available": False,
        "limits": {},
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate_line(value: str) -> str:
    line = value.splitlines()[0] if value else ""
    return line[:180] if line else "Claude status command failed."
