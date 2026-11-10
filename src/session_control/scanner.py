"""Session discovery for local AI assistant tools."""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import replace
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
from session_control.home_profiles import profile_homes, resume_args
from session_control.models import ScanError, ScanReport, SessionRecord

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

PROVIDERS = ("codex", "claude", "continue", "copilot", "grok", "agy")
CLAUDE_TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


class SessionScanner:
    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, ...] | None, tuple[float, ScanReport]] = {}

    def scan(self, providers: tuple[str, ...] | None = None) -> ScanReport:
        ttl = self.config.scan_cache_ttl_seconds
        if ttl > 0:
            now = time.monotonic()
            with self._lock:
                entry = self._cache.get(providers)
                if entry and now - entry[0] < ttl:
                    return entry[1]
            report = self._do_scan(providers)
            with self._lock:
                self._cache[providers] = (time.monotonic(), report)
            return report
        return self._do_scan(providers)

    def invalidate(self) -> None:
        """Drop cached scan results after session files change on disk."""
        with self._lock:
            self._cache.clear()

    def _do_scan(self, providers: tuple[str, ...] | None = None) -> ScanReport:
        selected = providers or PROVIDERS
        sessions: list[SessionRecord] = []
        errors: list[ScanError] = []
        scanners = {
            "codex": self._scan_codex,
            "claude": self._scan_claude,
            "continue": self._scan_continue,
            "copilot": self._scan_copilot,
            "grok": self._scan_grok,
            "agy": self._scan_agy,
        }
        for provider in selected:
            if provider not in scanners:
                errors.append(ScanError(provider, "Unknown provider."))
                continue
            try:
                provider_report = scanners[provider]()
                sessions.extend(provider_report.sessions)
                errors.extend(provider_report.errors)
            except OSError as exc:
                errors.append(ScanError(provider, str(exc)))
            except ValueError as exc:
                errors.append(ScanError(provider, str(exc)))
        sessions.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return ScanReport(tuple(sessions), tuple(errors))

    def _scan_codex(self) -> ScanReport:
        records: list[SessionRecord] = []
        errors: list[ScanError] = []
        for root, profile_name in profile_homes(self.config.codex_root, "codex"):
            index = _read_codex_index(root / "session_index.jsonl")
            for base in (root / "sessions", root / "archived_sessions"):
                if not base.exists():
                    continue
                for path in base.rglob("*.jsonl"):
                    issue = _probe_jsonl(
                        path,
                        description="Codex session",
                        supported=lambda item: bool(item.get("type")),
                        expectation="a typed event record",
                    )
                    if issue:
                        errors.append(ScanError("codex", issue))
                        continue
                    record = _codex_record(
                        path,
                        index,
                        self.config.max_preview_chars,
                        self.config.codex_resume_model,
                        self.config.codex_permission_preset,
                        profile_name=profile_name,
                    )
                    if record:
                        records.append(record)
        return ScanReport(tuple(records), tuple(errors))

    def _scan_claude(self) -> ScanReport:
        records: list[SessionRecord] = []
        errors: list[ScanError] = []
        local_sessions = _read_crew_chief_registry(self.config.crew_chief_registry)
        for home, profile_name in profile_homes(self.config.claude_root, "claude"):
            root = home / "projects"
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                issue = _probe_jsonl(
                    path,
                    description="Claude Code session",
                    supported=lambda item: bool(item.get("sessionId")),
                    expectation="a record with sessionId",
                )
                if issue:
                    errors.append(ScanError("claude", issue))
                    continue
                record = _claude_record(
                    path, self.config.max_preview_chars, profile_name=profile_name
                )
                if record:
                    local = local_sessions.get(record.session_id)
                    if local:
                        record = _with_local_backend(record, local)
                    records.append(record)
        return ScanReport(tuple(records), tuple(errors))

    def _scan_continue(self) -> ScanReport:
        records: list[SessionRecord] = []
        errors: list[ScanError] = []
        for home, profile_name in profile_homes(self.config.continue_root, "continue"):
            root = home / "sessions"
            if not root.exists():
                continue
            summaries = _read_json(root / "sessions.json")
            summary_by_id = {}
            index_path = root / "sessions.json"
            if index_path.exists() and not _is_continue_index(summaries):
                errors.append(
                    ScanError(
                        "continue",
                        "Unsupported Continue session index schema in sessions.json; "
                        "expected a list of session summaries with sessionId.",
                    )
                )
            if isinstance(summaries, list):
                summary_by_id = {
                    str(item.get("sessionId")): item for item in summaries if isinstance(item, dict)
                }
            for path in root.glob("*.json"):
                if path.name == "sessions.json":
                    continue
                data = _read_json(path)
                if not _is_continue_session(data):
                    errors.append(
                        ScanError(
                            "continue",
                            f"Unsupported Continue session schema in {path.name}; "
                            "expected an object with sessionId.",
                        )
                    )
                    continue
                record = _continue_record(
                    path,
                    summary_by_id,
                    self.config.max_preview_chars,
                    profile_name=profile_name,
                )
                if record:
                    records.append(record)
        return ScanReport(tuple(records), tuple(errors))

    def _scan_copilot(self) -> ScanReport:
        records: list[SessionRecord] = []
        errors: list[ScanError] = []
        for home, profile_name in profile_homes(self.config.copilot_root, "copilot"):
            root = home / "session-state"
            if not root.exists():
                continue
            for session_dir in root.iterdir():
                # Copilot keeps its own bookkeeping (.session-operation-locks) beside sessions.
                if not session_dir.is_dir() or session_dir.name.startswith("."):
                    continue
                workspace_file = session_dir / "workspace.yaml"
                workspace_data = _read_simple_yaml(workspace_file)
                if not workspace_file.exists() or not _is_copilot_workspace(workspace_data):
                    errors.append(
                        ScanError(
                            "copilot",
                            f"Unsupported Copilot session schema in {session_dir.name}; "
                            "expected workspace.yaml with id or cwd.",
                        )
                    )
                    continue
                events = session_dir / "events.jsonl"
                if events.exists():
                    issue = _probe_jsonl(
                        events,
                        description="Copilot event log",
                        supported=lambda item: bool(item.get("type")),
                        expectation="a typed event record",
                    )
                    if issue:
                        errors.append(ScanError("copilot", issue))
                        continue
                record = _copilot_record(
                    session_dir, self.config.max_preview_chars, profile_name=profile_name
                )
                if record:
                    records.append(record)
        return ScanReport(tuple(records), tuple(errors))

    def _scan_grok(self) -> ScanReport:
        homes = _grok_profile_homes(self.config.grok_root)
        records: list[SessionRecord] = []
        errors: list[ScanError] = []
        for root, profile_name in homes:
            home_records, home_errors = self._scan_one_grok_home(root, profile_name)
            records.extend(home_records)
            errors.extend(home_errors)
        return ScanReport(tuple(records), tuple(errors))

    def _scan_one_grok_home(
        self, root: Path, profile_name: str
    ) -> tuple[list[SessionRecord], list[ScanError]]:
        sessions_root = root / "sessions"
        if not sessions_root.exists():
            return [], []
        active_ids, active_registry_reliable = _grok_active_session_ids(
            root / "active_sessions.json"
        )
        records: list[SessionRecord] = []
        errors: list[ScanError] = []
        if not active_registry_reliable or (root / "active_sessions.json.corrupt").exists():
            active_registry_reliable = False
            errors.append(
                ScanError(
                    "grok",
                    "Grok active-session state is unreliable in active_sessions.json; "
                    "sessions are protected from deletion until it is repaired.",
                )
            )
        for summary_path in sessions_root.glob("*/*/summary.json"):
            data = _read_json(summary_path)
            if not _is_grok_summary(data):
                errors.append(
                    ScanError(
                        "grok",
                        f"Unsupported Grok session schema in {summary_path.parent.name}; "
                        "expected summary.json with info.id.",
                    )
                )
                continue
            record = _grok_record(
                summary_path.parent,
                data,
                active_ids,
                active_registry_reliable,
                self.config.max_preview_chars,
                profile_name=profile_name,
            )
            if record:
                records.append(record)
        return records, errors

    def _scan_agy(self) -> ScanReport:
        from session_control.profiles import load_profiles

        targets: list[tuple[Path, str, Path | None]] = [(self.config.agy_root, "default", None)]
        profile_json = self.config.agy_root.parent / "profiles.json"
        is_default_home = (
            self.config.agy_root.resolve()
            == (Path.home() / ".gemini" / "antigravity-cli").resolve()
        )

        if profile_json.is_file() or is_default_home:
            try:
                registry = load_profiles(profile_json if profile_json.is_file() else None)
                for prof_name, entry in registry.profiles.items():
                    p_root = entry.get_agy_root().resolve()
                    if p_root == self.config.agy_root.resolve():
                        continue
                    if (
                        not is_default_home
                        and p_root == (Path.home() / ".gemini" / "antigravity-cli").resolve()
                    ):
                        continue
                    if (p_root / "conversation_summaries.db").exists():
                        targets.append((p_root, prof_name, entry.get_gemini_path()))
            except Exception:
                pass

        records: list[SessionRecord] = []
        errors: list[ScanError] = []

        for root, prof_name, gemini_path in targets:
            index_path = root / "conversation_summaries.db"
            if not index_path.exists():
                continue
            try:
                rows = _read_agy_summaries(index_path)
            except (OSError, sqlite3.Error, ValueError) as exc:
                errors.append(
                    ScanError(
                        "agy",
                        "Unsupported Google AGY session index schema in "
                        f"conversation_summaries.db: {exc}",
                    )
                )
                continue

            for data in rows:
                session_id = str(data.get("conversation_id") or "")
                conversation_path = root / "conversations" / f"{session_id}.db"
                if not session_id or not conversation_path.is_file():
                    errors.append(
                        ScanError(
                            "agy",
                            f"Unsupported Google AGY session schema in {session_id or 'index row'}; "
                            "expected a matching conversation database.",
                        )
                    )
                    continue
                records.append(
                    _agy_record(
                        root,
                        conversation_path,
                        data,
                        self.config.max_preview_chars,
                        profile_name=prof_name,
                        gemini_dir=gemini_path,
                    )
                )
        return ScanReport(tuple(records), tuple(errors))


def _codex_is_subagent(payload: dict[str, Any]) -> bool:
    role = str(payload.get("agent_role") or "").strip().lower()
    if role and role not in {"default", "main"}:
        return True
    for key in ("source", "thread_source"):
        source = payload.get(key)
        if isinstance(source, str) and source.lower() in {"subagent", "sub_agent"}:
            return True
        if isinstance(source, dict) and any(
            str(name).lower().replace("_", "") == "subagent" for name in source
        ):
            return True
    return False


def _codex_record(
    path: Path,
    index: dict[str, dict[str, Any]],
    max_preview_chars: int,
    codex_resume_model: str | None,
    codex_permission_preset: str,
    profile_name: str = "default",
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
                    "is_subagent": _codex_is_subagent(payload),
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
    if latest_timestamp:
        metadata["last_event_at"] = latest_timestamp
    normalized_permission_preset = normalize_codex_permission_preset(codex_permission_preset)
    metadata["resume_permission_preset"] = normalized_permission_preset
    metadata["profile"] = profile_name
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
            resume_args(
                "codex",
                profile_name,
                _codex_resume_args(
                    session_id,
                    resume_model,
                    codex_permission_args(normalized_permission_preset, metadata),
                ),
            ),
        ),
        metadata=metadata,
    )


def _claude_record(
    path: Path, max_preview_chars: int, profile_name: str = "default"
) -> SessionRecord | None:
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
    is_subagent = "subagents" in path.parts

    for item in _iter_jsonl(path):
        if item.get("isSidechain") is True or item.get("agentId"):
            is_subagent = True
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
    metadata: dict[str, Any] = {
        "permission_mode": permission_mode,
        "is_subagent": is_subagent,
        "profile": profile_name,
    }
    if latest_timestamp:
        metadata["last_event_at"] = latest_timestamp
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
        resume_command=_command(
            workspace,
            resume_args("claude", profile_name, ["claude", "--resume", session_id]),
        ),
        metadata=metadata,
    )


def _continue_record(
    path: Path,
    summary_by_id: dict[str, dict[str, Any]],
    max_preview_chars: int,
    profile_name: str = "default",
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
        resume_command=_command(
            workspace,
            resume_args("continue", profile_name, ["cn", "--fork", session_id]),
        ),
        metadata={
            "mode": data.get("mode") or "",
            "model": data.get("chatModelTitle") or "",
            "profile": profile_name,
        },
    )


def _copilot_record(
    session_dir: Path, max_preview_chars: int, profile_name: str = "default"
) -> SessionRecord | None:
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
        resume_command=_command(
            workspace,
            resume_args("copilot", profile_name, ["copilot", f"--resume={session_id}"]),
        ),
        active=active,
        metadata={
            "summary_count": workspace_data.get("summary_count") or "",
            "profile": profile_name,
        },
    )


def _grok_profile_homes(root: Path) -> list[tuple[Path, str]]:
    """Default Grok home plus any extra accounts recorded beside it."""
    homes = [(root, "default")]
    registry_path = root / "profiles.json"
    if not registry_path.is_file():
        return homes
    try:
        from session_control.grok_profiles import load_profiles

        registry = load_profiles(registry_path, create=False)
    except (OSError, ValueError):
        return homes
    seen = {root.resolve()}
    for name, profile in registry.profiles.items():
        if name == "default" or not profile.enabled:
            continue
        try:
            config = Path(registry.config_path) if registry.config_path else registry_path
            home = profile.home(config).resolve()
        except OSError:
            continue
        if home in seen:
            continue
        seen.add(home)
        homes.append((home, name))
    return homes


def _grok_record(
    session_dir: Path,
    data: dict[str, Any],
    active_ids: set[str],
    active_registry_reliable: bool,
    max_preview_chars: int,
    profile_name: str = "default",
) -> SessionRecord | None:
    info = data.get("info")
    if not isinstance(info, dict):
        return None
    session_id = str(info.get("id") or session_dir.name)
    if not session_id:
        return None
    workspace = str(info.get("cwd") or "")
    summary = _clean_text(str(data.get("session_summary") or ""))
    first_user = _grok_first_user_text(session_dir)
    title = (
        _clean_text(str(data.get("generated_title") or ""))
        or _title_from_text(summary)
        or _title_from_text(first_user)
        or session_id
    )
    preview = summary or _clean_text(str(data.get("last_turn_summary") or "")) or first_user
    metadata = {
        "model": str(data.get("current_model_id") or ""),
        "agent_name": str(data.get("agent_name") or ""),
        "reasoning_effort": str(data.get("reasoning_effort") or ""),
        "sandbox_profile": str(data.get("sandbox_profile") or ""),
        "profile": profile_name,
    }
    if profile_name != "default":
        resume = ["grok-profile", "run", "--profile", profile_name, "--resume", session_id]
    else:
        resume = ["grok", "--resume", session_id]
    return SessionRecord(
        provider="grok",
        session_id=session_id,
        title=title,
        workspace=workspace,
        created_at=_normalize_datetime(str(data.get("created_at") or ""))
        or _mtime_iso(session_dir),
        updated_at=_normalize_datetime(
            str(data.get("last_active_at") or data.get("updated_at") or "")
        )
        or _mtime_iso(session_dir),
        primary_path=session_dir,
        delete_targets=(session_dir,),
        size_bytes=_path_size(session_dir),
        message_count=_int_or_none(data.get("num_chat_messages")) or 0,
        preview=_truncate(preview, max_preview_chars),
        resume_command=_command(workspace, resume),
        active=not active_registry_reliable or session_id in active_ids,
        metadata=metadata,
    )


def _agy_record(
    root: Path,
    conversation_path: Path,
    data: dict[str, Any],
    max_preview_chars: int,
    profile_name: str = "default",
    gemini_dir: Path | None = None,
) -> SessionRecord:
    session_id = str(data["conversation_id"])
    workspace = _agy_workspace(str(data.get("workspace_uris") or ""))
    updated_at = _normalize_datetime(str(data.get("last_modified_time") or ""))
    created_at = _normalize_datetime(str(data.get("last_user_input_time") or "")) or updated_at
    targets = [conversation_path]
    for candidate in (
        conversation_path.with_name(f"{conversation_path.name}-wal"),
        conversation_path.with_name(f"{conversation_path.name}-shm"),
        root / "brain" / session_id,
        root / "annotations" / f"{session_id}.pbtxt",
    ):
        if candidate.exists():
            targets.append(candidate)
    status = str(data.get("status") or "")
    killed = bool(data.get("killed"))
    active = (root / "presence" / f"{session_id}.lock").exists() or (
        bool(data.get("not_fully_idle")) and not killed
    )
    metadata = {
        "status": status,
        "source": str(data.get("source") or ""),
        "project_id": str(data.get("project_id") or ""),
        "agent_name": str(data.get("agent_name") or ""),
        "is_subagent": bool(data.get("parent_conversation_id")),
        "profile": profile_name,
    }
    if gemini_dir and profile_name != "default":
        resume_cmd = _command(
            workspace,
            ["agy-profile", "run", "--profile", profile_name, "--conversation", session_id],
        )
    else:
        resume_cmd = _command(workspace, ["agy", "--conversation", session_id])

    return SessionRecord(
        provider="agy",
        session_id=session_id,
        title=_clean_text(str(data.get("title") or "")) or session_id,
        workspace=workspace,
        created_at=created_at or _mtime_iso(conversation_path),
        updated_at=updated_at or _mtime_iso(conversation_path),
        primary_path=conversation_path,
        delete_targets=tuple(targets),
        size_bytes=sum(_path_size(path) for path in targets),
        message_count=_int_or_none(data.get("step_count")) or 0,
        preview=_truncate(_clean_text(str(data.get("preview") or "")), max_preview_chars),
        resume_command=resume_cmd,
        active=active,
        metadata=metadata,
    )


def _grok_first_user_text(session_dir: Path) -> str:
    for item in _iter_jsonl(session_dir / "updates.jsonl"):
        params = item.get("params")
        if not isinstance(params, dict):
            continue
        update = params.get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") != "user_message_chunk":
            continue
        text = _extract_text(update.get("content"))
        if text:
            return _clean_text(text)
    for item in _iter_jsonl(session_dir / "chat_history.jsonl"):
        if item.get("type") != "user":
            continue
        text = _extract_text(item.get("content"))
        if text:
            return _clean_text(text)
    return ""


def _grok_active_session_ids(path: Path) -> tuple[set[str], bool]:
    if not path.exists():
        return set(), True
    data = _read_json(path)
    if not isinstance(data, list):
        return set(), False
    active: set[str] = set()
    reliable = True
    for value in data:
        if not isinstance(value, dict):
            reliable = False
            continue
        session_id = ""
        for key in ("session_id", "sessionId", "id"):
            session_id = str(value.get(key) or "")
            if session_id:
                break
        if session_id:
            active.add(session_id)
        else:
            reliable = False
    return active, reliable


AGY_SUMMARY_COLUMNS = (
    "conversation_id",
    "title",
    "preview",
    "step_count",
    "last_modified_time",
    "workspace_uris",
    "status",
    "source",
    "project_id",
    "agent_name",
    "parent_conversation_id",
    "nesting_depth",
    "not_fully_idle",
    "killed",
    "last_user_input_time",
)


def _read_agy_summaries(path: Path) -> list[dict[str, Any]]:
    connection = _open_sqlite_readonly(path)
    try:
        columns = {
            str(row[1]) for row in connection.execute('PRAGMA table_info("conversation_summaries")')
        }
        missing = set(AGY_SUMMARY_COLUMNS) - columns
        if missing:
            raise ValueError("missing expected conversation summary fields")
        names = ", ".join(f'"{name}"' for name in AGY_SUMMARY_COLUMNS)
        rows = connection.execute(f"SELECT {names} FROM conversation_summaries").fetchall()
        return [dict(zip(AGY_SUMMARY_COLUMNS, row, strict=True)) for row in rows]
    finally:
        connection.close()


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri()
    try:
        return sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=1)
    except sqlite3.OperationalError:
        return sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True)


def _agy_workspace(value: str) -> str:
    try:
        uris = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        uris = []
    if isinstance(uris, list):
        for uri in uris:
            workspace = _uri_to_path(str(uri or ""))
            if workspace:
                return workspace
    return _uri_to_path(value)


def _read_crew_chief_registry(path: Path | None) -> dict[str, dict[str, str]]:
    """Map session IDs launched by `crew-chief code` to their backend details."""
    if path is None or not path.is_file():
        return {}
    entries: dict[str, dict[str, str]] = {}
    for item in _iter_jsonl(path):
        session_id = str(item.get("session_id") or "")
        model = str(item.get("model") or "")
        if session_id and model:
            entries[session_id] = {
                "backend": str(item.get("backend") or "ollama"),
                "base_url": str(item.get("base_url") or ""),
                "model": model,
            }
    return entries


def _with_local_backend(record: SessionRecord, local: dict[str, str]) -> SessionRecord:
    """Label a Claude session that ran on a local model and resume it there."""
    args = ["crew-chief"]
    if local["base_url"]:
        args.extend(["--url", local["base_url"]])
    args.extend(["code", "--model", local["model"], "--backend", local["backend"]])
    args.extend(["--", "--resume", record.session_id])
    return replace(
        record,
        resume_command=_command(record.workspace, args),
        metadata={**record.metadata, "local_backend": local},
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
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # callers read fields with .get(); a stray array, number or string line
                # in a file another program wrote must not abort the whole scan
                if isinstance(item, dict):
                    yield item
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


def _probe_jsonl(
    path: Path,
    *,
    description: str,
    supported: Callable[[dict[str, Any]], bool],
    expectation: str,
) -> str | None:
    valid = False
    malformed = False
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                malformed = True
                continue
            if isinstance(item, dict) and supported(item):
                valid = True
    except OSError as exc:
        return f"Could not read {description} {path.name}: {exc}"
    if valid:
        return None
    if malformed:
        return f"Unsupported {description} format in {path.name}; invalid JSONL was found."
    return f"Unsupported {description} schema in {path.name}; expected {expectation}."


def _is_continue_index(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict) and bool(item.get("sessionId")) for item in value
    )


def _is_continue_session(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("sessionId"))


def _is_copilot_workspace(value: dict[str, str]) -> bool:
    return bool(value.get("id") or value.get("cwd"))


def _is_grok_summary(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("info"), dict)
        and bool(value["info"].get("id"))
    )


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
    # Markers such as <environment_context> are tags, which cleaning removes, so
    # they are looked for in the raw text as well.
    candidates = [(value, _clean_text(value)) for value in values]
    candidates = [(raw, cleaned) for raw, cleaned in candidates if cleaned]
    for raw, cleaned in candidates:
        if not _is_bootstrap_text(raw) and not _is_bootstrap_text(cleaned):
            return cleaned
    return candidates[0][1] if candidates else ""


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


def _process_exists(pid: int) -> bool:
    """Report whether *pid* is a live process, portably.

    /proc only exists on Linux, so probing it silently reports every lock as
    dead on macOS — which would let prune and delete tear down sessions that
    are actively in use. Signal 0 performs the permission and existence checks
    without delivering a signal. A PermissionError means the process exists but
    belongs to another user, which still counts as live.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _has_live_lock(session_dir: Path) -> bool:
    for lock in session_dir.glob("inuse.*.lock"):
        match = re.search(r"inuse\.(\d+)\.lock$", lock.name)
        if not match:
            continue
        if _process_exists(int(match.group(1))):
            return True
    return False
