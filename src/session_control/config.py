"""Runtime configuration for session-control."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    codex_root: Path
    claude_root: Path
    continue_root: Path
    copilot_root: Path
    trash_dir: Path
    secret_key: str
    public_origin: str | None = None
    allowed_origins: tuple[str, ...] = ()
    max_preview_chars: int = 900
    webterm_url: str | None = None
    tmux_session: str = "pit-box"
    codex_resume_model: str | None = None

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
            public_origin=os.environ.get("SESSION_CONTROL_PUBLIC_ORIGIN") or None,
            allowed_origins=_split_csv(os.environ.get("SESSION_CONTROL_ALLOWED_ORIGINS", "")),
            max_preview_chars=int(os.environ.get("SESSION_CONTROL_MAX_PREVIEW_CHARS", "900")),
            webterm_url=os.environ.get("SESSION_CONTROL_WEBTERM_URL") or None,
            tmux_session=os.environ.get("SESSION_CONTROL_TMUX_SESSION") or "pit-box",
            codex_resume_model=os.environ.get("SESSION_CONTROL_CODEX_RESUME_MODEL") or None,
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


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())
