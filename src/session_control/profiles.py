"""Local multi-user profile management, routing, and usage tracing for Google AGY / Gemini."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".gemini" / "profiles.json"
DEFAULT_USAGE_DB_PATH = Path.home() / ".gemini" / "usage_tracking.sqlite"
DEFAULT_GEMINI_DIR = Path.home() / ".gemini"


@dataclass
class ProfileEntry:
    name: str
    email: str = ""
    gemini_dir: str = ""
    description: str = ""
    quota_limit_daily: int | None = None
    enabled: bool = True
    created_at: str = ""

    def get_gemini_path(self) -> Path:
        if not self.gemini_dir:
            if self.name in {"default", "personal"}:
                return DEFAULT_GEMINI_DIR
            return DEFAULT_GEMINI_DIR / "profiles" / self.name
        return Path(self.gemini_dir).expanduser().resolve()

    def get_agy_root(self) -> Path:
        base = self.get_gemini_path()
        return base / "antigravity-cli"


@dataclass
class ProfileRouteRule:
    pattern: str
    profile: str


@dataclass
class ProfileRegistry:
    default_profile: str = "default"
    active_profile: str = "default"
    strategy: str = "auto"  # auto, round-robin, repo-affinity, sticky
    profiles: dict[str, ProfileEntry] = field(default_factory=dict)
    routes: list[ProfileRouteRule] = field(default_factory=list)
    cooldowns: dict[str, float] = field(
        default_factory=dict
    )  # profile_name -> cooldown_until_timestamp


def get_profiles_config_path() -> Path:
    env_val = os.environ.get("SESSION_CONTROL_PROFILES_PATH") or os.environ.get(
        "GEMINI_PROFILES_CONFIG"
    )
    if env_val:
        return Path(env_val).expanduser().resolve()
    return DEFAULT_CONFIG_PATH


def get_usage_db_path() -> Path:
    env_val = os.environ.get("SESSION_CONTROL_USAGE_DB") or os.environ.get("GEMINI_USAGE_DB")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return DEFAULT_USAGE_DB_PATH


def _detect_known_accounts() -> list[str]:
    accounts_file = DEFAULT_GEMINI_DIR / "google_accounts.json"
    if not accounts_file.is_file():
        return []
    try:
        with open(accounts_file, encoding="utf-8") as f:
            data = json.load(f)
        emails: list[str] = []
        if isinstance(data, dict):
            active = data.get("active")
            if active and isinstance(active, str):
                emails.append(active)
            for old in data.get("old", []):
                if isinstance(old, str) and old not in emails:
                    emails.append(old)
        return emails
    except Exception:
        return []


def load_profiles(config_path: Path | None = None) -> ProfileRegistry:
    path = config_path or get_profiles_config_path()
    if not path.is_file():
        registry = ProfileRegistry()
        now = datetime.now(timezone.utc).isoformat()
        known = _detect_known_accounts()
        primary_email = known[0] if known else ""
        registry.profiles["default"] = ProfileEntry(
            name="default",
            email=primary_email,
            gemini_dir=str(DEFAULT_GEMINI_DIR),
            description="Default system profile",
            enabled=True,
            created_at=now,
        )
        if len(known) > 1:
            for second in known[1:]:
                clean_name = second.split("@")[0].replace(".", "-").replace("_", "-")
                if not clean_name or clean_name in registry.profiles:
                    clean_name = f"account-{clean_name}"
                registry.profiles[clean_name] = ProfileEntry(
                    name=clean_name,
                    email=second,
                    gemini_dir=str(DEFAULT_GEMINI_DIR / "profiles" / clean_name),
                    description=f"Auto-discovered profile for {second}",
                    enabled=True,
                    created_at=now,
                )
                if "umich" in second.lower():
                    registry.routes.append(
                        ProfileRouteRule(pattern="*university-coursework*", profile=clean_name)
                    )
                    registry.routes.append(
                        ProfileRouteRule(pattern="*research-repos*", profile=clean_name)
                    )
        save_profiles(registry, path)
        return registry

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return ProfileRegistry()

    profiles: dict[str, ProfileEntry] = {}
    for name, p_data in raw.get("profiles", {}).items():
        if isinstance(p_data, dict):
            profiles[name] = ProfileEntry(
                name=name,
                email=str(p_data.get("email") or ""),
                gemini_dir=str(p_data.get("gemini_dir") or ""),
                description=str(p_data.get("description") or ""),
                quota_limit_daily=p_data.get("quota_limit_daily"),
                enabled=bool(p_data.get("enabled", True)),
                created_at=str(p_data.get("created_at") or ""),
            )

    routes: list[ProfileRouteRule] = []
    for r in raw.get("routes", []):
        if isinstance(r, dict) and r.get("pattern") and r.get("profile"):
            routes.append(ProfileRouteRule(pattern=str(r["pattern"]), profile=str(r["profile"])))

    return ProfileRegistry(
        default_profile=str(raw.get("default_profile") or "default"),
        active_profile=str(raw.get("active_profile") or "default"),
        strategy=str(raw.get("strategy") or "auto"),
        profiles=profiles,
        routes=routes,
        cooldowns={},
    )


def save_profiles(registry: ProfileRegistry, config_path: Path | None = None) -> None:
    path = config_path or get_profiles_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "default_profile": registry.default_profile,
        "active_profile": registry.active_profile,
        "strategy": registry.strategy,
        "profiles": {name: asdict(p) for name, p in registry.profiles.items()},
        "routes": [asdict(r) for r in registry.routes],
    }
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp_path.replace(path)


def resolve_profile_for_context(
    registry: ProfileRegistry,
    cwd: Path | None = None,
    requested_profile: str | None = None,
    failover_exclude: set[str] | None = None,
) -> ProfileEntry:
    exclude = failover_exclude or set()
    now_ts = time.time()
    for prof, until_ts in list(registry.cooldowns.items()):
        if until_ts > now_ts:
            exclude.add(prof)

    if requested_profile and requested_profile in registry.profiles:
        candidate = registry.profiles[requested_profile]
        if candidate.enabled and candidate.name not in exclude:
            return candidate

    target_dir = str((cwd or Path.cwd()).resolve())
    for route in registry.routes:
        if fnmatch.fnmatch(target_dir, route.pattern) or fnmatch.fnmatch(
            os.path.basename(target_dir), route.pattern
        ):
            candidate = registry.profiles.get(route.profile)
            if candidate and candidate.enabled and candidate.name not in exclude:
                return candidate

    active = registry.profiles.get(registry.active_profile)
    if active and active.enabled and active.name not in exclude:
        return active

    default_p = registry.profiles.get(registry.default_profile)
    if default_p and default_p.enabled and default_p.name not in exclude:
        return default_p

    for candidate in registry.profiles.values():
        if candidate.enabled and candidate.name not in exclude:
            return candidate

    if registry.profiles:
        return next(iter(registry.profiles.values()))

    fallback = ProfileEntry(name="default", gemini_dir=str(DEFAULT_GEMINI_DIR), enabled=True)
    registry.profiles["default"] = fallback
    return fallback


def init_usage_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_usage_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            name TEXT PRIMARY KEY,
            email TEXT,
            gemini_dir TEXT,
            created_at TEXT
        );
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            source TEXT DEFAULT '',
            step_type TEXT DEFAULT '',
            UNIQUE(conversation_id, step_index)
        );
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions_summary (
            conversation_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            title TEXT DEFAULT '',
            workspace TEXT DEFAULT '',
            step_count INTEGER DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            total_cache_read_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            first_event_at TEXT,
            last_event_at TEXT,
            updated_at TEXT
        );
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS harvest_state (
            profile TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            transcript_mtime REAL NOT NULL,
            step_count INTEGER NOT NULL,
            PRIMARY KEY(profile, conversation_id)
        );
        """)
    conn.commit()
    return conn


def _read_session_metadata_from_index(agy_root: Path) -> dict[str, dict[str, Any]]:
    index_db = agy_root / "conversation_summaries.db"
    results: dict[str, dict[str, Any]] = {}
    if not index_db.is_file():
        return results
    try:
        conn = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for row in cur.execute(
            "SELECT conversation_id, title, workspace_uris, step_count, last_modified_time FROM conversation_summaries"
        ):
            cid = str(row["conversation_id"])
            results[cid] = {
                "title": str(row["title"] or ""),
                "workspace": str(row["workspace_uris"] or ""),
                "step_count": int(row["step_count"] or 0),
                "last_modified": str(row["last_modified_time"] or ""),
            }
        conn.close()
    except Exception:
        pass
    return results


def harvest_usage(
    registry: ProfileRegistry | None = None,
    db_path: Path | None = None,
) -> dict[str, int]:
    reg = registry or load_profiles()
    conn = init_usage_db(db_path)
    cur = conn.cursor()

    stats = {"new_steps": 0, "conversations_processed": 0}

    for prof_name, prof_entry in reg.profiles.items():
        cur.execute(
            "INSERT OR REPLACE INTO profiles (name, email, gemini_dir, created_at) VALUES (?, ?, ?, ?)",
            (
                prof_entry.name,
                prof_entry.email,
                str(prof_entry.get_gemini_path()),
                prof_entry.created_at,
            ),
        )

        agy_root = prof_entry.get_agy_root()
        brain_dir = agy_root / "brain"
        if not brain_dir.is_dir():
            continue

        index_metadata = _read_session_metadata_from_index(agy_root)

        for conv_item in brain_dir.iterdir():
            if not conv_item.is_dir():
                continue
            conv_id = conv_item.name
            transcript_file = conv_item / ".system_generated" / "logs" / "transcript.jsonl"
            if not transcript_file.is_file():
                continue

            try:
                mtime = transcript_file.stat().st_mtime
            except OSError:
                continue

            cur.execute(
                "SELECT transcript_mtime, step_count FROM harvest_state WHERE profile = ? AND conversation_id = ?",
                (prof_name, conv_id),
            )
            state_row = cur.fetchone()
            if state_row and abs(state_row[0] - mtime) < 0.001:
                continue

            steps_to_insert = []
            tot_in = 0
            tot_out = 0
            tot_cache = 0
            first_time = ""
            last_time = ""
            count = 0

            try:
                with open(transcript_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            step = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        s_idx = step.get("step_index", count)
                        s_time = str(step.get("created_at") or "")
                        in_tok = int(step.get("input_tokens") or 0)
                        out_tok = int(step.get("output_tokens") or 0)
                        cache_tok = int(step.get("cache_read_tokens") or 0)
                        src = str(step.get("source") or "")
                        stype = str(step.get("type") or "")

                        if not first_time and s_time:
                            first_time = s_time
                        if s_time:
                            last_time = s_time

                        tot_in += in_tok
                        tot_out += out_tok
                        tot_cache += cache_tok
                        count += 1

                        if in_tok > 0 or out_tok > 0 or cache_tok > 0:
                            steps_to_insert.append(
                                (
                                    prof_name,
                                    conv_id,
                                    s_idx,
                                    s_time,
                                    in_tok,
                                    out_tok,
                                    cache_tok,
                                    src,
                                    stype,
                                )
                            )
            except Exception:
                continue

            if steps_to_insert:
                cur.executemany(
                    """
                    INSERT INTO usage_events (
                        profile, conversation_id, step_index, created_at,
                        input_tokens, output_tokens, cache_read_tokens, source, step_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id, step_index) DO UPDATE SET
                        input_tokens=excluded.input_tokens,
                        output_tokens=excluded.output_tokens,
                        cache_read_tokens=excluded.cache_read_tokens,
                        created_at=excluded.created_at
                    """,
                    steps_to_insert,
                )
                stats["new_steps"] += len(steps_to_insert)

            meta = index_metadata.get(conv_id, {})
            title = meta.get("title") or conv_id
            workspace = meta.get("workspace") or ""
            now_iso = datetime.now(timezone.utc).isoformat()

            cur.execute(
                """
                INSERT INTO sessions_summary (
                    conversation_id, profile, title, workspace, step_count,
                    total_input_tokens, total_output_tokens, total_cache_read_tokens,
                    total_tokens, first_event_at, last_event_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    profile=excluded.profile,
                    title=excluded.title,
                    workspace=excluded.workspace,
                    step_count=excluded.step_count,
                    total_input_tokens=excluded.total_input_tokens,
                    total_output_tokens=excluded.total_output_tokens,
                    total_cache_read_tokens=excluded.total_cache_read_tokens,
                    total_tokens=excluded.total_tokens,
                    first_event_at=excluded.first_event_at,
                    last_event_at=excluded.last_event_at,
                    updated_at=excluded.updated_at
                """,
                (
                    conv_id,
                    prof_name,
                    title,
                    workspace,
                    count,
                    tot_in,
                    tot_out,
                    tot_cache,
                    tot_in + tot_out + tot_cache,
                    first_time,
                    last_time,
                    now_iso,
                ),
            )

            cur.execute(
                "INSERT OR REPLACE INTO harvest_state (profile, conversation_id, transcript_mtime, step_count) VALUES (?, ?, ?, ?)",
                (prof_name, conv_id, mtime, count),
            )
            stats["conversations_processed"] += 1

    conn.commit()
    conn.close()
    return stats


def get_usage_summary(
    db_path: Path | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    conn = init_usage_db(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    prof_filter = "WHERE profile = ?" if profile else ""
    params = [profile] if profile else []

    cur.execute(
        f"""
        SELECT
            profile,
            COUNT(DISTINCT conversation_id) as session_count,
            SUM(total_input_tokens) as total_input,
            SUM(total_output_tokens) as total_output,
            SUM(total_cache_read_tokens) as total_cache,
            SUM(total_tokens) as total_tokens,
            MAX(last_event_at) as last_active
        FROM sessions_summary
        {prof_filter}
        GROUP BY profile
        """,
        params,
    )
    profiles_summary = [dict(r) for r in cur.fetchall()]

    cur.execute(
        f"""
        SELECT
            conversation_id, profile, title, workspace, total_input_tokens,
            total_output_tokens, total_cache_read_tokens, total_tokens, last_event_at
        FROM sessions_summary
        {prof_filter}
        ORDER BY last_event_at DESC
        LIMIT 25
        """,
        params,
    )
    recent_sessions = [dict(r) for r in cur.fetchall()]

    conn.close()
    return {
        "profiles": profiles_summary,
        "recent_sessions": recent_sessions,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def execute_with_profile(
    args: list[str],
    requested_profile: str | None = None,
    cwd: Path | None = None,
    auto_failover: bool = True,
    config_path: Path | None = None,
) -> int:
    registry = load_profiles(config_path)
    excluded: set[str] = set()

    max_attempts = len(registry.profiles) if auto_failover else 1
    attempts = 0

    while attempts < max_attempts:
        attempts += 1
        prof = resolve_profile_for_context(
            registry,
            cwd=cwd,
            requested_profile=requested_profile,
            failover_exclude=excluded,
        )

        gemini_dir = prof.get_gemini_path()
        gemini_dir.mkdir(parents=True, exist_ok=True)

        # agy ignores GEMINI_DIR. Without --gemini_dir every profile
        # reuses ~/.gemini and stays on the same Google account.
        cmd = ["agy", *args]
        if not any(arg == "--gemini_dir" or arg.startswith("--gemini_dir=") for arg in args):
            cmd = ["agy", "--gemini_dir", str(gemini_dir), *args]

        env = dict(os.environ)
        env["ANTIGRAVITY_GEMINI_DIR"] = str(gemini_dir)
        env["GEMINI_DIR"] = str(gemini_dir)

        proc = subprocess.run(cmd, env=env, cwd=cwd)

        if proc.returncode == 0 or not auto_failover:
            with contextlib.suppress(Exception):
                harvest_usage(registry)
            return proc.returncode

        # If it failed, check if it was due to quota/rate limiting
        excluded.add(prof.name)
        registry.cooldowns[prof.name] = time.time() + 300

    with contextlib.suppress(Exception):
        harvest_usage(registry)
    return proc.returncode
