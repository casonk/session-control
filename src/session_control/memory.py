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

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
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
MAX_ROLLUP_ITEMS = 100
MAX_GIST_CHARS = 350
ROLLUP_PROVIDER = "rollup"
MAX_LLM_INPUT_CHARS = 12_000
logger = logging.getLogger(__name__)
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

_FILE_PATH = re.compile(r"(?<![\w/.:@-])(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}\b")
_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL)
_HEADING = re.compile(r"(?m)^#{1,6}[ \t]+")
_BOILERPLATE = re.compile(r"(?i)^(session notes|notes from .{0,60}|summary)$")
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
    error: str = ""  # why a model condenser fell back to extraction; empty on success


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
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return replace(base, error=f"{type(exc).__name__}: {exc}")
        text = _HEADING.sub("### ", _THINK.sub("", text)).strip()
        if not text:
            return replace(base, error="empty model response")
        return Condensed(
            summary=_clip(text, MAX_SUMMARY_CHARS),
            request=base.request,
            outcome=base.outcome,
            files=base.files,
            backend=f"{self.name}:{self.model}",
        )

    def _generate(self, prompt: str) -> str:
        try:
            return self._post(prompt, think=False)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
        # A model without a reasoning mode rejects the flag: ask again without it.
        return self._post(prompt, think=None)

    def _post(self, prompt: str, *, think: bool | None) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": 8192, "temperature": 0.2},
        }
        if think is not None:
            payload["think"] = think
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8", errors="replace"))
        value = body.get("response") if isinstance(body, dict) else None
        if not isinstance(value, str):
            raise ValueError("unexpected condenser response")
        return value


class _Fixed:
    """Adapter that hands an already-computed result back through the Condenser API."""

    def __init__(self, result: Condensed):
        self.name = result.backend
        self._result = result

    def condense(self, session: SessionRecord, turns: list[Turn]) -> Condensed:
        return self._result


def build_condenser(kind: str, url: str | None, model: str | None) -> Condenser:
    """Pick a condenser from configuration; anything unusable degrades to extractive."""
    if kind in {"ollama", "crew-chief"} and url and model:
        try:
            return OllamaCondenser(url, model)
        except MemoryStoreError:
            return ExtractiveCondenser()
    return ExtractiveCondenser()


@dataclass(frozen=True)
class MergeResult:
    """One workspace's stale notes folded into a rollup."""

    workspace: str
    rollup_id: str
    merged: tuple[str, ...]


@dataclass(frozen=True)
class RetentionResult:
    """What a retention pass removed (or would remove, on a dry run)."""

    notes: tuple[str, ...]
    archived: tuple[str, ...]
    failures: tuple[str, ...]


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
        self._lock = threading.Lock()

    @property
    def entries_dir(self) -> Path:
        return self.root / "entries"

    @property
    def index_path(self) -> Path:
        return self.root / "INDEX.md"

    def read_turns(self, session: SessionRecord) -> list[Turn]:
        """Redacted conversation turns, read once so a slow condenser can run later."""
        return _redact_turns(recent_turns(session, limit=MAX_TURNS_READ))

    def condense(
        self,
        session: SessionRecord,
        condenser: Condenser,
        *,
        reason: str,
        now: datetime | None = None,
        turns: list[Turn] | None = None,
    ) -> MemoryEntry:
        """Write (or refresh) the note for ``session`` and return it."""
        if turns is None:
            turns = self.read_turns(session)
        condensed = condenser.condense(session, turns)
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
        target = self.entries_dir / f"{entry_id}.md"
        with self._lock:
            self._write_private(target, _render(meta, body))
            self._write_index()
            logger.info("wrote note %s backend=%s reason=%s", entry_id, condensed.backend, reason)
            self._track_failure(entry_id, condensed.error, reason)
        return MemoryEntry(entry_id, meta, body, target)

    def condense_deferred(
        self,
        session: SessionRecord,
        fast: Condenser,
        slow: Condenser,
        *,
        reason: str,
        submit: Callable[[Callable[[], object]], object],
    ) -> MemoryEntry:
        """Write the fast note now, then upgrade it with ``slow`` through ``submit``.

        The transcript is read once, here, because removal is about to move it away.
        If the slow condenser fails or only reproduces the extractive result, the fast
        note simply stays.
        """
        turns = self.read_turns(session)
        entry = self.condense(session, fast, reason=reason, turns=turns)

        def upgrade() -> None:
            try:
                upgraded = slow.condense(session, turns)
                if upgraded.backend == fast.name:
                    with self._lock:
                        self._track_failure(entry.entry_id, upgraded.error, reason)
                    return
                self.condense(session, _Fixed(upgraded), reason=reason, turns=turns)
            except Exception as exc:  # noqa: BLE001 - a background upgrade must never raise
                with contextlib.suppress(Exception):
                    self.record_failure(entry.entry_id, f"{type(exc).__name__}: {exc}", reason)

        submit(upgrade)
        return entry

    @property
    def failures_path(self) -> Path:
        return self.root / "failures.json"

    def failures(self) -> dict[str, dict]:
        """Notes whose model pass failed, keyed by entry id (the note is extractive)."""
        try:
            data = json.loads(self.failures_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return (
            {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        )

    def record_failure(self, entry_id: str, error: str, reason: str = "") -> None:
        with self._lock:
            self._track_failure(entry_id, error, reason)

    def clear_failure(self, entry_id: str) -> None:
        with self._lock:
            self._track_failure(entry_id, "", "")

    def _track_failure(self, entry_id: str, error: str, reason: str) -> None:
        """Add or clear one failure record; callers hold the lock."""
        failures = self.failures()
        if error:
            logger.warning("condenser fell back to extraction for %s: %s", entry_id, error)
            previous = failures.get(entry_id, {})
            failures[entry_id] = {
                "error": _clip(_line(redact(error)[0]), 300),
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "attempts": int(previous.get("attempts", 0)) + 1,
            }
        elif entry_id in failures:
            logger.info("condensation failure cleared for %s", entry_id)
            del failures[entry_id]
        else:
            return
        self._write_private(
            self.failures_path, json.dumps(failures, indent=2, sort_keys=True) + "\n"
        )

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
        with self._lock:
            (self.entries_dir / f"{entry_id}.md").unlink(missing_ok=True)
            self._track_failure(entry_id, "", "")
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
        with self._lock:
            return self._write_index()

    def rotate(
        self,
        *,
        older_than: timedelta | None = None,
        max_per_repo: int | None = None,
        dry_run: bool = False,
    ) -> list[MemoryEntry]:
        """Drop notes older than ``older_than`` and/or beyond the newest ``max_per_repo``.

        Age is the time a note was condensed, not a file mtime. Returns what was (or, on a
        dry run, would be) removed.
        """
        entries = self.entries()
        doomed: dict[str, MemoryEntry] = {}
        if older_than is not None:
            cutoff = (datetime.now(timezone.utc) - older_than).isoformat()
            for entry in entries:
                if str(entry.meta.get("condensed_at", "")) < cutoff:
                    doomed[entry.entry_id] = entry
        if max_per_repo is not None:
            seen: Counter[str] = Counter()
            for entry in entries:  # newest first
                repo = str(entry.meta.get("repo", "unknown"))
                seen[repo] += 1
                if seen[repo] > max_per_repo:
                    doomed[entry.entry_id] = entry
        if not dry_run:
            for entry in doomed.values():
                with self._lock:
                    entry.path.unlink(missing_ok=True)
            if doomed:
                self.write_index()
        return list(doomed.values())

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    def merged_ids(self) -> set[str]:
        """Ids of notes folded into a rollup; their sessions are not re-noted by backfills.

        Rollup items count too, so expiring an archived original does not make a backfill
        write that session's note again.
        """
        found: set[str] = set()
        if self.archive_dir.is_dir():
            found.update(p.stem for p in self.archive_dir.glob("*.md") if not p.is_symlink())
        for entry in self.entries():
            if entry.meta.get("provider") == ROLLUP_PROVIDER:
                found.update(
                    str(i["id"]) for i in entry.meta.get("items") or [] if isinstance(i, dict)
                )
        return found

    def expire(
        self,
        *,
        note_retention: timedelta | None = None,
        archive_retention: timedelta | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> RetentionResult:
        """Apply the retention policy: age out notes and rollups, then archived originals.

        A note's age is the last activity of the session it describes (a rollup's is its
        newest session). An archived original's age is the time since it was archived.
        Failure records for notes that no longer exist are dropped. ``None`` skips a limit.
        """
        moment = now or datetime.now(timezone.utc)
        notes = [
            e
            for e in self.entries()
            if note_retention is not None and _entry_activity(e) < moment - note_retention
        ]
        archived: list[Path] = []
        if archive_retention is not None and self.archive_dir.is_dir():
            cutoff = (moment - archive_retention).timestamp()
            archived = [
                p
                for p in self.archive_dir.glob("*.md")
                if not p.is_symlink() and p.stat().st_mtime < cutoff
            ]
        doomed = {e.entry_id for e in notes}
        stale_failures = tuple(
            sorted(
                key
                for key in self.failures()
                if key in doomed or not (self.entries_dir / f"{key}.md").exists()
            )
        )
        if not dry_run:
            with self._lock:
                for entry in notes:
                    entry.path.unlink(missing_ok=True)
                for path in archived:
                    path.unlink(missing_ok=True)
                for key in stale_failures:
                    self._track_failure(key, "", "")
                if notes:
                    self._write_index()
            logger.info(
                "retention removed %d note(s), %d archived original(s), %d failure record(s)",
                len(notes),
                len(archived),
                len(stale_failures),
            )
        return RetentionResult(
            tuple(sorted(e.entry_id for e in notes)),
            tuple(sorted(p.stem for p in archived)),
            stale_failures,
        )

    def merge_stale(
        self,
        *,
        older_than: timedelta,
        min_notes: int = 5,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> list[MergeResult]:
        """Fold old per-session notes of one workspace into a single rollup note.

        A workspace qualifies when at least ``min_notes`` of its notes belong to sessions
        last active before ``now - older_than``. The rollup keeps one short entry per
        session (date, provider, title, gist, files) and is searchable like any note; the
        originals move to ``archive/`` instead of being deleted. Re-running adds newly
        stale notes to the existing rollup.
        """
        cutoff = (now or datetime.now(timezone.utc)) - older_than
        groups: dict[str, list[MemoryEntry]] = {}
        for entry in self.entries():
            workspace = str(entry.meta.get("workspace") or "")
            if (
                entry.meta.get("provider") == ROLLUP_PROVIDER
                or not workspace
                or _entry_activity(entry) >= cutoff
            ):
                continue
            groups.setdefault(workspace, []).append(entry)
        results: list[MergeResult] = []
        for workspace, stale in sorted(groups.items()):
            if len(stale) < max(1, min_notes):
                continue
            rollup_id = _rollup_id(workspace)
            results.append(MergeResult(workspace, rollup_id, tuple(e.entry_id for e in stale)))
            if not dry_run:
                self._merge_group(workspace, rollup_id, stale, now=now)
        return results

    def _merge_group(
        self,
        workspace: str,
        rollup_id: str,
        stale: list[MemoryEntry],
        *,
        now: datetime | None,
    ) -> None:
        existing = self._read(self.entries_dir / f"{rollup_id}.md")
        items: dict[str, dict] = {}
        if existing is not None:
            for item in existing.meta.get("items") or []:
                if isinstance(item, dict) and item.get("id"):
                    items[str(item["id"])] = item
        for entry in stale:
            items[entry.entry_id] = _rollup_item(entry)
        ordered = sorted(items.values(), key=lambda i: str(i.get("date", "")), reverse=True)
        kept = ordered[:MAX_ROLLUP_ITEMS]
        repo = Path(workspace).name or "workspace"
        dates = [str(i["date"]) for i in ordered if i.get("date")]
        meta = {
            "provider": ROLLUP_PROVIDER,
            "session_id": rollup_id,
            "title": _line(redact(f"{repo}: {len(ordered)} earlier sessions")[0]),
            "repo": repo,
            "workspace": redact(workspace)[0],
            "created_at": min(dates) if dates else "",
            "updated_at": max(dates) if dates else "",
            "condensed_at": (now or datetime.now(timezone.utc)).isoformat(),
            "messages": sum(int(i.get("messages") or 0) for i in ordered),
            "category": "rollup",
            "reason": "merge",
            "backend": "merge",
            "items": kept,
        }
        body = _render_rollup(meta, kept, omitted=len(ordered) - len(kept))
        with self._lock:
            self._write_private(self.entries_dir / f"{rollup_id}.md", _render(meta, body))
            self.archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.archive_dir, 0o700)
            for entry in stale:
                archived = self.archive_dir / entry.path.name
                os.replace(entry.path, archived)
                os.utime(archived)  # archive age counts from now, not from the note's write
                self._track_failure(entry.entry_id, "", "")
            self._write_index()
        logger.info("merged %d note(s) for %s into %s", len(stale), workspace, rollup_id)

    def _write_index(self) -> Path:
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


def _entry_activity(entry: MemoryEntry) -> datetime:
    """When the session behind a note was last active (falls back to when it was noted)."""
    for key in ("updated_at", "created_at", "condensed_at"):
        raw = str(entry.meta.get(key) or "")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _rollup_id(workspace: str) -> str:
    slug = re.sub(r"[^\w.-]+", "-", Path(workspace).name).strip("-.")[:40] or "workspace"
    return f"rollup-{slug}-{hashlib.sha1(workspace.encode()).hexdigest()[:8]}"  # noqa: S324


def _section(body: str, heading: str) -> str:
    _head, found, rest = body.partition(f"## {heading}\n")
    return rest.split("\n## ", 1)[0].strip() if found else ""


def _rollup_item(entry: MemoryEntry) -> dict:
    lines = [
        re.sub(r"[#*`>]+", " ", raw).strip() for raw in _section(entry.body, "Summary").splitlines()
    ]
    gist = " ".join(line for line in lines if line and not _BOILERPLATE.match(line))
    files = [
        line.strip()[2:].strip("`")
        for line in _section(entry.body, "Files mentioned").splitlines()
        if line.strip().startswith("- ")
    ][:5]
    return {
        "id": entry.entry_id,
        "date": _entry_activity(entry).date().isoformat(),
        "provider": str(entry.meta.get("provider", "")),
        "title": _line(redact(entry.title)[0])[:120],
        "gist": _clip(_line(redact(gist)[0]), MAX_GIST_CHARS),
        "files": [redact(f)[0] for f in files],
        "messages": int(entry.meta.get("messages") or 0),
    }


def _render_rollup(meta: dict, items: list[dict], *, omitted: int) -> str:
    providers = Counter(str(i.get("provider", "")) for i in items)
    mix = ", ".join(f"{name} {count}" for name, count in providers.most_common())
    lines = [
        f"# {meta['title']}",
        "",
        "## Summary",
        "",
        f"Rollup of {len(items) + omitted} older sessions in `{meta['workspace']}` "
        f"({meta['created_at']} to {meta['updated_at']}; {mix}). One line per session; "
        "the full per-session notes are kept under `archive/`.",
        "",
        "## Sessions",
        "",
    ]
    for item in items:
        gist = f" — {item['gist']}" if item.get("gist") else ""
        lines.append(f"- **{item['date']}** · {item['provider']} · {item['title']}{gist}")
    if omitted:
        lines.append(f"- … {omitted} older session(s) omitted; see `archive/`.")
    lines.append("")
    counts: Counter[str] = Counter(f for item in items for f in item.get("files", []))
    if counts:
        lines += ["## Files mentioned", ""]
        lines += [f"- `{path}`" for path, _ in counts.most_common(MAX_FILES)]
        lines.append("")
    return "\n".join(lines)


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
