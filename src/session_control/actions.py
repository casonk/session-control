"""State-changing operations for session records."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from session_control.codex_permissions import (
    codex_permission_args,
    normalize_codex_permission_preset,
)
from session_control.config import AppConfig
from session_control.models import ScanError, SessionRecord
from session_control.scanner import SessionScanner


class SessionActionError(RuntimeError):
    """Raised when a requested session action cannot be completed."""


@dataclass(frozen=True)
class OpenResult:
    session: SessionRecord


@dataclass(frozen=True)
class BulkOpenResult:
    opened: tuple[OpenResult, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class DeleteResult:
    session: SessionRecord
    moved_to: Path
    moved_count: int


@dataclass(frozen=True)
class BulkDeleteResult:
    deleted: tuple[DeleteResult, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class PruneSkipped:
    session: SessionRecord
    reason: str


@dataclass(frozen=True)
class PruneError:
    provider: str
    message: str
    session: SessionRecord | None = None


@dataclass(frozen=True)
class PruneResult:
    cutoff: datetime
    dry_run: bool
    scanned_count: int
    eligible: tuple[SessionRecord, ...]
    deleted: tuple[DeleteResult, ...]
    skipped: tuple[PruneSkipped, ...]
    errors: tuple[PruneError, ...]


class SessionActionService:
    def __init__(self, config: AppConfig, scanner: SessionScanner | None = None):
        self.config = config
        self.scanner = scanner or SessionScanner(config)

    def open_in_webterm(
        self, public_id: str, *, codex_permission_preset: str | None = None
    ) -> OpenResult:
        session = self._find(public_id)
        command = _launch_command(
            session,
            codex_permission_preset=codex_permission_preset or self.config.codex_permission_preset,
        )
        window_name = session.title[:20].strip() or session.session_id[:12]
        result = subprocess.run(
            [
                "tmux",
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}",
                "-t",
                self.config.tmux_session,
                "-n",
                window_name,
                _interactive_shell_command(command),
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace").strip()
            raise SessionActionError(f"Could not open tmux window: {err}")
        window_id = result.stdout.decode(errors="replace").strip().splitlines()[-1:]
        if window_id:
            select = subprocess.run(
                ["tmux", "select-window", "-t", window_id[0]],
                capture_output=True,
            )
            if select.returncode != 0:
                err = select.stderr.decode(errors="replace").strip()
                raise SessionActionError(f"Could not select tmux window: {err}")
        return OpenResult(session=session)

    def open_many_in_webterm(
        self, public_ids: tuple[str, ...], *, codex_permission_preset: str | None = None
    ) -> BulkOpenResult:
        opened: list[OpenResult] = []
        errors: list[str] = []
        for public_id in _dedupe(public_ids):
            try:
                opened.append(
                    self.open_in_webterm(
                        public_id,
                        codex_permission_preset=codex_permission_preset,
                    )
                )
            except SessionActionError as exc:
                errors.append(f"{public_id}: {exc}")
        return BulkOpenResult(opened=tuple(opened), errors=tuple(errors))

    def delete(self, public_id: str) -> DeleteResult:
        session = self._find(public_id)
        return self._delete_session(session)

    def delete_many(self, public_ids: tuple[str, ...]) -> BulkDeleteResult:
        deleted: list[DeleteResult] = []
        errors: list[str] = []
        for public_id in _dedupe(public_ids):
            try:
                deleted.append(self.delete(public_id))
            except SessionActionError as exc:
                errors.append(f"{public_id}: {exc}")
        return BulkDeleteResult(deleted=tuple(deleted), errors=tuple(errors))

    def prune(
        self,
        older_than: timedelta,
        *,
        providers: tuple[str, ...] | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> PruneResult:
        if older_than <= timedelta(0):
            raise SessionActionError("Prune age must be greater than zero.")
        reference_time = _to_utc(now or datetime.now(timezone.utc))
        cutoff = reference_time - older_than
        report = self.scanner.scan(providers=providers)
        eligible: list[SessionRecord] = []
        deleted: list[DeleteResult] = []
        skipped: list[PruneSkipped] = []
        errors = [_prune_error_from_scan_error(error) for error in report.errors]

        for session in report.sessions:
            activity_at = _session_activity_at(session)
            if activity_at is None or activity_at >= cutoff:
                continue
            if session.active:
                skipped.append(PruneSkipped(session=session, reason="active"))
                continue
            eligible.append(session)

        if not dry_run:
            for session in eligible:
                try:
                    deleted.append(self._delete_session(session))
                except SessionActionError as exc:
                    errors.append(
                        PruneError(provider=session.provider, session=session, message=str(exc))
                    )

        return PruneResult(
            cutoff=cutoff,
            dry_run=dry_run,
            scanned_count=len(report.sessions),
            eligible=tuple(eligible),
            deleted=tuple(deleted),
            skipped=tuple(skipped),
            errors=tuple(errors),
        )

    def _delete_session(self, session: SessionRecord) -> DeleteResult:
        if session.active:
            raise SessionActionError("Refusing to delete a session that appears to be active.")
        provider_root = self.config.provider_root(session.provider)
        batch_dir = self._trash_batch_dir(session)
        moved = 0
        for target in session.delete_targets:
            if not target.exists():
                continue
            if not _is_under(target, provider_root):
                raise SessionActionError(f"Refusing to delete path outside provider root: {target}")
            destination = batch_dir / target.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(destination))
            moved += 1
        if session.provider == "codex":
            _remove_codex_index_entry(provider_root / "session_index.jsonl", session.session_id)
        if session.provider == "continue":
            _remove_continue_index_entry(
                provider_root / "sessions" / "sessions.json", session.session_id
            )
        return DeleteResult(session=session, moved_to=batch_dir, moved_count=moved)

    def _find(self, public_id: str) -> SessionRecord:
        for session in self.scanner.scan().sessions:
            if session.public_id == public_id:
                return session
        raise SessionActionError("Session was not found. Refresh and try again.")

    def _trash_batch_dir(self, session: SessionRecord) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_session = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in session.session_id
        )
        return self.config.trash_dir / timestamp / session.provider / safe_session


def _prune_error_from_scan_error(error: ScanError) -> PruneError:
    return PruneError(provider=error.provider, message=error.message)


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _session_activity_at(session: SessionRecord) -> datetime | None:
    return _parse_datetime(session.updated_at) or _parse_datetime(session.created_at)


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return _to_utc(parsed)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _launch_command(session: SessionRecord, *, codex_permission_preset: str | None) -> str:
    if session.provider != "codex":
        return session.resume_command
    model = str(session.metadata.get("resume_model") or "")
    args = ["codex", "resume"]
    if model:
        args.extend(["--model", model])
    args.extend(
        codex_permission_args(
            normalize_codex_permission_preset(codex_permission_preset),
            session.metadata,
        )
    )
    args.append(session.session_id)
    return _command(session.workspace, args)


def _command(workspace: str, args: list[str]) -> str:
    command = " ".join(shlex.quote(part) for part in args)
    if workspace:
        return f"cd {shlex.quote(workspace)} && {command}"
    return command


def _interactive_shell_command(command: str) -> str:
    script = "\n".join(
        [
            command,
            "status=$?",
            'if [ "$status" -ne 0 ]; then',
            '  printf "\\n[session-control] Resume command exited with status %s. Press Ctrl-D to close this window.\\n" "$status"',
            "  exec bash -l",
            "fi",
            'exit "$status"',
        ]
    )
    return "bash -lc " + shlex.quote(script)


def _remove_codex_index_entry(path: Path, session_id: str) -> None:
    if not path.exists():
        return
    kept_lines = []
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                kept_lines.append(raw_line)
                continue
            if str(item.get("id") or "") != session_id:
                kept_lines.append(raw_line)
        path.write_text("\n".join(kept_lines).rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        raise SessionActionError(f"Could not update Codex session index: {exc}") from exc


def _remove_continue_index_entry(path: Path, session_id: str) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionActionError(f"Could not read Continue session index: {exc}") from exc
    if not isinstance(data, list):
        return
    kept = [
        item for item in data if not isinstance(item, dict) or item.get("sessionId") != session_id
    ]
    try:
        path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SessionActionError(f"Could not update Continue session index: {exc}") from exc
