"""Shared agent memory: condense a session into a durable note before it is removed.

Removal (prune, delete, terminate) moves a session's transcript into trash, where it is
eventually expired for good. Condensation runs first and keeps what is worth remembering
as a small, redacted markdown note under a private memory directory. Every assistant on
this machine (Codex, Claude Code, Grok, ...) can read that directory, or query it with
``session-control memory search``, so work done in one tool is not lost to the others.

Two condensers exist behind one interface:

* ``ExtractiveCondenser`` is deterministic and offline: it keeps the opening request, the
  final outcome and the files that came up most. It is always available.
* ``OllamaCondenser`` asks a *loopback* Ollama (the local ``crew-chief`` service) for a
  real summary, and falls back to the extractive result on any failure so condensation
  never depends on a service being up.

Everything written passes through ``redact`` first, and transcripts are never sent to a
non-loopback host.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from session_control.digests import category_for
from session_control.handoff import Turn, recent_turns
from session_control.models import SessionRecord
from session_control.redaction import redact

MAX_TURNS_READ = 100_000
MAX_REQUEST_CHARS = 1500
MAX_OUTCOME_CHARS = 2000
MAX_SUMMARY_CHARS = 4000
MAX_FILES = 15
MAX_LLM_INPUT_CHARS = 12_000
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

_FILE_PATH = re.compile(r"(?<![\w/.:@-])(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}\b")
_TOKEN = re.compile(r"[a-z0-9_]{2,}")


class MemoryStoreError(RuntimeError):
    """The memory store cannot complete the request."""


@dataclass(frozen=True)
class Condensed:
    """What a condenser returns; the store adds redaction and metadata around it."""

    summary: str
    request: str
    outcome: str
    files: tuple[str, ...]
    backend: str


class Condenser(Protocol):
    name: str

    def condense(self, session: SessionRecord, turns: list[Turn]) -> Condensed: ...


class ExtractiveCondenser:
    """Deterministic condenser: no model, no network."""

    name = "extractive"

    def condense(self, session: SessionRecord, turns: list[Turn]) -> Condensed:
        users = [t for t in turns if t.role == "User"]
        assistants = [t for t in turns if t.role == "Assistant"]
        request = users[0].text if users else (session.preview or session.title)
        outcome = assistants[-1].text if assistants else ""
        parts = []
        if request:
            parts.append(f"Started with: {_clip(request, 400)}")
        if outcome:
            parts.append(f"Ended with: {_clip(outcome, 600)}")
        if not parts:
            parts.append("No readable transcript; only session metadata was kept.")
        return Condensed(
            summary="\n\n".join(parts),
            request=_clip(request, MAX_REQUEST_CHARS),
            outcome=_clip(outcome, MAX_OUTCOME_CHARS),
            files=_top_files(turns),
            backend=self.name,
        )


class OllamaCondenser:
    """Summarize with a loopback Ollama service, falling back to extraction on failure."""

    name = "ollama"

    def __init__(self, base_url: str, model: str, *, timeout: int = 120):
        host = urlparse(base_url).hostname or ""
        if host not in LOOPBACK_HOSTS:
            raise MemoryStoreError(
                "Refusing to send session text to a non-loopback condenser; "
                "run the model on this machine."
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._fallback = ExtractiveCondenser()

    def condense(self, session: SessionRecord, turns: list[Turn]) -> Condensed:
        base = self._fallback.condense(session, turns)
        if not turns:
            return base
        try:
            text = self._generate(_llm_prompt(session, turns))
        except (OSError, ValueError, urllib.error.URLError):
            return base
        text = text.strip()
        if not text:
            return base
        return Condensed(
            summary=_clip(text, MAX_SUMMARY_CHARS),
            request=base.request,
            outcome=base.outcome,
            files=base.files,
            backend=f"{self.name}:{self.model}",
        )

    def _generate(self, prompt: str) -> str:
        payload = json.dumps({"model": self.model, "prompt": prompt, "stream": False})
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8", errors="replace"))
        value = body.get("response") if isinstance(body, dict) else None
        if not isinstance(value, str):
            raise ValueError("unexpected condenser response")
        return value


def build_condenser(kind: str, url: str | None, model: str | None) -> Condenser:
    """Pick a condenser from configuration; anything unusable degrades to extractive."""
    if kind in {"ollama", "crew-chief"} and url and model:
        try:
            return OllamaCondenser(url, model)
        except MemoryStoreError:
            return ExtractiveCondenser()
    return ExtractiveCondenser()


@dataclass(frozen=True)
class MemoryEntry:
    entry_id: str
    meta: dict
    body: str
    path: Path

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.entry_id)

    def to_dict(self) -> dict:
        return {"id": self.entry_id, **self.meta, "body": self.body}


class MemoryStore:
    def __init__(self, root: Path):
        self.root = root

    @property
    def entries_dir(self) -> Path:
        return self.root / "entries"

    @property
    def index_path(self) -> Path:
        return self.root / "INDEX.md"

    def condense(
        self,
        session: SessionRecord,
        condenser: Condenser,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> MemoryEntry:
        """Write (or refresh) the note for ``session`` and return it."""
        turns = recent_turns(session, limit=MAX_TURNS_READ)
        condensed = condenser.condense(session, _redact_turns(turns))
        entry_id = f"{session.provider}-{session.public_id}"
        repo = Path(session.workspace).name if session.workspace else ""
        meta = {
            "provider": session.provider,
            "session_id": session.session_id,
            "title": _line(redact(session.title or session.session_id)[0]),
            "repo": repo or "unknown",
            "workspace": redact(session.workspace)[0],
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "condensed_at": (now or datetime.now(timezone.utc)).isoformat(),
            "messages": session.message_count,
            "category": category_for(session),
            "reason": reason,
            "backend": condensed.backend,
        }
        body = _render_body(meta, condensed)
        self._write_private(self.entries_dir / f"{entry_id}.md", _render(meta, body))
        entry = MemoryEntry(entry_id, meta, body, self.entries_dir / f"{entry_id}.md")
        self.write_index()
        return entry

    def entries(self) -> list[MemoryEntry]:
        found: list[MemoryEntry] = []
        if not self.entries_dir.is_dir():
            return found
        for path in self.entries_dir.glob("*.md"):
            entry = self._read(path)
            if entry:
                found.append(entry)
        found.sort(key=lambda e: str(e.meta.get("condensed_at", "")), reverse=True)
        return found

    def get(self, entry_id: str) -> MemoryEntry | None:
        if not re.fullmatch(r"[\w.-]+", entry_id or ""):
            return None
        return self._read(self.entries_dir / f"{entry_id}.md")

    def ids(self) -> set[str]:
        """Entry ids only, from file names: cheap enough for every session-list render."""
        if not self.entries_dir.is_dir():
            return set()
        return {p.stem for p in self.entries_dir.glob("*.md") if not p.is_symlink()}

    def delete(self, entry_id: str) -> bool:
        if self.get(entry_id) is None:
            return False
        (self.entries_dir / f"{entry_id}.md").unlink()
        self.write_index()
        return True

    def search(self, query: str, *, limit: int = 10) -> list[tuple[int, MemoryEntry]]:
        terms = _TOKEN.findall(query.lower())
        if not terms:
            return []
        scored: list[tuple[int, MemoryEntry]] = []
        for entry in self.entries():
            title = entry.title.lower()
            repo = str(entry.meta.get("repo", "")).lower()
            body = entry.body.lower()
            score = sum(3 * title.count(t) + 2 * repo.count(t) + body.count(t) for t in terms)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[:limit]

    def write_index(self) -> Path:
        lines = [
            "# Shared agent memory",
            "",
            "Condensed notes from AI assistant sessions that were removed or archived on",
            "this machine. Search before starting related work:",
            "`session-control memory search <topic>`. Notes are summaries, not transcripts:",
            "verify against the repository before relying on them.",
            "",
        ]
        for entry in self.entries():
            meta = entry.meta
            day = str(meta.get("condensed_at", ""))[:10]
            lines.append(
                f"- [{_line(entry.title)}](entries/{entry.entry_id}.md) — "
                f"{meta.get('provider')} · {meta.get('repo')} · {meta.get('category')} · {day}"
            )
        self._write_private(self.index_path, "\n".join(lines) + "\n")
        return self.index_path

    def _read(self, path: Path) -> MemoryEntry | None:
        if path.is_symlink() or not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        meta, body = _parse(text)
        if not meta:
            return None
        return MemoryEntry(path.stem, meta, body, path)

    def _write_private(self, target: Path, text: str) -> None:
        for directory in (self.root, target.parent):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        temp = target.with_name(f".{target.name}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, target)


def _redact_turns(turns: list[Turn]) -> list[Turn]:
    return [Turn(turn.role, redact(turn.text)[0]) for turn in turns]


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def _line(text: str) -> str:
    return " ".join(text.split())


def _top_files(turns: list[Turn]) -> tuple[str, ...]:
    counts: Counter[str] = Counter()
    for turn in turns:
        counts.update(_FILE_PATH.findall(turn.text))
    return tuple(path for path, _ in counts.most_common(MAX_FILES))


def _llm_prompt(session: SessionRecord, turns: list[Turn]) -> str:
    head, tail = turns[:2], turns[2:]
    budget = MAX_LLM_INPUT_CHARS
    kept: list[Turn] = []
    for turn in reversed(tail):
        cost = min(len(turn.text), 1200)
        if budget - cost < 0:
            break
        budget -= cost
        kept.append(turn)
    chosen = head + list(reversed(kept))
    transcript = "\n\n".join(f"{t.role}: {_clip(t.text, 1200)}" for t in chosen)
    return (
        "Condense this AI coding-assistant session into notes that another assistant can "
        "use later. Reply in markdown with short bullets under these headings: Goal, "
        "Decisions, State at end, Open items. Be factual; do not invent. Never repeat "
        "secrets or tokens.\n\n"
        f"Workspace: {session.workspace or 'unknown'}\n\n"
        f"--- TRANSCRIPT ---\n{transcript}\n--- END ---\n"
    )


def _render_body(meta: dict, condensed: Condensed) -> str:
    lines = [f"# {meta['title']}", "", "## Summary", "", redact(condensed.summary)[0], ""]
    if condensed.request:
        lines += ["## Opening request", "", redact(condensed.request)[0], ""]
    if condensed.outcome:
        lines += ["## Final outcome", "", redact(condensed.outcome)[0], ""]
    if condensed.files:
        lines += ["## Files mentioned", ""]
        lines += [f"- `{redact(path)[0]}`" for path in condensed.files]
        lines.append("")
    return "\n".join(lines)


def _render(meta: dict, body: str) -> str:
    front = "\n".join(f"{key}: {json.dumps(value)}" for key, value in meta.items())
    return f"---\n{front}\n---\n{body}"


def _parse(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    head, separator, body = text[4:].partition("\n---\n")
    if not separator:
        return {}, text
    meta: dict = {}
    for line in head.splitlines():
        key, colon, value = line.partition(": ")
        if not colon:
            continue
        try:
            meta[key] = json.loads(value)
        except ValueError:
            meta[key] = value
    return meta, body
