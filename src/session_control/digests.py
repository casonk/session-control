"""Small, private records of completed session removals."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from session_control.models import SessionRecord

_CATEGORIES = {
    "code": ("code", "implement", "feature", "refactor", "python", "script"),
    "debugging": ("bug", "fix", "debug", "error", "issue", "failure"),
    "documentation": ("doc", "readme", "guide", "write", "explain"),
    "testing": ("test", "pytest", "verify", "validation", "ci"),
    "operations": ("deploy", "service", "server", "config", "install", "network"),
    "research": ("research", "investigate", "compare", "review", "analyze"),
}


def category_for(session: SessionRecord) -> str:
    """Classify in memory; never persist the transcript, title, or preview."""
    words = f"{session.title} {session.preview}".lower()
    scores = {
        name: sum(words.count(token) for token in tokens) for name, tokens in _CATEGORIES.items()
    }
    category = max(scores, key=scores.get)
    return category if scores[category] else "other"


class DigestStore:
    def __init__(self, root: Path):
        self.root = root

    def record(self, session: SessionRecord, *, reason: str) -> Path:
        now = datetime.now(timezone.utc)
        identifier = hashlib.sha256(
            f"{session.provider}\0{session.session_id}\0{now.isoformat()}".encode()
        ).hexdigest()
        repo = Path(session.workspace).name if session.workspace else "unknown"
        payload = {
            "version": 1,
            "category": category_for(session),
            "repo": repo or "unknown",
            "provider": session.provider,
            "reason": reason,
            "is_subagent": session.is_subagent,
            "message_count": session.message_count,
            "size_bytes": session.size_bytes,
            "ended_at": now.isoformat(),
        }
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        target = self.root / f"{identifier}.json"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
        return target

    def records(self) -> tuple[dict, ...]:
        if not self.root.exists():
            return ()
        records = []
        for path in self.root.glob("*.json"):
            if path.is_symlink():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("version") == 1:
                records.append(record)
        return tuple(records)

    def coverage(self) -> dict:
        records = self.records()
        return {
            "total": len(records),
            "categories": Counter(record.get("category", "other") for record in records),
            "repos": Counter(record.get("repo", "unknown") for record in records),
        }

    def rotate(self, older_than: timedelta, *, dry_run: bool = False) -> int:
        if not self.root.exists():
            return 0
        cutoff = datetime.now(timezone.utc) - older_than
        expired = 0
        for path in self.root.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= cutoff:
                continue
            expired += 1
            if not dry_run:
                path.unlink()
        return expired
