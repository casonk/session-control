"""Isolated logins for CLIs that keep one account per home directory.

The registry stays in the default home. A selected account's own environment
variable is never used to find that registry, or the selected account would
hide the list. Launching a profile sets the CLI's home variable and drops
token variables that would otherwise override the directory login.
"""

from __future__ import annotations

import base64
import contextlib
import fnmatch
import json
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_COPILOT_LOGIN = re.compile(
    r'"lastLoggedInUser"\s*:\s*\{[^{}]*?"login"\s*:\s*"([A-Za-z0-9][A-Za-z0-9-]{0,38})"',
    re.DOTALL,
)


@dataclass(frozen=True)
class HomeProfileSpec:
    provider: str
    title: str
    executable: str
    env_var: str
    launcher: str
    sign_in_args: tuple[str, ...]
    default_home_env: str
    config_envs: tuple[str, ...]
    usage_db_envs: tuple[str, ...]
    default_dirname: str
    strip_env: tuple[str, ...]
    auth_filenames: tuple[str, ...]
    account_label: str


def _spec(
    provider: str,
    title: str,
    executable: str,
    env_var: str,
    launcher: str,
    sign_in_args: tuple[str, ...],
    dirname: str,
    strip_env: tuple[str, ...],
    auth_filenames: tuple[str, ...],
    account_label: str,
) -> HomeProfileSpec:
    token = provider.upper()
    return HomeProfileSpec(
        provider=provider,
        title=title,
        executable=executable,
        env_var=env_var,
        launcher=launcher,
        sign_in_args=sign_in_args,
        default_home_env=f"SESSION_CONTROL_{token}_HOME",
        config_envs=(f"SESSION_CONTROL_{token}_PROFILES_PATH", f"{token}_PROFILES_CONFIG"),
        usage_db_envs=(f"SESSION_CONTROL_{token}_USAGE_DB", f"{token}_PROFILE_USAGE_DB"),
        default_dirname=dirname,
        strip_env=strip_env,
        auth_filenames=auth_filenames,
        account_label=account_label,
    )


SPECS: dict[str, HomeProfileSpec] = {
    spec.provider: spec
    for spec in (
        _spec(
            "codex",
            "Codex accounts",
            "codex",
            "CODEX_HOME",
            "codex-profile",
            ("login",),
            ".codex",
            ("OPENAI_API_KEY",),
            ("auth.json",),
            "Default Codex account",
        ),
        _spec(
            "claude",
            "Claude Code accounts",
            "claude",
            "CLAUDE_CONFIG_DIR",
            "claude-profile",
            ("auth", "login"),
            ".claude",
            ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
            (".credentials.json",),
            "Default Claude Code account",
        ),
        _spec(
            "continue",
            "Continue accounts",
            "cn",
            "CONTINUE_GLOBAL_DIR",
            "continue-profile",
            ("login",),
            ".continue",
            ("CONTINUE_API_KEY",),
            ("auth.json",),
            "Default Continue account",
        ),
        _spec(
            "copilot",
            "Copilot CLI accounts",
            "copilot",
            "COPILOT_HOME",
            "copilot-profile",
            ("login",),
            ".copilot",
            ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
            ("config.json",),
            "Default Copilot CLI account",
        ),
    )
}


def default_home(spec: HomeProfileSpec) -> Path:
    """Default account directory. Ignores the CLI's own home variable."""
    override = os.environ.get(spec.default_home_env, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / spec.default_dirname).resolve()


def get_profiles_config_path(spec: HomeProfileSpec) -> Path:
    for name in spec.config_envs:
        override = os.environ.get(name, "").strip()
        if override:
            return Path(override).expanduser().resolve()
    return default_home(spec) / "profiles.json"


def get_usage_db_path(spec: HomeProfileSpec) -> Path:
    for name in spec.usage_db_envs:
        override = os.environ.get(name, "").strip()
        if override:
            return Path(override).expanduser().resolve()
    return default_home(spec) / "profile_usage.sqlite"


@dataclass
class HomeProfile:
    name: str
    email: str = ""
    home_dir: str = ""
    description: str = ""
    enabled: bool = True
    created_at: str = ""

    def home(self, config_path: Path) -> Path:
        if self.home_dir.strip():
            return Path(self.home_dir).expanduser().resolve()
        base = config_path.parent
        if self.name in {"default", "personal"}:
            return base.resolve()
        return (base / "profiles" / self.name).resolve()


@dataclass
class HomeRoute:
    pattern: str
    profile: str


@dataclass
class HomeProfileRegistry:
    spec_name: str
    default_profile: str = "default"
    active_profile: str = "default"
    strategy: str = "auto"
    profiles: dict[str, HomeProfile] = field(default_factory=dict)
    routes: list[HomeRoute] = field(default_factory=list)
    cooldowns: dict[str, float] = field(default_factory=dict)
    config_path: str = ""


def _int_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)


def _jwt_email(token: str) -> str:
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return ""
    payload = parts[1]
    padded = payload + ("=" * (-len(payload) % 4))
    try:
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    email = data.get("email")
    if isinstance(email, str) and "@" in email:
        return email.strip()
    return ""


def _email_from_json(payload: Any) -> str:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"email", "userEmail"} and isinstance(value, str) and "@" in value:
                    found.append(value.strip())
                elif key == "id_token" and isinstance(value, str):
                    email = _jwt_email(value)
                    if email:
                        found.append(email)
                elif key not in {"access_token", "refresh_token", "accessToken", "refreshToken"}:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found[0] if found else ""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def discover_account_label(spec: HomeProfileSpec, home: Path) -> str:
    """Account label from auth files. Token values are never returned."""
    root = home.resolve()
    for name in spec.auth_filenames:
        path = root / name
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file() or not resolved.is_relative_to(root):
            continue
        if spec.provider == "copilot":
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = _COPILOT_LOGIN.search(text)
            if match and _GITHUB_LOGIN.fullmatch(match.group(1)):
                return match.group(1)
            continue
        email = _email_from_json(_read_json(resolved))
        if email:
            return email
    return ""


def _blank_registry(spec: HomeProfileSpec, home: Path) -> HomeProfileRegistry:
    now = datetime.now(timezone.utc).isoformat()
    registry = HomeProfileRegistry(spec_name=spec.provider)
    registry.profiles["default"] = HomeProfile(
        name="default",
        email=discover_account_label(spec, home),
        description=spec.account_label,
        enabled=True,
        created_at=now,
    )
    return registry


def load_profiles(
    spec: HomeProfileSpec,
    config_path: Path | None = None,
    *,
    create: bool = True,
) -> HomeProfileRegistry:
    path = config_path or get_profiles_config_path(spec)
    if not path.is_file():
        registry = _blank_registry(spec, path.parent)
        registry.config_path = str(path)
        if create:
            save_profiles(registry, path)
        return registry
    raw = _read_json(path)
    if not isinstance(raw, dict):
        registry = _blank_registry(spec, path.parent)
        registry.config_path = str(path)
        return registry

    profiles: dict[str, HomeProfile] = {}
    raw_profiles = raw.get("profiles")
    if isinstance(raw_profiles, dict):
        for name, item in raw_profiles.items():
            if not isinstance(item, dict):
                continue
            profiles[str(name)] = HomeProfile(
                name=str(name),
                email=str(item.get("email") or ""),
                home_dir=str(item.get("home") or ""),
                description=str(item.get("description") or ""),
                enabled=bool(item.get("enabled", True)),
                created_at=str(item.get("created_at") or ""),
            )
    routes: list[HomeRoute] = []
    raw_routes = raw.get("routes")
    if isinstance(raw_routes, list):
        for item in raw_routes:
            if isinstance(item, dict) and item.get("pattern") and item.get("profile"):
                routes.append(HomeRoute(pattern=str(item["pattern"]), profile=str(item["profile"])))
    if "default" not in profiles:
        profiles = {**_blank_registry(spec, path.parent).profiles, **profiles}
    return HomeProfileRegistry(
        spec_name=spec.provider,
        default_profile=str(raw.get("default_profile") or "default"),
        active_profile=str(raw.get("active_profile") or "default"),
        strategy=str(raw.get("strategy") or "auto"),
        profiles=profiles,
        routes=routes,
        config_path=str(path),
    )


def save_profiles(registry: HomeProfileRegistry, config_path: Path | None = None) -> None:
    spec = SPECS[registry.spec_name]
    path = config_path or (
        Path(registry.config_path) if registry.config_path else get_profiles_config_path(spec)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "default_profile": registry.default_profile,
        "active_profile": registry.active_profile,
        "strategy": registry.strategy,
        "profiles": {
            name: {
                "name": profile.name,
                "email": profile.email,
                "home": profile.home_dir,
                "description": profile.description,
                "enabled": profile.enabled,
                "created_at": profile.created_at,
            }
            for name, profile in registry.profiles.items()
        },
        "routes": [asdict(route) for route in registry.routes],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    registry.config_path = str(path)


def validate_profile_name(name: str) -> str | None:
    if PROFILE_NAME.fullmatch(name):
        return None
    return "Profile name must start with a letter or digit and use only letters, digits, _ or -."


def resolve_profile(
    registry: HomeProfileRegistry,
    cwd: Path | None = None,
    requested_profile: str | None = None,
    failover_exclude: set[str] | None = None,
) -> HomeProfile:
    excluded = set(failover_exclude or ())
    now = time.time()
    for name, until in registry.cooldowns.items():
        if until > now:
            excluded.add(name)

    if requested_profile and requested_profile in registry.profiles:
        chosen = registry.profiles[requested_profile]
        if chosen.enabled and chosen.name not in excluded:
            return chosen

    target = str((cwd or Path.cwd()).resolve())
    target_name = os.path.basename(target)
    for route in registry.routes:
        if fnmatch.fnmatch(target, route.pattern) or fnmatch.fnmatch(target_name, route.pattern):
            chosen = registry.profiles.get(route.profile)
            if chosen and chosen.enabled and chosen.name not in excluded:
                return chosen

    for name in (registry.active_profile, registry.default_profile):
        chosen = registry.profiles.get(name)
        if chosen and chosen.enabled and chosen.name not in excluded:
            return chosen
    for chosen in registry.profiles.values():
        if chosen.enabled and chosen.name not in excluded:
            return chosen
    fallback = registry.profiles.get("default")
    if fallback is None:
        fallback = HomeProfile(name="default", enabled=True)
        registry.profiles["default"] = fallback
    return fallback


def profile_homes(root: Path, provider: str) -> list[tuple[Path, str]]:
    """Default home plus extra accounts recorded in ``<root>/profiles.json``."""
    homes = [(root, "default")]
    spec = SPECS.get(provider)
    registry_path = root / "profiles.json"
    if spec is None or not registry_path.is_file():
        return homes
    try:
        registry = load_profiles(spec, registry_path, create=False)
    except (OSError, ValueError):
        return homes
    seen = {root.resolve()}
    for name, profile in registry.profiles.items():
        if name == "default" or not profile.enabled:
            continue
        try:
            home = profile.home(registry_path).resolve()
        except OSError:
            continue
        if home in seen:
            continue
        seen.add(home)
        homes.append((home, name))
    return homes


def owning_account_home(provider_root: Path, session_path: Path) -> Path:
    """Account directory that owns a session file under ``provider_root``."""
    root = provider_root.resolve()
    try:
        resolved = session_path.resolve()
        relative = resolved.relative_to(root / "profiles")
    except (OSError, ValueError):
        return root
    if not relative.parts:
        return root
    return root / "profiles" / relative.parts[0]


def resume_args(provider: str, profile: str, native_args: list[str]) -> list[str]:
    """Resume through the profile launcher when the session is not on default."""
    spec = SPECS[provider]
    if profile == "default" or not native_args:
        return native_args
    remainder = native_args[1:] if native_args[0] == spec.executable else native_args
    return [spec.launcher, "run", "--profile", profile, *remainder]


def init_usage_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions_summary (
            profile TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            title TEXT DEFAULT '',
            workspace TEXT DEFAULT '',
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            total_cache_read_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            last_event_at TEXT,
            updated_at TEXT,
            PRIMARY KEY(profile, conversation_id)
        )
        """
    )
    connection.commit()
    return connection


def _contained_files(home: Path, pattern: str) -> list[Path]:
    root = home.resolve()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.glob(pattern):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(root):
            found.append(resolved)
    return found


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _session_id_from_name(path: Path) -> str:
    match = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        path.name,
    )
    return match.group(0) if match else path.stem


@dataclass(frozen=True)
class UsageRow:
    conversation_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    total_tokens: int = 0
    last_event_at: str = ""


def _codex_rows(home: Path) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for path in _contained_files(home, "sessions/**/*.jsonl") + _contained_files(
        home, "archived_sessions/**/*.jsonl"
    ):
        session_id = ""
        last_event = ""
        usage: dict[str, Any] | None = None
        for item in _iter_jsonl(path):
            timestamp = str(item.get("timestamp") or "")
            if timestamp:
                last_event = timestamp
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            if item.get("type") == "session_meta":
                session_id = str(payload.get("id") or session_id)
            if item.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                total = info.get("total_token_usage") if isinstance(info, dict) else None
                if isinstance(total, dict):
                    usage = total
        session_id = session_id or _session_id_from_name(path)
        if not session_id:
            continue
        counted = usage or {}
        total_tokens = _int_value(counted.get("total_tokens"))
        input_tokens = _int_value(counted.get("input_tokens"))
        output_tokens = _int_value(counted.get("output_tokens"))
        cache_tokens = _int_value(counted.get("cached_input_tokens"))
        if total_tokens <= 0:
            total_tokens = input_tokens + output_tokens + cache_tokens
        rows.append(
            UsageRow(
                conversation_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_tokens=cache_tokens,
                total_tokens=total_tokens,
                last_event_at=last_event,
            )
        )
    return rows


def _claude_rows(home: Path) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for path in _contained_files(home, "projects/**/*.jsonl"):
        session_id = ""
        last_event = ""
        input_tokens = output_tokens = cache_tokens = 0
        for item in _iter_jsonl(path):
            timestamp = str(item.get("timestamp") or "")
            if timestamp:
                last_event = timestamp
            session_id = session_id or str(item.get("sessionId") or "")
            message = item.get("message")
            if item.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            input_tokens += _int_value(usage.get("input_tokens"))
            output_tokens += _int_value(usage.get("output_tokens"))
            cache_tokens += _int_value(usage.get("cache_read_input_tokens"))
            cache_tokens += _int_value(usage.get("cache_creation_input_tokens"))
        session_id = session_id or _session_id_from_name(path)
        if not session_id:
            continue
        rows.append(
            UsageRow(
                conversation_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_tokens=cache_tokens,
                total_tokens=input_tokens + output_tokens + cache_tokens,
                last_event_at=last_event,
            )
        )
    return rows


def _usage_object_tokens(usage: dict[str, Any]) -> tuple[int, int, int]:
    input_tokens = _int_value(usage.get("inputTokens")) or _int_value(usage.get("promptTokens"))
    output_tokens = _int_value(usage.get("outputTokens")) or _int_value(
        usage.get("completionTokens")
    )
    cache_tokens = _int_value(usage.get("cacheReadTokens"))
    return input_tokens, output_tokens, cache_tokens


def _continue_rows(home: Path) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for path in _contained_files(home, "sessions/*.json"):
        if path.name == "sessions.json":
            continue
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        session_id = str(data.get("sessionId") or _session_id_from_name(path))
        if not session_id:
            continue
        input_tokens = output_tokens = cache_tokens = 0
        objects: list[dict[str, Any]] = []
        top = data.get("usage")
        if isinstance(top, dict):
            objects.append(top)
        history = data.get("history")
        if isinstance(history, list):
            for turn in history:
                if not isinstance(turn, dict):
                    continue
                if isinstance(turn.get("usage"), dict):
                    objects.append(turn["usage"])
                message = turn.get("message")
                if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                    objects.append(message["usage"])
        for usage in objects:
            added_input, added_output, added_cache = _usage_object_tokens(usage)
            input_tokens += added_input
            output_tokens += added_output
            cache_tokens += added_cache
        rows.append(
            UsageRow(
                conversation_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_tokens=cache_tokens,
                total_tokens=input_tokens + output_tokens + cache_tokens,
            )
        )
    return rows


def _nested_token_count(node: Any, key: str) -> int:
    if not isinstance(node, dict):
        return 0
    child = node.get(key)
    if isinstance(child, dict):
        return _int_value(child.get("tokenCount"))
    return _int_value(child)


def _copilot_rows(home: Path) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for path in _contained_files(home, "session-state/*/events.jsonl"):
        session_id = path.parent.name
        last_event = ""
        input_tokens = output_tokens = cache_tokens = 0
        saw_record = False
        shutdown_usage: dict[str, Any] | None = None
        for item in _iter_jsonl(path):
            timestamp = str(item.get("timestamp") or "")
            if timestamp:
                last_event = timestamp
            data = item.get("data")
            if not isinstance(data, dict):
                continue
            event_type = str(item.get("type") or "")
            if event_type == "session.usage_record":
                saw_record = True
                input_tokens += _int_value(data.get("inputTokens"))
                output_tokens += _int_value(data.get("outputTokens"))
                cache_tokens += _int_value(data.get("cacheReadTokens"))
                cache_tokens += _int_value(data.get("cacheWriteTokens"))
            elif event_type == "session.shutdown":
                details = data.get("tokenDetails")
                if isinstance(details, dict):
                    shutdown_usage = details
        if not saw_record and shutdown_usage is not None:
            input_tokens = _nested_token_count(shutdown_usage, "input")
            output_tokens = _nested_token_count(shutdown_usage, "output")
            cache_tokens = _nested_token_count(shutdown_usage, "cache_read")
            cache_tokens += _nested_token_count(shutdown_usage, "cache_write")
        if session_id.startswith("."):
            continue
        rows.append(
            UsageRow(
                conversation_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_tokens=cache_tokens,
                total_tokens=input_tokens + output_tokens + cache_tokens,
                last_event_at=last_event,
            )
        )
    return rows


HARVESTERS: dict[str, Callable[[Path], list[UsageRow]]] = {
    "codex": _codex_rows,
    "claude": _claude_rows,
    "continue": _continue_rows,
    "copilot": _copilot_rows,
}


def harvest_usage(
    registry: HomeProfileRegistry,
    db_path: Path | None = None,
) -> dict[str, int]:
    spec = SPECS[registry.spec_name]
    harvester = HARVESTERS[spec.provider]
    connection = init_usage_db(db_path or get_usage_db_path(spec))
    cursor = connection.cursor()
    seen = 0
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    config_path = (
        Path(registry.config_path) if registry.config_path else get_profiles_config_path(spec)
    )
    for profile in registry.profiles.values():
        if not profile.enabled:
            continue
        for row in harvester(profile.home(config_path)):
            if not row.conversation_id:
                continue
            seen += 1
            cursor.execute(
                """
                INSERT INTO sessions_summary (
                    profile, conversation_id, total_input_tokens, total_output_tokens,
                    total_cache_read_tokens, total_tokens, last_event_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile, conversation_id) DO UPDATE SET
                    total_input_tokens=excluded.total_input_tokens,
                    total_output_tokens=excluded.total_output_tokens,
                    total_cache_read_tokens=excluded.total_cache_read_tokens,
                    total_tokens=excluded.total_tokens,
                    last_event_at=excluded.last_event_at,
                    updated_at=excluded.updated_at
                """,
                (
                    profile.name,
                    row.conversation_id,
                    row.input_tokens,
                    row.output_tokens,
                    row.cache_tokens,
                    row.total_tokens,
                    row.last_event_at,
                    now,
                ),
            )
            updated += 1
    connection.commit()
    connection.close()
    return {"sessions_seen": seen, "sessions_updated": updated}


def get_usage_summary(
    spec: HomeProfileSpec,
    db_path: Path | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    connection = init_usage_db(db_path or get_usage_db_path(spec))
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    where = "WHERE profile = ?" if profile else ""
    params: list[str] = [profile] if profile else []
    cursor.execute(
        f"""
        SELECT profile,
               COUNT(*) AS session_count,
               SUM(total_input_tokens) AS total_input,
               SUM(total_output_tokens) AS total_output,
               SUM(total_cache_read_tokens) AS total_cache,
               SUM(total_tokens) AS total_tokens,
               MAX(last_event_at) AS last_active
        FROM sessions_summary
        {where}
        GROUP BY profile
        """,
        params,
    )
    profiles = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        f"""
        SELECT conversation_id, profile, total_input_tokens, total_output_tokens,
               total_cache_read_tokens, total_tokens, last_event_at
        FROM sessions_summary
        {where}
        ORDER BY last_event_at DESC
        LIMIT 25
        """,
        params,
    )
    recent = [dict(row) for row in cursor.fetchall()]
    connection.close()
    return {
        "profiles": profiles,
        "recent_sessions": recent,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def execute_with_profile(
    spec: HomeProfileSpec,
    args: list[str],
    requested_profile: str | None = None,
    cwd: Path | None = None,
    auto_failover: bool = True,
    config_path: Path | None = None,
    executable: str | None = None,
) -> int:
    registry = load_profiles(spec, config_path)
    excluded: set[str] = set()
    attempts = 0
    limit = len(registry.profiles) if auto_failover else 1
    return_code = 1
    binary = executable or spec.executable
    while attempts < limit:
        attempts += 1
        profile = resolve_profile(
            registry,
            cwd=cwd,
            requested_profile=requested_profile,
            failover_exclude=excluded,
        )
        config = Path(registry.config_path) if registry.config_path else config_path
        if config is None:
            config = get_profiles_config_path(spec)
        home = profile.home(config)
        home.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(home, 0o700)
        env = dict(os.environ)
        for name in spec.strip_env:
            env.pop(name, None)
        env[spec.env_var] = str(home)
        completed = subprocess.run([binary, *args], env=env, cwd=cwd, check=False)
        return_code = int(completed.returncode)
        if return_code == 0 or not auto_failover:
            break
        excluded.add(profile.name)
        registry.cooldowns[profile.name] = time.time() + 300
    with contextlib.suppress(Exception):
        harvest_usage(registry)
    return return_code
