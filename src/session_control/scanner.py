"""Session discovery for local AI assistant tools."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from session_control.codex_permissions import (
    codex_permission_args,
    codex_permission_summary,
    normalize_codex_permission_preset,
)
from session_control.config import AppConfig
from session_control.models import ScanError, ScanReport, SessionRecord

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

PROVIDERS = ("codex", "claude", "continue", "copilot")
CLAUDE_TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


class SessionScanner:
    def __init__(self, config: AppConfig):
        self.config = config

    def scan(self, providers: tuple[str, ...] | None = None) -> ScanReport:
        selected = providers or PROVIDERS
        sessions: list[SessionRecord] = []
        errors: list[ScanError] = []
        scanners = {
            "codex": self._scan_codex,
            "claude": self._scan_claude,
            "continue": self._scan_continue,
            "copilot": self._scan_copilot,
        }
        for provider in selected:
            if provider not in scanners:
                errors.append(ScanError(provider, "Unknown provider."))
                continue
            try:
                sessions.extend(scanners[provider]())
            except OSError as exc:
                errors.append(ScanError(provider, str(exc)))
            except ValueError as exc:
                errors.append(ScanError(provider, str(exc)))
        sessions.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return ScanReport(tuple(sessions), tuple(errors))

    def _scan_codex(self) -> list[SessionRecord]:
        root = self.config.codex_root
        index = _read_codex_index(root / "session_index.jsonl")
        records: list[SessionRecord] = []
        for base in (root / "sessions", root / "archived_sessions"):
            if not base.exists():
                continue
            for path in base.rglob("*.jsonl"):
                record = _codex_record(
                    path,
                    index,
                    self.config.max_preview_chars,
                    self.config.codex_resume_model,
                    self.config.codex_permission_preset,
                )
                if record:
                    records.append(record)
        return records

    def _scan_claude(self) -> list[SessionRecord]:
        root = self.config.claude_root / "projects"
        if not root.exists():
            return []
        records = []
        for path in root.rglob("*.jsonl"):
            record = _claude_record(path, self.config.max_preview_chars)
            if record:
                records.append(record)
        return records

    def _scan_continue(self) -> list[SessionRecord]:
        root = self.config.continue_root / "sessions"
        if not root.exists():
            return []
        summaries = _read_json(root / "sessions.json")
        summary_by_id = {}
        if isinstance(summaries, list):
            summary_by_id = {
                str(item.get("sessionId")): item for item in summaries if isinstance(item, dict)
            }
        records = []
        for path in root.glob("*.json"):
            if path.name == "sessions.json":
                continue
            record = _continue_record(path, summary_by_id, self.config.max_preview_chars)
            if record:
                records.append(record)
        return records

    def _scan_copilot(self) -> list[SessionRecord]:
        root = self.config.copilot_root / "session-state"
        if not root.exists():
            return []
        records = []
        for session_dir in root.iterdir():
            if not session_dir.is_dir():
                continue
            record = _copilot_record(session_dir, self.config.max_preview_chars)
            if record:
                records.append(record)
        return records


def _codex_record(
    path: Path,
    index: dict[str, dict[str, Any]],
    max_preview_chars: int,
    codex_resume_model: str | None,
    codex_permission_preset: str,
) -> SessionRecord | None:
    metadata: dict[str, Any] = {}
    user_texts: list[str] = []
    message_count = 0
    latest_timestamp = ""

    for item in _iter_jsonl(path):
        timestamp = _normalize_datetime(str(item.get("timestamp") or ""))
        latest_timestamp = timestamp or latest_timestamp
        if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
            payload = item["payload"]
            metadata.update(
                {
                    "originator": payload.get("originator") or "",
                    "agent_role": payload.get("agent_role") or "",
                    "agent_nickname": payload.get("agent_nickname") or "",
                    "model_provider": payload.get("model_provider") or "",
                    "model": payload.get("model") or metadata.get("model") or "",
                }
            )
            metadata["session_id"] = str(payload.get("id") or "")
            metadata["created_at"] = _normalize_datetime(str(payload.get("timestamp") or ""))
            metadata["workspace"] = str(payload.get("cwd") or "")
            continue
        if item.get("type") == "turn_context" and isinstance(item.get("payload"), dict):
            payload = item["payload"]
            model = str(payload.get("model") or "")
            if model:
                metadata["model"] = model
            approval_policy = str(payload.get("approval_policy") or "")
            if approval_policy:
                metadata["approval_policy"] = approval_policy
            sandbox_mode = _codex_sandbox_mode(payload.get("sandbox_policy"))
            if sandbox_mode:
                metadata["sandbox_mode"] = sandbox_mode
            continue
        if item.get("type") == "event_msg" and isinstance(item.get("payload"), dict):
            payload = item["payload"]
            if payload.get("type") == "token_count":
                metadata["token_usage"] = _token_usage_metadata(payload.get("info"), timestamp)
                metadata["rate_limits"] = _rate_limits_metadata(payload.get("rate_limits"))
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role in {"user", "assistant"}:
            message_count += 1
        if role == "user":
            user_texts.append(_extract_text(payload.get("content")))

    session_id = metadata.get("session_id") or _uuid_from_text(path.name)
    if not session_id:
        return None
    first_user = _best_user_text(user_texts)
    indexed = index.get(session_id, {})
    title = indexed.get("thread_name") or _title_from_text(first_user) or path.stem
    created_at = metadata.get("created_at") or _mtime_iso(path)
    updated_at = (
        _normalize_datetime(str(indexed.get("updated_at") or ""))
        or latest_timestamp
        or _mtime_iso(path)
    )
    workspace = str(metadata.get("workspace") or "")
    resume_model = codex_resume_model or str(metadata.get("model") or "")
    if resume_model:
        metadata["resume_model"] = resume_model
    metadata["permission_summary"] = codex_permission_summary(metadata)
    normalized_permission_preset = normalize_codex_permission_preset(codex_permission_preset)
    metadata["resume_permission_preset"] = normalized_permission_preset
    return SessionRecord(
        provider="codex",
        session_id=session_id,
        title=title,
        workspace=workspace,
        created_at=created_at,
        updated_at=updated_at,
        primary_path=path,
        delete_targets=(path,),
        size_bytes=_path_size(path),
        message_count=message_count,
        preview=_truncate(_clean_text(first_user), max_preview_chars),
        resume_command=_command(
            workspace,
            _codex_resume_args(
                session_id,
                resume_model,
                codex_permission_args(normalized_permission_preset, metadata),
            ),
        ),
        metadata=metadata,
    )


def _claude_record(path: Path, max_preview_chars: int) -> SessionRecord | None:
    session_id = ""
    workspace = ""
    created_at = ""
    latest_timestamp = ""
    user_texts: list[str] = []
    message_count = 0
    permission_mode = ""
    model = ""
    latest_usage_at = ""
    last_usage: dict[str, int] = {}
    total_usage = dict.fromkeys(CLAUDE_TOKEN_FIELDS, 0)
    service_tier = ""
    speed = ""

    for item in _iter_jsonl(path):
        timestamp = _normalize_datetime(str(item.get("timestamp") or ""))
        if timestamp:
            created_at = created_at or timestamp
            latest_timestamp = timestamp
        if item.get("type") == "permission-mode":
            permission_mode = str(item.get("permissionMode") or "")
            session_id = session_id or str(item.get("sessionId") or "")
            continue
        item_session_id = str(item.get("sessionId") or "")
        if item_session_id:
            session_id = session_id or item_session_id
        item_cwd = str(item.get("cwd") or "")
        if item_cwd:
            workspace = workspace or item_cwd
        role = str(item.get("type") or "")
        if role in {"user", "assistant"}:
            message_count += 1
        if role == "assistant":
            message = item.get("message")
            if isinstance(message, dict):
                model = str(message.get("model") or model)
                usage = message.get("usage")
                if isinstance(usage, dict):
                    parsed_usage = _int_dict(usage)
                    if parsed_usage:
                        latest_usage_at = timestamp or latest_usage_at
                        last_usage = parsed_usage
                        for field in CLAUDE_TOKEN_FIELDS:
                            total_usage[field] += parsed_usage.get(field, 0)
                    service_tier = str(usage.get("service_tier") or service_tier)
                    speed = str(usage.get("speed") or speed)
        if role == "user":
            message = item.get("message")
            if isinstance(message, dict):
                user_texts.append(_extract_text(message.get("content")))

    session_id = session_id or _uuid_from_text(path.name)
    if not session_id:
        return None
    first_user = _best_user_text(user_texts)
    title = _title_from_text(first_user) or session_id
    workspace = workspace or _workspace_from_claude_project(path)
    metadata: dict[str, Any] = {"permission_mode": permission_mode}
    if model:
        metadata["model"] = model
    if last_usage:
        metadata["token_usage"] = {
            "updated_at": latest_usage_at or latest_timestamp,
            "last": last_usage,
            "total": {key: value for key, value in total_usage.items() if value},
            "model": model,
            "service_tier": service_tier,
            "speed": speed,
            "limits_available": False,
            "limits_note": (
                "Claude Code local session files record token usage, but not plan "
                "limit windows or reset percentages."
            ),
        }
    return SessionRecord(
        provider="claude",
        session_id=session_id,
        title=title,
        workspace=workspace,
        created_at=created_at or _mtime_iso(path),
        updated_at=latest_timestamp or _mtime_iso(path),
        primary_path=path,
        delete_targets=(path,),
        size_bytes=_path_size(path),
        message_count=message_count,
        preview=_truncate(_clean_text(first_user), max_preview_chars),
        resume_command=_command(workspace, ["claude", "--resume", session_id]),
        metadata=metadata,
    )


def _continue_record(
    path: Path,
    summary_by_id: dict[str, dict[str, Any]],
    max_preview_chars: int,
) -> SessionRecord | None:
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    session_id = str(data.get("sessionId") or _uuid_from_text(path.name) or "")
    if not session_id:
        return None
    summary = summary_by_id.get(session_id, {})
    title = str(data.get("title") or summary.get("title") or session_id)
    workspace = _uri_to_path(
        str(data.get("workspaceDirectory") or summary.get("workspaceDirectory") or "")
    )
    created_at = _millis_to_iso(
        data.get("dateCreated") or summary.get("dateCreated")
    ) or _mtime_iso(path)
    history = data.get("history")
    message_count = 0
    user_texts: list[str] = []
    if isinstance(history, list):
        for turn in history:
            if not isinstance(turn, dict):
                continue
            message = turn.get("message")
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role in {"user", "assistant"}:
                message_count += 1
            if role == "user":
                user_texts.append(_extract_text(message.get("content")))
    message_count = int(data.get("messageCount") or summary.get("messageCount") or message_count)
    first_user = _best_user_text(user_texts)
    return SessionRecord(
        provider="continue",
        session_id=session_id,
        title=title,
        workspace=workspace,
        created_at=created_at,
        updated_at=_mtime_iso(path),
        primary_path=path,
        delete_targets=(path,),
        size_bytes=_path_size(path),
        message_count=message_count,
        preview=_truncate(_clean_text(first_user), max_preview_chars),
        resume_command=_command(workspace, ["cn", "--fork", session_id]),
        metadata={"mode": data.get("mode") or "", "model": data.get("chatModelTitle") or ""},
    )


def _copilot_record(session_dir: Path, max_preview_chars: int) -> SessionRecord | None:
    workspace_file = session_dir / "workspace.yaml"
    if not workspace_file.exists():
        return None
    workspace_data = _read_simple_yaml(workspace_file)
    session_id = workspace_data.get("id") or session_dir.name
    workspace = workspace_data.get("cwd") or ""
    created_at = _normalize_datetime(workspace_data.get("created_at") or "") or _mtime_iso(
        session_dir
    )
    updated_at = _normalize_datetime(workspace_data.get("updated_at") or "") or _mtime_iso(
        session_dir
    )
    title = ""
    preview = ""
    message_count = 0

    events = session_dir / "events.jsonl"
    if events.exists():
        for item in _iter_jsonl(events):
            timestamp = _normalize_datetime(str(item.get("timestamp") or ""))
            updated_at = timestamp or updated_at
            event_type = str(item.get("type") or "")
            data = item.get("data")
            if event_type == "session.start" and isinstance(data, dict):
                context = data.get("context")
                if isinstance(context, dict):
                    workspace = workspace or str(context.get("cwd") or "")
                created_at = _normalize_datetime(str(data.get("startTime") or "")) or created_at
            if event_type == "user.message" and isinstance(data, dict):
                message_count += 1
                content = _extract_text(data.get("content"))
                preview = preview or content
                title = title or _title_from_text(content)
            if event_type == "assistant.message":
                message_count += 1

    checkpoint_title = _first_checkpoint_title(session_dir / "checkpoints" / "index.md")
    plan_title = _first_markdown_heading(session_dir / "plan.md")
    title = title or checkpoint_title or plan_title or session_id
    active = _has_live_lock(session_dir)
    return SessionRecord(
        provider="copilot",
        session_id=session_id,
        title=title,
        workspace=workspace,
        created_at=created_at,
        updated_at=updated_at,
        primary_path=session_dir,
        delete_targets=(session_dir,),
        size_bytes=_path_size(session_dir),
        message_count=message_count,
        preview=_truncate(_clean_text(preview or plan_title), max_preview_chars),
        resume_command=_command(workspace, ["copilot", f"--resume={session_id}"]),
        active=active,
        metadata={"summary_count": workspace_data.get("summary_count") or ""},
    )


def _read_codex_index(path: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return index
    for item in _iter_jsonl(path):
        session_id = str(item.get("id") or "")
        if session_id:
            index[session_id] = item
    return index


def _iter_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _read_simple_yaml(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        return values
    return values


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_extract_text(item) for item in value) if part)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            text = _extract_text(value.get(key))
            if text:
                return text
    return ""


def _best_user_text(values: list[str]) -> str:
    cleaned = [_clean_text(value) for value in values if _clean_text(value)]
    for value in cleaned:
        if not _is_bootstrap_text(value):
            return value
    return cleaned[0] if cleaned else ""


def _is_bootstrap_text(value: str) -> bool:
    lowered = value.lower()
    bootstrap_markers = (
        "# agents.md instructions",
        "<environment_context>",
        "available skills",
        "caveat: the messages below were generated",
    )
    return any(marker in lowered for marker in bootstrap_markers)


def _title_from_text(text: str) -> str:
    text = _clean_text(text)
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    first_line = re.sub(r"^[#>*\-\s]+", "", first_line)
    return _truncate(first_line, 72)


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _normalize_datetime(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _millis_to_iso(value: Any) -> str:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return ""
    seconds = millis / 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _mtime_iso(path: Path) -> str:
    try:
        if path.is_dir():
            mtime = max(child.stat().st_mtime for child in path.rglob("*") if child.exists())
        else:
            mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return ""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
    except OSError:
        return 0


def _uuid_from_text(value: str) -> str:
    match = UUID_RE.search(value)
    return match.group(0) if match else ""


def _workspace_from_claude_project(path: Path) -> str:
    project_dir = path.parent.name
    if not project_dir.startswith("-"):
        return ""
    return "/" + project_dir[1:].replace("-", "/")


def _uri_to_path(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    return value


def _command(workspace: str, args: list[str]) -> str:
    command = " ".join(shlex.quote(part) for part in args)
    if workspace:
        return f"cd {shlex.quote(workspace)} && {command}"
    return command


def _codex_resume_args(
    session_id: str, model: str, permission_args: list[str] | None = None
) -> list[str]:
    args = ["codex", "resume"]
    if model:
        args.extend(["--model", model])
    args.extend(permission_args or [])
    args.append(session_id)
    return args


def _codex_sandbox_mode(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("type") or value.get("kind") or "")
    return str(value or "")


def _token_usage_metadata(value: Any, timestamp: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "updated_at": timestamp,
        "total": _int_dict(value.get("total_token_usage")),
        "last": _int_dict(value.get("last_token_usage")),
        "model_context_window": _int_or_none(value.get("model_context_window")),
    }


def _rate_limits_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "plan_type": str(value.get("plan_type") or ""),
        "limit_id": str(value.get("limit_id") or ""),
        "rate_limit_reached_type": str(value.get("rate_limit_reached_type") or ""),
        "primary": _rate_limit_window(value.get("primary")),
        "secondary": _rate_limit_window(value.get("secondary")),
    }


def _rate_limit_window(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    resets_at = _epoch_to_iso(value.get("resets_at"))
    return {
        "used_percent": _float_or_none(value.get("used_percent")),
        "window_minutes": _int_or_none(value.get("window_minutes")),
        "resets_at": resets_at,
    }


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, raw in value.items():
        parsed = _int_or_none(raw)
        if parsed is not None:
            result[str(key)] = parsed
    return result


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_to_iso(value: Any) -> str:
    seconds = _int_or_none(value)
    if seconds is None:
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return ""


def _first_checkpoint_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "|" not in line or line.startswith("| # ") or line.startswith("|---"):
                continue
            parts = [part.strip() for part in line.strip("|").split("|")]
            if len(parts) >= 2 and parts[1]:
                return parts[1]
    except OSError:
        return ""
    return ""


def _first_markdown_heading(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
    except OSError:
        return ""
    return ""


def _has_live_lock(session_dir: Path) -> bool:
    for lock in session_dir.glob("inuse.*.lock"):
        match = re.search(r"inuse\.(\d+)\.lock$", lock.name)
        if not match:
            continue
        if Path(f"/proc/{match.group(1)}").exists():
            return True
    return False
