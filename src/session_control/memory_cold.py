"""Compressed cold storage for expired and merged memory notes.

Retention and merging never delete a note outright. They append it to a monthly bundle,
``cold/cold-YYYY-MM.tar.gz``, written and re-read before the live file is removed. Bundles
use only the standard library, so they behave the same on Linux, macOS and Windows, and are
read member by member in memory (nothing is ever extracted to disk). Members are named
``entries/<id>.md`` (an expired note) or ``merged/<id>.md`` (an original folded into a
rollup). A bundle is deleted once its whole month is older than the cold retention.
"""

from __future__ import annotations

import io
import os
import re
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_MEMBER_BYTES = 1_000_000
_BUNDLE = re.compile(r"cold-(\d{4})-(\d{2})\.tar\.gz")
_MEMBER = re.compile(r"(entries|merged)/([\w.-]+)\.md")


def bundle_name(moment: datetime) -> str:
    return f"cold-{moment.year:04d}-{moment.month:02d}.tar.gz"


def bundles(directory: Path) -> list[Path]:
    """Bundle files, oldest month first."""
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir() if _BUNDLE.fullmatch(p.name) and not p.is_symlink()]
    return sorted(found, key=lambda p: p.name)


def read_bundle(path: Path) -> dict[str, bytes]:
    """Member name -> bytes for the well-formed members of one bundle."""
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            for info in archive:
                if not info.isfile() or not _MEMBER.fullmatch(info.name):
                    continue
                if info.size > MAX_MEMBER_BYTES:
                    continue
                handle = archive.extractfile(info)
                if handle is not None:
                    members[info.name] = handle.read()
    except (OSError, tarfile.TarError, EOFError):
        return members
    return members


def append(directory: Path, moment: datetime, files: dict[str, bytes]) -> Path:
    """Add ``files`` to the bundle for ``moment``'s month; a repeated name is replaced.

    The bundle is rewritten to a temporary file, read back and compared, and only then
    moved into place, so a crash or a full disk leaves the previous bundle intact.
    """
    for name in files:
        if not _MEMBER.fullmatch(name):
            raise ValueError(f"unsupported cold member name: {name!r}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    target = directory / bundle_name(moment)
    merged = read_bundle(target) if target.exists() else {}
    merged.update(files)
    temp = target.with_name(f".{target.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w:gz") as archive:
        for name, data in sorted(merged.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(moment.timestamp())
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    if read_bundle(temp) != merged:
        temp.unlink(missing_ok=True)
        raise OSError(f"cold bundle {target.name} did not verify after writing")
    os.replace(temp, target)
    return target


def expire(
    directory: Path, retention: timedelta, *, now: datetime, dry_run: bool = False
) -> list[Path]:
    """Delete bundles whose whole month is older than ``retention``."""
    doomed: list[Path] = []
    for path in bundles(directory):
        match = _BUNDLE.fullmatch(path.name)
        year, month = int(match.group(1)), int(match.group(2))
        end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)
        if end < now - retention:
            doomed.append(path)
    if not dry_run:
        for path in doomed:
            path.unlink(missing_ok=True)
    return doomed
