"""Runtime configuration for session-control."""

from __future__ import annotations

import os
import secrets
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from session_control.codex_permissions import normalize_codex_permission_preset


@dataclass(frozen=True)
class AppConfig:
    codex_root: Path
    claude_root: Path
    continue_root: Path
    copilot_root: Path
    trash_dir: Path
    secret_key: str
    digest_dir: Path | None = None
    trash_retention_days: int = 7
    digest_retention_days: int = 365
    public_origin: str | None = None
    allowed_origins: tuple[str, ...] = ()
    max_preview_chars: int = 900
    webterm_url: str | None = None
    tmux_session: str = "main"
    codex_resume_model: str | None = None
    codex_permission_preset: str = "default"
    claude_status_poll_enabled: bool = False
    claude_status_command: tuple[str, ...] = ("claude", "auth", "status", "--json")
    claude_status_poll_interval_seconds: int = 300
    claude_status_timeout_seconds: int = 10
    scan_cache_ttl_seconds: float = 5.0
    monitor_targets: tuple[str, ...] = ()
    monitor_interval_seconds: float = 5.0
    monitor_webterm_client_cert: Path | None = None
    monitor_webterm_client_key: Path | None = None
    monitor_webterm_ca_cert: Path | None = None
    federation_node_id: str = "local"
    federation_node_label: str = "Local machine"
    federation_store_dir: Path | None = None
    federation_tokens: dict[str, str] = field(default_factory=dict)
    crew_chief_registry: Path | None = None

    @classmethod
    def from_env(cls) -> AppConfig:
        home = Path.home()
        return cls(
            codex_root=_path_env("SESSION_CONTROL_CODEX_ROOT", home / ".codex"),
            claude_root=_path_env("SESSION_CONTROL_CLAUDE_ROOT", home / ".claude"),
            continue_root=_path_env("SESSION_CONTROL_CONTINUE_ROOT", home / ".continue"),
            copilot_root=_path_env("SESSION_CONTROL_COPILOT_ROOT", home / ".copilot"),
            trash_dir=_path_env(
                "SESSION_CONTROL_TRASH_DIR",
                home / ".local" / "share" / "session-control" / "trash",
            ),
            secret_key=os.environ.get("SESSION_CONTROL_SECRET_KEY") or secrets.token_hex(32),
            digest_dir=_path_env(
                "SESSION_CONTROL_DIGEST_DIR",
                home / ".local" / "share" / "session-control" / "digests",
            ),
            trash_retention_days=_int_env("SESSION_CONTROL_TRASH_RETENTION_DAYS", 7),
            digest_retention_days=_int_env("SESSION_CONTROL_DIGEST_RETENTION_DAYS", 365),
            public_origin=os.environ.get("SESSION_CONTROL_PUBLIC_ORIGIN") or None,
            allowed_origins=_split_csv(os.environ.get("SESSION_CONTROL_ALLOWED_ORIGINS", "")),
            max_preview_chars=int(os.environ.get("SESSION_CONTROL_MAX_PREVIEW_CHARS", "900")),
            webterm_url=os.environ.get("SESSION_CONTROL_WEBTERM_URL") or None,
            tmux_session=os.environ.get("SESSION_CONTROL_TMUX_SESSION") or "main",
            codex_resume_model=os.environ.get("SESSION_CONTROL_CODEX_RESUME_MODEL") or None,
            codex_permission_preset=normalize_codex_permission_preset(
                os.environ.get("SESSION_CONTROL_CODEX_PERMISSION_PRESET")
            ),
            claude_status_poll_enabled=_truthy(
                os.environ.get("SESSION_CONTROL_CLAUDE_STATUS_POLL")
            ),
            claude_status_command=_command_env(
                "SESSION_CONTROL_CLAUDE_STATUS_COMMAND",
                ("claude", "auth", "status", "--json"),
            ),
            claude_status_poll_interval_seconds=_int_env(
                "SESSION_CONTROL_CLAUDE_STATUS_POLL_INTERVAL", 300
            ),
            claude_status_timeout_seconds=_int_env("SESSION_CONTROL_CLAUDE_STATUS_TIMEOUT", 10),
            scan_cache_ttl_seconds=_float_env("SESSION_CONTROL_SCAN_CACHE_TTL", 5.0),
            monitor_targets=_split_csv(os.environ.get("SESSION_CONTROL_MONITOR_TARGETS", "")),
            monitor_interval_seconds=_float_env("SESSION_CONTROL_MONITOR_INTERVAL", 5.0),
            monitor_webterm_client_cert=_optional_path_env(
                "SESSION_CONTROL_MONITOR_WEBTERM_CLIENT_CERT"
            ),
            monitor_webterm_client_key=_optional_path_env(
                "SESSION_CONTROL_MONITOR_WEBTERM_CLIENT_KEY"
            ),
            monitor_webterm_ca_cert=_optional_path_env("SESSION_CONTROL_MONITOR_WEBTERM_CA_CERT"),
            federation_node_id=os.environ.get("SESSION_CONTROL_FEDERATION_NODE_ID", "local"),
            federation_node_label=os.environ.get(
                "SESSION_CONTROL_FEDERATION_NODE_LABEL", "Local machine"
            ),
            federation_store_dir=_optional_path_env("SESSION_CONTROL_FEDERATION_STORE_DIR"),
            federation_tokens=_token_map(os.environ.get("SESSION_CONTROL_FEDERATION_TOKENS", "")),
            crew_chief_registry=_path_env(
                "SESSION_CONTROL_CREW_CHIEF_REGISTRY", _crew_chief_registry_default(home)
            ),
        )

    def provider_root(self, provider: str) -> Path:
        roots = {
            "codex": self.codex_root,
            "claude": self.claude_root,
            "continue": self.continue_root,
            "copilot": self.copilot_root,
        }
        return roots[provider]


def _path_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default
    return Path(value).expanduser()


def _crew_chief_registry_default(home: Path) -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else home / ".local" / "state"
    return base / "crew-chief" / "code-sessions.jsonl"


def _optional_path_env(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _token_map(value: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for item in _split_csv(value):
        node_id, separator, token = item.partition("=")
        if separator and node_id.strip() and token.strip():
            tokens[node_id.strip().lower()] = token.strip()
    return tokens


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        return default


def _command_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.environ.get(name)
    if not value:
        return default
    parts = tuple(shlex.split(value))
    return parts or default
