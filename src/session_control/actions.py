"""State-changing operations for session records."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from session_control.codex_permissions import (
    codex_permission_args,
    normalize_codex_permission_preset,
)
from session_control.config import AppConfig
from session_control.dedup import DEFAULT_SIMILARITY, find_duplicate_groups
from session_control.digests import DigestStore
from session_control.handoff import (
    HandoffError,
    build_brief,
    handoff_command,
    recent_turns,
    write_brief,
)
from session_control.home_profiles import owning_account_home
from session_control.memory import ExtractiveCondenser, MemoryStore, build_condenser
from session_control.models import ScanError, SessionRecord
from session_control.scanner import SessionScanner

logger = logging.getLogger(__name__)


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
    killed_window_count: int = 0


@dataclass(frozen=True)
class RestoreCandidate:
    trash_id: str
    provider: str
    session_id: str
    moved_at: str
    moved_count: int


@dataclass(frozen=True)
class RestoreResult:
    candidate: RestoreCandidate
    restored_to: tuple[Path, ...]


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
    short_subagent_cutoff: datetime | None = None


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
    def __init__(
        self,
        config: AppConfig,
        scanner: SessionScanner | None = None,
        *,
        background_condense: bool = False,
    ):
        self.config = config
        self.scanner = scanner or SessionScanner(config)
        # A long-lived server must not block a request on a model call: removal writes the
        # fast extractive note first and upgrades it on a worker thread afterwards.
        self.background_condense = background_condense
        self._executor: ThreadPoolExecutor | None = None

    def open_in_webterm(
        self, public_id: str, *, codex_permission_preset: str | None = None
    ) -> OpenResult:
        session = self._find(public_id)
        command = _launch_command(
            session,
            codex_permission_preset=codex_permission_preset or self.config.codex_permission_preset,
        )
        return self._launch_window(session, command)

    def prepare_handoff(self, public_id: str, target: str) -> tuple[SessionRecord, Path, str]:
        """Write the context brief for a session and return its handoff launch command."""
        session = self._find(public_id)
        try:
            brief = build_brief(session, recent_turns(session))
            path = write_brief(self.config.trash_dir.parent / "handoffs", session, brief)
            return session, path, handoff_command(session, target, path)
        except HandoffError as exc:
            raise SessionActionError(str(exc)) from exc
        except OSError as exc:
            raise SessionActionError(f"Could not write the handoff brief: {exc}") from exc

    def handoff_in_webterm(self, public_id: str, target: str) -> OpenResult:
        """Start a new session in another provider that reads this session's brief."""
        session, _path, command = self.prepare_handoff(public_id, target)
        return self._launch_window(session, command, name_prefix=f"{target}:")

    def _launch_window(
        self, session: SessionRecord, command: str, *, name_prefix: str = ""
    ) -> OpenResult:
        window_name = (name_prefix + session.title[:20].strip()) or session.session_id[:12]
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
            _tag_tmux_window(parts[0], session)
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
                    _tag_tmux_window(parts[0], session)
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
        return self._delete_session(session, reason="delete")

    def terminate_current(self, session_id: str) -> DeleteResult:
        """Remove only the exact Codex thread identified by the caller environment."""
        if not session_id:
            raise SessionActionError("CODEX_THREAD_ID is required to identify this session.")
        matches = [
            item
            for item in self.scanner.scan(providers=("codex",)).sessions
            if item.session_id == session_id
        ]
        if len(matches) != 1:
            raise SessionActionError("Current Codex session was not found uniquely.")
        return self._delete_session(matches[0], reason="self-terminate", kill_windows=False)

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
                deleted.append(self._delete_session(session, reason="delete"))
            except SessionActionError as exc:
                errors.append(f"{public_id}: {exc}")
        return BulkDeleteResult(deleted=tuple(deleted), errors=tuple(errors))

    def list_trash(self) -> tuple[RestoreCandidate, ...]:
        if not self.config.trash_dir.exists():
            return ()
        candidates: list[RestoreCandidate] = []
        for manifest_path in self.config.trash_dir.rglob(".session-control-restore.json"):
            if manifest_path.is_symlink():
                continue
            try:
                candidate = _restore_candidate_from_manifest(manifest_path, self.config.trash_dir)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            candidates.append(candidate)
        return tuple(sorted(candidates, key=lambda item: item.trash_id, reverse=True))

    def restore(self, trash_id: str) -> RestoreResult:
        batch_dir = _trash_batch_path(self.config.trash_dir, trash_id)
        manifest_path = batch_dir / ".session-control-restore.json"
        try:
            candidate = _restore_candidate_from_manifest(manifest_path, self.config.trash_dir)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SessionActionError("Trash entry was not found.") from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SessionActionError(f"Trash entry is invalid: {exc}") from exc
        if candidate.trash_id != trash_id:
            raise SessionActionError("Trash entry does not match its restore manifest.")

        provider_root = self.config.provider_root(candidate.provider)
        targets = _restore_targets(manifest, batch_dir, provider_root)
        _preflight_provider_index_restore(candidate, provider_root, manifest)
        for source, destination in targets:
            if not source.exists():
                raise SessionActionError(f"Trash payload is missing: {source.name}")
            if destination.exists():
                raise SessionActionError(f"Refusing to overwrite existing path: {destination}")

        restored: list[Path] = []
        try:
            for source, destination in targets:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                restored.append(destination)
            if candidate.provider == "continue":
                _restore_continue_index_entry(
                    provider_root / "sessions" / "sessions.json",
                    candidate.session_id,
                    restored,
                )
            _restore_provider_index_entry(candidate, provider_root, manifest)
            manifest_path.unlink()
            _remove_empty_trash_dirs(batch_dir, self.config.trash_dir)
        except OSError as exc:
            raise SessionActionError(f"Could not restore session: {exc}") from exc
        finally:
            self._invalidate_scan_cache()
        return RestoreResult(candidate=candidate, restored_to=tuple(restored))

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
        if provider in {"grok", "agy"}:
            raise SessionActionError(
                f"Deduplication is not supported for {provider} session storage."
            )
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
            account_home = owning_account_home(provider_root, session.primary_path)
            if provider == "codex":
                _remove_codex_index_entry(account_home / "session_index.jsonl", session.session_id)
            if provider == "continue":
                _remove_continue_index_entry(
                    account_home / "sessions" / "sessions.json", session.session_id
                )
            try:
                self.digest_store.record(session, reason="merge")
            except OSError as exc:
                raise SessionActionError(f"Could not record merged session: {exc}") from exc

        return MergeResult(kept=kept, absorbed=tuple(absorbed), moved_to=batch_dir)

    def prune(
        self,
        older_than: timedelta,
        *,
        providers: tuple[str, ...] | None = None,
        dry_run: bool = False,
        short_subagents_only: bool = False,
        short_subagent_age: timedelta | None = None,
        now: datetime | None = None,
        condense: bool = True,
    ) -> PruneResult:
        if older_than <= timedelta(0):
            raise SessionActionError("Prune age must be greater than zero.")
        if short_subagent_age is not None and not timedelta(0) < short_subagent_age <= older_than:
            raise SessionActionError(
                "Short subagent age must be positive and no longer than prune age."
            )
        reference_time = _to_utc(now or datetime.now(timezone.utc))
        cutoff = reference_time - older_than
        short_cutoff = reference_time - short_subagent_age if short_subagent_age else None
        report = self.scanner.scan(providers=providers)
        eligible: list[SessionRecord] = []
        deleted: list[DeleteResult] = []
        skipped: list[PruneSkipped] = []
        errors = [_prune_error_from_scan_error(error) for error in report.errors]

        for session in report.sessions:
            if short_subagents_only and not session.is_short_subagent:
                continue
            activity_at = _session_activity_at(session)
            session_cutoff = short_cutoff if short_cutoff and session.is_short_subagent else cutoff
            if activity_at is None or activity_at >= session_cutoff:
                continue
            if session.active:
                skipped.append(PruneSkipped(session=session, reason="active"))
                continue
            eligible.append(session)

        if not dry_run:
            for session in eligible:
                try:
                    deleted.append(self._delete_session(session, reason="prune", condense=condense))
                except SessionActionError as exc:
                    errors.append(
                        PruneError(provider=session.provider, session=session, message=str(exc))
                    )

        logger.info(
            "prune: %d eligible, %d removed, %d errors%s",
            len(eligible),
            len(deleted),
            len(errors),
            " (dry run)" if dry_run else "",
        )
        return PruneResult(
            cutoff=cutoff,
            dry_run=dry_run,
            scanned_count=len(report.sessions),
            eligible=tuple(eligible),
            deleted=tuple(deleted),
            skipped=tuple(skipped),
            errors=tuple(errors),
            short_subagent_cutoff=short_cutoff,
        )

    def _delete_session(
        self,
        session: SessionRecord,
        *,
        reason: str = "delete",
        kill_windows: bool = True,
        condense: bool = True,
    ) -> DeleteResult:
        if session.active:
            logger.warning(
                "refused to remove active session %s-%s", session.provider, session.public_id
            )
            raise SessionActionError("Refusing to delete a session that appears to be active.")
        if condense:
            self._condense_before_removal(session, reason)
        provider_root = self.config.provider_root(session.provider)
        batch_dir = self._trash_batch_dir(session)
        target_mappings: list[tuple[Path, Path]] = []
        for target in session.delete_targets:
            if not target.exists():
                continue
            if not _is_under(target, provider_root):
                raise SessionActionError(f"Refusing to delete path outside provider root: {target}")
            target_mappings.append((target, batch_dir / target.name))
        if not target_mappings:
            raise SessionActionError("Session files were not found. Refresh and try again.")
        killed_window_count = (
            _kill_tmux_windows_for_session(self.config.tmux_session, session) if kill_windows else 0
        )
        provider_metadata = _provider_index_snapshot(session, provider_root)
        _write_restore_manifest(
            session,
            provider_root,
            batch_dir,
            target_mappings,
            provider_metadata=provider_metadata,
        )
        moved = 0
        try:
            for target, destination in target_mappings:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(destination))
                moved += 1
            account_home = owning_account_home(provider_root, session.primary_path)
            if session.provider == "codex":
                _remove_codex_index_entry(account_home / "session_index.jsonl", session.session_id)
            if session.provider == "continue":
                _remove_continue_index_entry(
                    account_home / "sessions" / "sessions.json", session.session_id
                )
            _remove_provider_index_entry(session, provider_root, provider_metadata)
            self.digest_store.record(session, reason=reason)
        except OSError as exc:
            logger.error(
                "removal of %s-%s failed after moving %d file(s): %s",
                session.provider,
                session.public_id,
                moved,
                exc,
            )
            raise SessionActionError(f"Could not finish session removal: {exc}") from exc
        finally:
            self._invalidate_scan_cache()
        logger.info(
            "removed session %s-%s reason=%s files=%d",
            session.provider,
            session.public_id,
            reason,
            moved,
        )
        return DeleteResult(
            session=session,
            moved_to=batch_dir,
            moved_count=moved,
            killed_window_count=killed_window_count,
        )

    @property
    def memory_store(self) -> MemoryStore:
        return MemoryStore(self.config.resolved_memory_dir)

    def condense(self, public_id: str, *, reason: str = "manual"):
        """Write the shared-memory note for one session without removing it."""
        session = self._find(public_id)
        try:
            return self._write_note(session, reason)
        except OSError as exc:
            raise SessionActionError(f"Could not write the memory note: {exc}") from exc

    def condense_all(
        self,
        *,
        providers: tuple[str, ...] | None = None,
        older_than: timedelta | None = None,
        refresh: bool = False,
        stale_only: bool = False,
        failed_only: bool = False,
    ) -> tuple[list[str], int, list[str]]:
        """Write notes for existing sessions. Returns (written ids, skipped, errors).

        Existing notes are kept unless ``refresh`` rewrites them all or ``stale_only``
        rewrites those whose session has changed since the note was written.
        ``failed_only`` redoes just the notes in the failure log; entries whose session
        is gone cannot be retried and are dropped from the log (the note stays).
        """
        cutoff = datetime.now(timezone.utc) - older_than if older_than else None
        have = self.memory_store.ids()
        merged = self.memory_store.merged_ids()
        retention = self.config.memory_note_retention_days
        expiry = datetime.now(timezone.utc) - timedelta(days=retention) if retention else None
        condensed_at = (
            {
                entry.entry_id: _parse_datetime(str(entry.meta.get("condensed_at", "")))
                for entry in self.memory_store.entries()
            }
            if stale_only
            else {}
        )
        written: list[str] = []
        errors: list[str] = []
        skipped = 0
        failed = set(self.memory_store.failures()) if failed_only else set()
        seen: set[str] = set()
        for session in self.scanner.scan(providers=providers).sessions:
            entry_id = f"{session.provider}-{session.public_id}"
            seen.add(entry_id)
            if entry_id in merged and not failed_only:
                skipped += 1  # folded into a rollup; --refresh must not resurrect it
                continue
            seen_at = _session_activity_at(session)
            if expiry and not failed_only and seen_at and _to_utc(seen_at) < expiry:
                skipped += 1  # retention would delete the note again straight away
                continue
            activity = _session_activity_at(session)
            if failed_only:
                if entry_id not in failed or session.active:
                    skipped += 1
                    continue
            elif (
                session.active
                or session.is_short_subagent
                or (entry_id in have and not refresh and not _is_stale(session, condensed_at))
                or (cutoff and (activity is None or activity >= cutoff))
            ):
                skipped += 1
                continue
            try:
                written.append(self._condense_session(session, "backfill").entry_id)
            except OSError as exc:
                logger.error("condense %s failed: %s", entry_id, exc)
                errors.append(f"{entry_id}: {exc}")
        logger.info(
            "condense_all: %d written, %d skipped, %d errors", len(written), skipped, len(errors)
        )
        if failed_only and providers is None:
            for gone in sorted(failed - seen):
                self.memory_store.clear_failure(gone)
        return written, skipped, errors

    def _condenser(self):
        return build_condenser(
            self.config.condenser, self.config.condenser_url, self.config.condenser_model
        )

    def _condense_session(self, session: SessionRecord, reason: str):
        return self.memory_store.condense(session, self._condenser(), reason=reason)

    def _write_note(self, session: SessionRecord, reason: str):
        """Write the note; with a model condenser on a server, defer the model call."""
        slow = self._condenser()
        if self.background_condense and not isinstance(slow, ExtractiveCondenser):
            return self.memory_store.condense_deferred(
                session,
                ExtractiveCondenser(),
                slow,
                reason=reason,
                submit=self._submit_background,
            )
        return self.memory_store.condense(session, slow, reason=reason)

    def _submit_background(self, job) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="condense")
        self._executor.submit(job)

    def _condense_before_removal(self, session: SessionRecord, reason: str) -> None:
        """Keep a memory note before files move; failure blocks removal, never loses data."""
        if not self.config.condense_on_remove or session.is_short_subagent:
            return
        try:
            self._write_note(session, reason)
        except OSError as exc:
            logger.error(
                "condensation before removal failed for %s-%s: %s",
                session.provider,
                session.public_id,
                exc,
            )
            raise SessionActionError(
                f"Could not condense the session before removal ({exc}). "
                "Fix the memory directory or pass --no-condense."
            ) from exc

    @property
    def digest_store(self) -> DigestStore:
        return DigestStore(self.config.digest_dir or self.config.trash_dir.parent / "digests")

    def rotate(self, *, dry_run: bool = False) -> tuple[int, int]:
        """Expire validated restorable trash entries and aged digest records."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.trash_retention_days)
        trash_count = 0
        for candidate in self.list_trash():
            try:
                batch = _trash_batch_path(self.config.trash_dir, candidate.trash_id)
                if any(part.is_symlink() for part in (batch, batch.parent, batch.parent.parent)):
                    continue
                moved = datetime.strptime(candidate.trash_id.split("/")[0], "%Y%m%dT%H%M%SZ")
                if moved.replace(tzinfo=timezone.utc) >= cutoff:
                    continue
                trash_count += 1
                if not dry_run:
                    shutil.rmtree(batch)
                    _remove_empty_trash_dirs(batch.parent, self.config.trash_dir)
            except (OSError, ValueError, SessionActionError):
                # A suspect entry (for example a symlinked folder that leaves the trash) is
                # skipped; it must not stop the rest of the trash from rotating.
                continue
        digests = self.digest_store.rotate(
            timedelta(days=self.config.digest_retention_days), dry_run=dry_run
        )
        return trash_count, digests

    def _invalidate_scan_cache(self) -> None:
        invalidate = getattr(self.scanner, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def _ensure_tmux_session(self) -> None:
        check = subprocess.run(
            ["tmux", "has-session", "-t", self.config.tmux_session],
            capture_output=True,
        )
        if check.returncode == 0:
            return
        command = ["tmux", "new-session", "-d", "-s", self.config.tmux_session]
        prefix = _scope_prefix()
        create = subprocess.run([*prefix, *command], capture_output=True)
        if create.returncode != 0 and prefix:
            # No usable systemd user manager after all: start tmux the plain way.
            create = subprocess.run(command, capture_output=True)
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


def _is_stale(session: SessionRecord, condensed_at: dict[str, datetime | None]) -> bool:
    """True when the session changed after its note was written (False if no note)."""
    noted = condensed_at.get(f"{session.provider}-{session.public_id}")
    activity = _session_activity_at(session)
    return bool(noted and activity and _to_utc(activity) > _to_utc(noted))


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


def _write_restore_manifest(
    session: SessionRecord,
    provider_root: Path,
    batch_dir: Path,
    target_mappings: list[tuple[Path, Path]],
    *,
    provider_metadata: dict | None = None,
) -> None:
    try:
        targets = [
            {
                "trash_path": str(destination.relative_to(batch_dir)),
                "provider_path": str(source.relative_to(provider_root)),
            }
            for source, destination in target_mappings
        ]
        payload = {
            "version": 2 if provider_metadata else 1,
            "provider": session.provider,
            "session_id": session.session_id,
            "moved_at": batch_dir.parts[-3],
            "targets": targets,
        }
        if provider_metadata:
            payload["provider_metadata"] = provider_metadata
        batch_dir.mkdir(parents=True, exist_ok=True)
        (batch_dir / ".session-control-restore.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        raise SessionActionError(f"Could not record restore metadata: {exc}") from exc


def _trash_batch_path(trash_dir: Path, trash_id: str) -> Path:
    candidate = Path(trash_id)
    if (
        candidate.is_absolute()
        or len(candidate.parts) != 3
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise SessionActionError(
            "Trash entry must be a relative timestamp/provider/session-id path."
        )
    batch_dir = trash_dir / candidate
    if not _is_under(batch_dir, trash_dir):
        raise SessionActionError("Trash entry must stay inside the configured trash directory.")
    return batch_dir


def _restore_candidate_from_manifest(manifest_path: Path, trash_dir: Path) -> RestoreCandidate:
    batch_dir = manifest_path.parent
    relative = batch_dir.relative_to(trash_dir)
    if len(relative.parts) != 3:
        raise ValueError("unexpected trash entry path")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    provider = str(manifest.get("provider") or "")
    session_id = str(manifest.get("session_id") or "")
    moved_at = str(manifest.get("moved_at") or "")
    targets = manifest.get("targets")
    if provider not in {"codex", "claude", "continue", "copilot", "grok", "agy"} or not session_id:
        raise ValueError("missing provider or session identifier")
    if not isinstance(targets, list):
        raise ValueError("missing restore targets")
    if relative.parts[1] != provider or relative.parts[2] != _safe_session_id(session_id):
        raise ValueError("restore manifest does not match its directory")
    return RestoreCandidate(
        trash_id=str(relative),
        provider=provider,
        session_id=session_id,
        moved_at=moved_at,
        moved_count=len(targets),
    )


def _restore_targets(
    manifest: object, batch_dir: Path, provider_root: Path
) -> list[tuple[Path, Path]]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("targets"), list):
        raise SessionActionError("Trash entry has no restore targets.")
    targets: list[tuple[Path, Path]] = []
    for item in manifest["targets"]:
        if not isinstance(item, dict):
            raise SessionActionError("Trash entry has an invalid restore target.")
        trash_path = Path(str(item.get("trash_path") or ""))
        provider_path = Path(str(item.get("provider_path") or ""))
        if (
            trash_path.is_absolute()
            or provider_path.is_absolute()
            or not trash_path.parts
            or not provider_path.parts
            or any(part in {"", ".", ".."} for part in (*trash_path.parts, *provider_path.parts))
        ):
            raise SessionActionError("Trash entry contains an unsafe restore path.")
        source = batch_dir / trash_path
        destination = provider_root / provider_path
        if not _is_under(source, batch_dir) or not _is_under(destination, provider_root):
            raise SessionActionError("Trash entry contains an unsafe restore path.")
        targets.append((source, destination))
    if not targets:
        raise SessionActionError("Trash entry has no restorable files.")
    return targets


def _safe_session_id(session_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id)


def _remove_empty_trash_dirs(batch_dir: Path, trash_dir: Path) -> None:
    current = batch_dir
    while current != trash_dir:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _restore_continue_index_entry(index_path: Path, session_id: str, restored: list[Path]) -> None:
    if not restored:
        return
    try:
        data = json.loads(restored[0].read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        summary = {
            key: data[key]
            for key in ("sessionId", "title", "dateCreated", "workspaceDirectory", "messageCount")
            if key in data
        }
        summary["sessionId"] = session_id
        existing = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        if not isinstance(existing, list):
            raise SessionActionError("Continue session index has an unsupported format.")
        if any(
            isinstance(item, dict) and str(item.get("sessionId") or "") == session_id
            for item in existing
        ):
            return
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps([*existing, summary], indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionActionError(f"Could not restore Continue session index: {exc}") from exc


GROK_SEARCH_COLUMNS = ("session_id", "cwd", "updated_at", "title", "content", "content_hash")
AGY_SUMMARY_COLUMNS = (
    "conversation_id",
    "title",
    "preview",
    "step_count",
    "last_modified_time",
    "workspace_uris",
    "status",
    "source",
    "project_id",
    "agent_name",
    "parent_conversation_id",
    "nesting_depth",
    "battle_id",
    "winning_conversation_id",
    "not_fully_idle",
    "killed",
    "last_user_input_time",
    "last_user_input_step_index",
    "app_data_dir",
    "raw_summary",
    "group_id",
)
AGY_FTS_COLUMNS = (
    "conversation_id",
    "chunk_index",
    "chunk_first_step",
    "chunk_last_step",
    "last_modified_time",
    "step_count",
    "last_step_created_at",
    "archived",
    "user_step_byte_offsets",
    "agent_step_byte_offsets",
    "title",
    "workspace",
    "project",
    "user_body",
    "agent_body",
)


def _provider_index_snapshot(session: SessionRecord, provider_root: Path) -> dict[str, Any]:
    if session.provider == "grok":
        path = provider_root / "sessions" / "session_search.sqlite"
        row = _sqlite_select_one(path, "session_docs", GROK_SEARCH_COLUMNS, session.session_id)
        return {"grok_search_doc": _encode_sqlite_row(row)} if row else {}
    if session.provider == "agy":
        path = provider_root / "conversation_summaries.db"
        row = _sqlite_select_one(
            path, "conversation_summaries", AGY_SUMMARY_COLUMNS, session.session_id
        )
        if row is None:
            raise SessionActionError("Google AGY conversation summary was not found.")
        fts_rows = _sqlite_select_all(
            path, "conversations_fts", AGY_FTS_COLUMNS, "conversation_id", session.session_id
        )
        return {
            "agy_summary": _encode_sqlite_row(row),
            "agy_fts_rows": [_encode_sqlite_row(item) for item in fts_rows],
        }
    return {}


def _preflight_provider_index_restore(
    candidate: RestoreCandidate, provider_root: Path, manifest: object
) -> None:
    if candidate.provider not in {"grok", "agy"}:
        return
    metadata = _provider_metadata(manifest)
    if candidate.provider == "grok" and metadata.get("grok_search_doc"):
        _decode_sqlite_row(metadata["grok_search_doc"], GROK_SEARCH_COLUMNS)
    if candidate.provider == "agy":
        if "agy_summary" not in metadata:
            raise SessionActionError("Trash entry has no Google AGY index metadata.")
        _decode_sqlite_row(metadata["agy_summary"], AGY_SUMMARY_COLUMNS)
        rows_value = metadata.get("agy_fts_rows", [])
        if not isinstance(rows_value, list):
            raise SessionActionError("Trash entry has invalid Google AGY search metadata.")
        for row in rows_value:
            _decode_sqlite_row(row, AGY_FTS_COLUMNS)
    path = (
        provider_root / "sessions" / "session_search.sqlite"
        if candidate.provider == "grok"
        else provider_root / "conversation_summaries.db"
    )
    needs_index = candidate.provider == "agy" or bool(metadata.get("grok_search_doc"))
    if needs_index and not path.exists():
        raise SessionActionError(f"Provider index {path.name} was not found.")
    table = "session_docs" if candidate.provider == "grok" else "conversation_summaries"
    key = "session_id" if candidate.provider == "grok" else "conversation_id"
    expected_columns = GROK_SEARCH_COLUMNS if candidate.provider == "grok" else AGY_SUMMARY_COLUMNS
    if needs_index and not set(expected_columns).issubset(_sqlite_table_columns(path, table)):
        raise SessionActionError(f"Provider index {path.name} has an unsupported schema.")
    if (
        candidate.provider == "agy"
        and metadata.get("agy_fts_rows")
        and not set(AGY_FTS_COLUMNS).issubset(_sqlite_table_columns(path, "conversations_fts"))
    ):
        raise SessionActionError(f"Provider index {path.name} has an unsupported search schema.")
    if needs_index and _sqlite_has_key(path, table, key, candidate.session_id):
        raise SessionActionError("Refusing to overwrite an existing provider index entry.")


def _remove_provider_index_entry(
    session: SessionRecord, provider_root: Path, metadata: dict[str, Any]
) -> None:
    if session.provider == "grok" and metadata.get("grok_search_doc"):
        path = provider_root / "sessions" / "session_search.sqlite"
        _sqlite_delete_index_rows(
            path,
            (
                (
                    "session_docs_fts",
                    "rowid IN (SELECT rowid FROM session_docs WHERE session_id = ?)",
                    session.session_id,
                ),
                ("session_docs", "session_id = ?", session.session_id),
            ),
        )
    if session.provider == "agy":
        path = provider_root / "conversation_summaries.db"
        _sqlite_delete_index_rows(
            path,
            (
                ("conversations_fts", "conversation_id = ?", session.session_id),
                ("conversation_summaries", "conversation_id = ?", session.session_id),
            ),
        )


def _restore_provider_index_entry(
    candidate: RestoreCandidate, provider_root: Path, manifest: object
) -> None:
    metadata = _provider_metadata(manifest)
    if candidate.provider == "grok" and metadata.get("grok_search_doc"):
        row = _decode_sqlite_row(metadata["grok_search_doc"], GROK_SEARCH_COLUMNS)
        path = provider_root / "sessions" / "session_search.sqlite"
        _sqlite_insert_rows(path, "session_docs", GROK_SEARCH_COLUMNS, [row])
        with _sqlite_write_connection(path) as connection:
            indexed = connection.execute(
                "SELECT rowid, title, content FROM session_docs WHERE session_id = ?",
                (candidate.session_id,),
            ).fetchone()
            if indexed:
                connection.execute(
                    "INSERT INTO session_docs_fts(rowid, title, content) VALUES (?, ?, ?)",
                    indexed,
                )
        return
    if candidate.provider == "agy":
        summary = _decode_sqlite_row(metadata.get("agy_summary"), AGY_SUMMARY_COLUMNS)
        rows_value = metadata.get("agy_fts_rows", [])
        if not isinstance(rows_value, list):
            raise SessionActionError("Trash entry has invalid Google AGY search metadata.")
        fts_rows = [_decode_sqlite_row(item, AGY_FTS_COLUMNS) for item in rows_value]
        path = provider_root / "conversation_summaries.db"
        _sqlite_insert_rows(path, "conversation_summaries", AGY_SUMMARY_COLUMNS, [summary])
        if fts_rows:
            _sqlite_insert_rows(path, "conversations_fts", AGY_FTS_COLUMNS, fts_rows)


def _provider_metadata(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise SessionActionError("Trash entry has invalid provider metadata.")
    value = manifest.get("provider_metadata", {})
    if not isinstance(value, dict):
        raise SessionActionError("Trash entry has invalid provider metadata.")
    return value


def _sqlite_select_one(
    path: Path, table: str, columns: tuple[str, ...], session_id: str
) -> dict[str, Any] | None:
    rows = _sqlite_select_all(path, table, columns, columns[0], session_id)
    return rows[0] if rows else None


def _sqlite_select_all(
    path: Path, table: str, columns: tuple[str, ...], key: str, value: str
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    names = ", ".join(f'"{name}"' for name in columns)
    try:
        rows = _sqlite_fetchall(
            path,
            f'SELECT {names} FROM "{table}" WHERE "{key}" = ?',
            (value,),
        )
    except sqlite3.Error as exc:
        raise SessionActionError(f"Could not read {path.name} provider index: {exc}") from exc
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _sqlite_has_key(path: Path, table: str, key: str, value: str) -> bool:
    try:
        return bool(
            _sqlite_fetchall(
                path,
                f'SELECT 1 FROM "{table}" WHERE "{key}" = ? LIMIT 1',
                (value,),
            )
        )
    except sqlite3.Error as exc:
        raise SessionActionError(f"Could not inspect {path.name} provider index: {exc}") from exc


def _sqlite_table_columns(path: Path, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in _sqlite_fetchall(path, f'PRAGMA table_info("{table}")')}
    except sqlite3.Error as exc:
        raise SessionActionError(f"Could not inspect {path.name} provider index: {exc}") from exc


def _sqlite_fetchall(
    path: Path, statement: str, parameters: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    uri = path.resolve().as_uri()
    last_error: sqlite3.OperationalError | None = None
    for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{uri}{suffix}", uri=True, timeout=1)
            return connection.execute(statement, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
    assert last_error is not None
    raise last_error


def _sqlite_delete_index_rows(path: Path, operations: tuple[tuple[str, str, str], ...]) -> None:
    try:
        with _sqlite_write_connection(path) as connection:
            for table, clause, value in operations:
                connection.execute(f'DELETE FROM "{table}" WHERE {clause}', (value,))
    except sqlite3.Error as exc:
        raise SessionActionError(f"Could not update {path.name} provider index: {exc}") from exc


def _sqlite_insert_rows(
    path: Path, table: str, columns: tuple[str, ...], rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    names = ", ".join(f'"{name}"' for name in columns)
    placeholders = ", ".join("?" for _ in columns)
    try:
        with _sqlite_write_connection(path) as connection:
            connection.executemany(
                f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                [[row[name] for name in columns] for row in rows],
            )
    except sqlite3.Error as exc:
        raise SessionActionError(f"Could not restore {path.name} provider index: {exc}") from exc


@contextlib.contextmanager
def _sqlite_write_connection(path: Path):
    if not path.exists():
        raise SessionActionError(f"Provider index {path.name} was not found.")
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _encode_sqlite_row(row: dict[str, Any]) -> dict[str, Any]:
    encoded: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, bytes):
            encoded[key] = {"base64": base64.b64encode(value).decode("ascii")}
        elif value is None or isinstance(value, (str, int, float)):
            encoded[key] = value
        else:
            raise SessionActionError("Provider index contains unsupported metadata.")
    return encoded


def _decode_sqlite_row(value: object, columns: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(columns):
        raise SessionActionError("Trash entry has invalid provider index metadata.")
    decoded: dict[str, Any] = {}
    for key in columns:
        item = value[key]
        if isinstance(item, dict) and set(item) == {"base64"}:
            try:
                decoded[key] = base64.b64decode(str(item["base64"]), validate=True)
            except (ValueError, TypeError) as exc:
                raise SessionActionError(
                    "Trash entry has invalid encoded provider metadata."
                ) from exc
        elif item is None or isinstance(item, (str, int, float)):
            decoded[key] = item
        else:
            raise SessionActionError("Trash entry has invalid provider index metadata.")
    return decoded


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
    profile = str(session.metadata.get("profile") or "default")
    if profile != "default":
        args = ["codex-profile", "run", "--profile", profile, *args[1:]]
    return _command(session.workspace, args)


def _scope_prefix() -> list[str]:
    """Run a new tmux server in its own systemd scope when a user manager is available.

    A server started from the web service lives in the service's cgroup, so restarting or
    redeploying the service would kill every terminal window in it. A transient scope
    outlives the service.
    """
    if not os.environ.get("XDG_RUNTIME_DIR") or shutil.which("systemd-run") is None:
        return []
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect"]


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


def _tag_tmux_window(window_id: str, session: SessionRecord) -> None:
    options = {
        "@session_control_public_id": session.public_id,
        "@session_control_provider": session.provider,
        "@session_control_session_id": session.session_id,
    }
    for name, value in options.items():
        result = subprocess.run(
            ["tmux", "set-option", "-w", "-t", window_id, name, value],
            capture_output=True,
        )
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace").strip()
            raise SessionActionError(f"Could not tag tmux window: {err}")


def _kill_tmux_windows_for_session(tmux_session: str, session: SessionRecord) -> int:
    try:
        check = subprocess.run(
            ["tmux", "has-session", "-t", tmux_session],
            capture_output=True,
        )
    except FileNotFoundError:
        return 0
    if check.returncode != 0:
        return 0

    result = subprocess.run(
        [
            "tmux",
            "list-windows",
            "-t",
            tmux_session,
            "-F",
            "#{window_id}\t#{@session_control_public_id}\t"
            "#{@session_control_provider}\t#{@session_control_session_id}",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace").strip()
        raise SessionActionError(f"Could not inspect tmux windows: {err}")

    killed = 0
    for line in result.stdout.decode(errors="replace").splitlines():
        window_id, public_id, provider, session_id = (*line.split("\t"), "", "", "")[:4]
        if public_id != session.public_id and (
            provider != session.provider or session_id != session.session_id
        ):
            continue
        kill = subprocess.run(["tmux", "kill-window", "-t", window_id], capture_output=True)
        if kill.returncode != 0:
            err = kill.stderr.decode(errors="replace").strip()
            raise SessionActionError(f"Could not close tmux window {window_id}: {err}")
        killed += 1
    return killed


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
