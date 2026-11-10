"""Isolated Grok accounts, directory routing, and local usage totals.

Each profile is its own ``GROK_HOME``. The default profile stays at ``~/.grok``.
Added profiles live under ``~/.grok/profiles/<name>`` unless a directory is set.
The registry stays in the default home so a selected profile cannot hide it.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def default_grok_home() -> Path:
    override = os.environ.get("SESSION_CONTROL_GROK_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".grok"


def get_profiles_config_path() -> Path:
    override = os.environ.get("SESSION_CONTROL_GROK_PROFILES_PATH") or os.environ.get(
        "GROK_PROFILES_CONFIG", ""
    )
    if override.strip():
        return Path(override).expanduser().resolve()
    return default_grok_home() / "profiles.json"


def get_usage_db_path() -> Path:
    override = os.environ.get("SESSION_CONTROL_GROK_USAGE_DB") or os.environ.get(
        "GROK_PROFILE_USAGE_DB", ""
    )
    if override.strip():
        return Path(override).expanduser().resolve()
    return default_grok_home() / "profile_usage.sqlite"


@dataclass
class GrokProfile:
    name: str
    email: str = ""
    grok_home: str = ""
    description: str = ""
    enabled: bool = True
    created_at: str = ""

    def home(self, config_path: Path | None = None) -> Path:
        """Directory Grok should use as ``GROK_HOME`` for this account."""
        if self.grok_home.strip():
            return Path(self.grok_home).expanduser().resolve()
        base = (config_path or get_profiles_config_path()).parent
        if self.name in {"default", "personal"}:
            return base.resolve()
        return (base / "profiles" / self.name).resolve()


@dataclass
class GrokRoute:
    pattern: str
    profile: str


@dataclass
class GrokProfileRegistry:
    default_profile: str = "default"
    active_profile: str = "default"
    strategy: str = "auto"
    profiles: dict[str, GrokProfile] = field(default_factory=dict)
    routes: list[GrokRoute] = field(default_factory=list)
    cooldowns: dict[str, float] = field(default_factory=dict)
    config_path: str = ""


def discover_email(home: Path) -> str:
    """Return the first email stored in a Grok auth file. Tokens are ignored."""
    path = home / "auth.json"
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "email" and isinstance(value, str) and "@" in value:
                    found.append(value.strip())
                else:
                    walk(value)

    walk(payload)
    return found[0] if found else ""


def _blank_registry(home: Path) -> GrokProfileRegistry:
    now = datetime.now(timezone.utc).isoformat()
    registry = GrokProfileRegistry()
    registry.profiles["default"] = GrokProfile(
        name="default",
        email=discover_email(home),
        description="Default Grok account",
        enabled=True,
        created_at=now,
    )
    return registry


def load_profiles(config_path: Path | None = None, *, create: bool = True) -> GrokProfileRegistry:
    path = config_path or get_profiles_config_path()
    if not path.is_file():
        registry = _blank_registry(path.parent)
        registry.config_path = str(path)
        if create:
            save_profiles(registry, path)
        return registry
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        registry = _blank_registry(path.parent)
        registry.config_path = str(path)
        return registry
    if not isinstance(raw, dict):
        registry = _blank_registry(path.parent)
        registry.config_path = str(path)
        return registry

    profiles: dict[str, GrokProfile] = {}
    raw_profiles = raw.get("profiles")
    if isinstance(raw_profiles, dict):
        for name, item in raw_profiles.items():
            if not isinstance(item, dict):
                continue
            profiles[str(name)] = GrokProfile(
                name=str(name),
                email=str(item.get("email") or ""),
                grok_home=str(item.get("grok_home") or ""),
                description=str(item.get("description") or ""),
                enabled=bool(item.get("enabled", True)),
                created_at=str(item.get("created_at") or ""),
            )
    routes: list[GrokRoute] = []
    raw_routes = raw.get("routes")
    if isinstance(raw_routes, list):
        for item in raw_routes:
            if isinstance(item, dict) and item.get("pattern") and item.get("profile"):
                routes.append(GrokRoute(pattern=str(item["pattern"]), profile=str(item["profile"])))
    if "default" not in profiles:
        profiles = {**_blank_registry(path.parent).profiles, **profiles}
    return GrokProfileRegistry(
        default_profile=str(raw.get("default_profile") or "default"),
        active_profile=str(raw.get("active_profile") or "default"),
        strategy=str(raw.get("strategy") or "auto"),
        profiles=profiles,
        routes=routes,
        config_path=str(path),
    )


def save_profiles(registry: GrokProfileRegistry, config_path: Path | None = None) -> None:
    path = config_path or get_profiles_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "default_profile": registry.default_profile,
        "active_profile": registry.active_profile,
        "strategy": registry.strategy,
        "profiles": {name: asdict(profile) for name, profile in registry.profiles.items()},
        "routes": [asdict(route) for route in registry.routes],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_profile_name(name: str) -> str | None:
    if PROFILE_NAME.fullmatch(name):
        return None
    return "Profile name must start with a letter or digit and use only letters, digits, _ or -."


def resolve_profile(
    registry: GrokProfileRegistry,
    cwd: Path | None = None,
    requested_profile: str | None = None,
    failover_exclude: set[str] | None = None,
) -> GrokProfile:
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
        fallback = GrokProfile(name="default", enabled=True)
        registry.profiles["default"] = fallback
    return fallback


def init_usage_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_usage_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("""
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
        """)
    connection.commit()
    return connection


def _usage_files(home: Path) -> list[Path]:
    sessions = home / "sessions"
    if not sessions.is_dir():
        return []
    home_resolved = home.resolve()
    found: list[Path] = []
    for path in sessions.glob("*/*/usage.json"):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_relative_to(home_resolved) and resolved.is_file():
            found.append(resolved)
    return found


def _int_field(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)


def harvest_usage(
    registry: GrokProfileRegistry | None = None,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Record each session's latest usage.json totals. One row per profile and session."""
    current = registry or load_profiles()
    connection = init_usage_db(db_path)
    cursor = connection.cursor()
    seen = 0
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for profile in current.profiles.values():
        if not profile.enabled:
            continue
        config_path = Path(current.config_path) if current.config_path else None
        for path in _usage_files(profile.home(config_path)):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            session = payload.get("session")
            if not isinstance(session, dict):
                continue
            conversation_id = str(payload.get("sessionId") or path.parent.name)
            if not conversation_id:
                continue
            seen += 1
            last_event = str(payload.get("updatedAt") or "")
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
                    conversation_id,
                    _int_field(session, "inputTokens"),
                    _int_field(session, "outputTokens"),
                    _int_field(session, "cachedReadTokens"),
                    _int_field(session, "totalTokens"),
                    last_event,
                    now,
                ),
            )
            updated += 1
    connection.commit()
    connection.close()
    return {"sessions_seen": seen, "sessions_updated": updated}


def get_usage_summary(
    db_path: Path | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    connection = init_usage_db(db_path)
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
    args: list[str],
    requested_profile: str | None = None,
    cwd: Path | None = None,
    auto_failover: bool = True,
    config_path: Path | None = None,
    executable: str = "grok",
) -> int:
    registry = load_profiles(config_path)
    excluded: set[str] = set()
    attempts = 0
    limit = len(registry.profiles) if auto_failover else 1
    return_code = 1
    while attempts < limit:
        attempts += 1
        profile = resolve_profile(
            registry,
            cwd=cwd,
            requested_profile=requested_profile,
            failover_exclude=excluded,
        )
        config = Path(registry.config_path) if registry.config_path else config_path
        home = profile.home(config)
        home.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(home, 0o700)
        env = dict(os.environ)
        env["GROK_HOME"] = str(home)
        completed = subprocess.run([executable, *args], env=env, cwd=cwd, check=False)
        return_code = int(completed.returncode)
        if return_code == 0 or not auto_failover:
            break
        excluded.add(profile.name)
        registry.cooldowns[profile.name] = time.time() + 300
    with contextlib.suppress(Exception):
        harvest_usage(registry)
    return return_code
