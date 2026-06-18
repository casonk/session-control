"""Periodic Claude CLI status polling."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections.abc import Callable
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

_USAGE_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)%\s+used", re.IGNORECASE)
_RESET_RE = re.compile(
    r"resets\s+(\w+)\s+(\d+),\s+(\d+)(?::(\d+))?(am|pm)\s+\(([^)]+)\)",
    re.IGNORECASE,
)


class ClaudeStatusPoller:
    def __init__(
        self,
        command: tuple[str, ...],
        interval_seconds: int,
        timeout_seconds: int,
        session_finder: Callable[[], str | None] | None = None,
    ):
        self.command = command
        self.interval_seconds = max(1, interval_seconds)
        self.timeout_seconds = max(1, timeout_seconds)
        self.session_finder = session_finder
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
        if self.session_finder:
            session_id = self.session_finder()
            if session_id:
                usage_limits = collect_claude_usage(session_id, self.timeout_seconds)
                if usage_limits.get("ok"):
                    status["limits_available"] = True
                    status["usage_limits"] = usage_limits
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


def collect_claude_usage(session_id: str, timeout_seconds: int) -> dict[str, Any]:
    """Run /usage against a Claude session and return parsed rate-limit data."""
    checked_at = _now_iso()
    command = (
        "claude",
        "--resume",
        session_id,
        "--print",
        "/usage",
        "--output-format",
        "json",
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=max(1, timeout_seconds),
        )
    except OSError as exc:
        return _error_usage(checked_at, f"claude /usage failed: {exc}")
    except subprocess.TimeoutExpired:
        return _error_usage(checked_at, "claude /usage timed out.")
    if completed.returncode:
        return _error_usage(checked_at, "claude /usage exited with error.")
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return _error_usage(checked_at, "claude /usage did not return JSON.")
    if not isinstance(data, dict):
        return _error_usage(checked_at, "claude /usage returned non-object JSON.")
    result_text = str(data.get("result") or "")
    return _parse_usage_text(checked_at, result_text)


def _parse_usage_text(checked_at: str, text: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    windows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        m_pct = _USAGE_LINE_RE.search(line)
        if not m_pct:
            continue
        pct = float(m_pct.group(1))
        resets_at = _parse_reset_time(line, now)
        windows.append({"used_percent": pct, "resets_at": resets_at})
    if not windows:
        return _error_usage(checked_at, "No usage data found in /usage output.")
    return {
        "checked_at": checked_at,
        "ok": True,
        "limits_available": True,
        "primary": windows[0],
        "secondary": windows[1] if len(windows) > 1 else None,
    }


def _parse_reset_time(line: str, reference: datetime) -> str:
    m = _RESET_RE.search(line)
    if not m:
        return ""
    month_str, day_str, hour_str, minute_str, ampm, tz_name = m.groups()
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        return ""
    try:
        month = datetime.strptime(month_str, "%b").month
    except ValueError:
        return ""
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    elif ampm.lower() == "am" and hour == 12:
        hour = 0
    year = reference.year
    for candidate_year in (year, year + 1):
        try:
            dt = datetime(candidate_year, month, int(day_str), hour, minute, tzinfo=tz)
            if dt >= reference:
                return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError):
            continue
    return ""


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


def _error_usage(checked_at: str, message: str) -> dict[str, Any]:
    return {
        "checked_at": checked_at,
        "ok": False,
        "message": message,
        "limits_available": False,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate_line(value: str) -> str:
    line = value.splitlines()[0] if value else ""
    return line[:180] if line else "Claude status command failed."
