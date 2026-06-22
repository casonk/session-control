"""State-changing operations for session records."""

from __future__ import annotations

import contextlib
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
from session_control.dedup import DEFAULT_SIMILARITY, find_duplicate_groups
from session_control.models import ScanError, SessionRecord
from session_control.scanner import SessionScanner


class SessionActionError(RuntimeError):
    """Raised when a requested session action cannot be completed."""


@dataclass(frozen=True)
class OpenResult:
    session: SessionRecord
    window_index: int | None = None


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


@dataclass(frozen=True)
class MergeResult:
    kept: SessionRecord
    absorbed: tuple[SessionRecord, ...]
    moved_to: Path


@dataclass(frozen=True)
class DeduplicateResult:
    min_age: timedelta
    max_age: timedelta
    dry_run: bool
    scanned_count: int
    groups: tuple[tuple[SessionRecord, ...], ...]
    merged: tuple[MergeResult, ...]
    errors: tuple[str, ...]


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
        self._ensure_tmux_session()
        result = subprocess.run(
            [
                "tmux",
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}\t#{window_index}",
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
        window_index: int | None = None
        output_line = result.stdout.decode(errors="replace").strip().splitlines()[-1:]
        if output_line:
            parts = output_line[0].split("\t", 1)
            select = subprocess.run(
                ["tmux", "select-window", "-t", parts[0]],
                capture_output=True,
            )
            if select.returncode != 0:
                err = select.stderr.decode(errors="replace").strip()
                raise SessionActionError(f"Could not select tmux window: {err}")
            if len(parts) > 1:
                with contextlib.suppress(ValueError):
                    window_index = int(parts[1])
        return OpenResult(session=session, window_index=window_index)

    def open_many_in_webterm(
        self, public_ids: tuple[str, ...], *, codex_permission_preset: str | None = None
    ) -> BulkOpenResult:
        deduped = _dedupe(public_ids)
        session_map, lookup_errors = self._find_many(deduped)
        opened: list[OpenResult] = []
        errors: list[str] = list(lookup_errors)
        for public_id in deduped:
            session = session_map.get(public_id)
            if session is None:
                continue
            command = _launch_command(
                session,
                codex_permission_preset=codex_permission_preset
                or self.config.codex_permission_preset,
            )
            window_name = session.title[:20].strip() or session.session_id[:12]
            try:
                self._ensure_tmux_session()
                result = subprocess.run(
                    [
                        "tmux",
                        "new-window",
                        "-d",
                        "-P",
                        "-F",
                        "#{window_id}\t#{window_index}",
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
                win_index: int | None = None
                output_line = result.stdout.decode(errors="replace").strip().splitlines()[-1:]
                if output_line:
                    parts = output_line[0].split("\t", 1)
                    select = subprocess.run(
                        ["tmux", "select-window", "-t", parts[0]],
                        capture_output=True,
                    )
                    if select.returncode != 0:
                        err = select.stderr.decode(errors="replace").strip()
                        raise SessionActionError(f"Could not select tmux window: {err}")
                    if len(parts) > 1:
                        with contextlib.suppress(ValueError):
                            win_index = int(parts[1])
                opened.append(OpenResult(session=session, window_index=win_index))
            except SessionActionError as exc:
                errors.append(f"{public_id}: {exc}")
        return BulkOpenResult(opened=tuple(opened), errors=tuple(errors))

    def delete(self, public_id: str) -> DeleteResult:
        session = self._find(public_id)
        return self._delete_session(session)

    def delete_many(self, public_ids: tuple[str, ...]) -> BulkDeleteResult:
        deduped = _dedupe(public_ids)
        session_map, lookup_errors = self._find_many(deduped)
        deleted: list[DeleteResult] = []
        errors: list[str] = list(lookup_errors)
        for public_id in deduped:
            session = session_map.get(public_id)
            if session is None:
                continue
            try:
                deleted.append(self._delete_session(session))
            except SessionActionError as exc:
                errors.append(f"{public_id}: {exc}")
        return BulkDeleteResult(deleted=tuple(deleted), errors=tuple(errors))

    def deduplicate(
        self,
        min_age: timedelta,
        max_age: timedelta,
        *,
        similarity: float = DEFAULT_SIMILARITY,
        providers: tuple[str, ...] | None = None,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> DeduplicateResult:
        if min_age <= timedelta(0) or max_age <= timedelta(0):
            raise SessionActionError("Age bounds must be greater than zero.")
        if min_age >= max_age:
            raise SessionActionError("min_age must be less than max_age.")
        report = self.scanner.scan(providers=providers)
        reference = _to_utc(now or datetime.now(timezone.utc))
        raw_groups = find_duplicate_groups(
            report.sessions, min_age, max_age, similarity=similarity, now=reference
        )
        groups = tuple(tuple(g) for g in raw_groups)
        merged: list[MergeResult] = []
        errors: list[str] = []

        if not dry_run:
            for group in raw_groups:
                try:
                    merged.append(self._merge_group(group))
                except SessionActionError as exc:
                    ids = ", ".join(s.session_id[:8] for s in group)
                    errors.append(f"group [{ids}]: {exc}")

        return DeduplicateResult(
            min_age=min_age,
            max_age=max_age,
            dry_run=dry_run,
            scanned_count=len(report.sessions),
            groups=groups,
            merged=tuple(merged),
            errors=tuple(errors),
        )

    def _merge_group(self, group: list[SessionRecord]) -> MergeResult:
        if any(s.active for s in group):
            raise SessionActionError("Refusing to merge a group that contains an active session.")
        # Newest session receives the merged content; older ones are absorbed.
        sorted_group = sorted(group, key=lambda s: s.updated_at or s.created_at or "")
        absorbed = sorted_group[:-1]
        kept = sorted_group[-1]
        provider = kept.provider
        provider_root = self.config.provider_root(provider)
        batch_dir = self._trash_batch_dir(kept)

        if provider in ("claude", "codex"):
            merged_content = _merge_jsonl(sorted_group)
            try:
                kept.primary_path.write_bytes(merged_content)
            except OSError as exc:
                raise SessionActionError(f"Could not write merged session: {exc}") from exc
        elif provider == "continue":
            merged_data = _merge_continue_json(sorted_group)
            try:
                kept.primary_path.write_text(
                    json.dumps(merged_data, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                raise SessionActionError(f"Could not write merged session: {exc}") from exc
        # Copilot (directory): content merge not supported — just trash the older ones.

        for session in absorbed:
            for target in session.delete_targets:
                if not target.exists():
                    continue
                if not _is_under(target, provider_root):
                    raise SessionActionError(
                        f"Refusing to delete path outside provider root: {target}"
                    )
                destination = batch_dir / session.session_id[:12] / target.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(destination))
            if provider == "codex":
                _remove_codex_index_entry(provider_root / "session_index.jsonl", session.session_id)
            if provider == "continue":
                _remove_continue_index_entry(
                    provider_root / "sessions" / "sessions.json", session.session_id
                )

        return MergeResult(kept=kept, absorbed=tuple(absorbed), moved_to=batch_dir)

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

    def _ensure_tmux_session(self) -> None:
        check = subprocess.run(
            ["tmux", "has-session", "-t", self.config.tmux_session],
            capture_output=True,
        )
        if check.returncode == 0:
            return
        create = subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.config.tmux_session],
            capture_output=True,
        )
        if create.returncode != 0:
            err = create.stderr.decode(errors="replace").strip()
            raise SessionActionError(f"Could not create tmux session: {err}")

    def _find(self, public_id: str) -> SessionRecord:
        for session in self.scanner.scan().sessions:
            if session.public_id == public_id:
                return session
        raise SessionActionError("Session was not found. Refresh and try again.")

    def _find_many(self, public_ids: tuple[str, ...]) -> tuple[dict[str, SessionRecord], list[str]]:
        wanted = set(public_ids)
        found: dict[str, SessionRecord] = {}
        for session in self.scanner.scan().sessions:
            pid = session.public_id
            if pid in wanted:
                found[pid] = session
        errors = [
            f"{pid}: Session was not found. Refresh and try again."
            for pid in public_ids
            if pid not in found
        ]
        return found, errors

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


def _merge_jsonl(sessions: list[SessionRecord]) -> bytes:
    """Concatenate JSONL content from sessions in chronological order."""
    lines: list[str] = []
    for session in sessions:
        try:
            for raw_line in session.primary_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if raw_line.strip():
                    lines.append(raw_line)
        except OSError as exc:
            raise SessionActionError(f"Could not read {session.primary_path}: {exc}") from exc
    return ("\n".join(lines) + "\n").encode("utf-8")


def _merge_continue_json(sessions: list[SessionRecord]) -> dict:
    """Merge Continue session JSON files, combining history arrays chronologically."""
    import json as _json

    all_history: list = []
    base: dict = {}
    for session in sessions:
        try:
            data = _json.loads(session.primary_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            raise SessionActionError(f"Could not read {session.primary_path}: {exc}") from exc
        if not isinstance(data, dict):
            continue
        base = data  # last (newest) session provides the base metadata
        history = data.get("history")
        if isinstance(history, list):
            all_history.extend(history)
    base["history"] = all_history
    base["messageCount"] = len(all_history)
    return base
