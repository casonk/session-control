"""Data models shared by scanners, actions, and the web UI."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionRecord:
    provider: str
    session_id: str
    title: str
    workspace: str
    created_at: str
    updated_at: str
    primary_path: Path
    delete_targets: tuple[Path, ...]
    size_bytes: int
    message_count: int
    preview: str
    resume_command: str
    active: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def public_id(self) -> str:
        raw = f"{self.provider}\0{self.session_id}\0{self.primary_path}".encode()
        return hashlib.sha256(raw).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_id": self.public_id,
            "provider": self.provider,
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "primary_path": str(self.primary_path),
            "delete_targets": [str(path) for path in self.delete_targets],
            "size_bytes": self.size_bytes,
            "message_count": self.message_count,
            "preview": self.preview,
            "resume_command": self.resume_command,
            "active": self.active,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ScanError:
    provider: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "message": self.message}


@dataclass(frozen=True)
class ScanReport:
    sessions: tuple[SessionRecord, ...]
    errors: tuple[ScanError, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": [session.to_dict() for session in self.sessions],
            "errors": [error.to_dict() for error in self.errors],
        }
