"""Process logging for session-control, following the portfolio Python logging convention.

One ``session_control`` logger tree, plain stdlib ``logging`` to stderr (systemd captures
that in the journal), level from ``--log-level`` or ``SESSION_CONTROL_LOG_LEVEL``. Every
record passes through ``redact`` before it is emitted: log lines can carry exception text
and request URLs, and neither may leak a token or session content.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from session_control.redaction import redact

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LEVEL_ENV = "SESSION_CONTROL_LOG_LEVEL"
FILE_ENV = "SESSION_CONTROL_LOG_FILE"
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
MAX_FILE_BYTES = 5 * 1024 * 1024
FILE_BACKUPS = 3


class RedactingFilter(logging.Filter):
    """Mask secrets in the fully formatted message, including exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())[0]
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))[0]
        return True


def resolve_level(value: str | None) -> int:
    name = (value or os.environ.get(LEVEL_ENV) or "INFO").strip().upper()
    return getattr(logging, name if name in _LEVELS else "INFO")


def default_log_path(platform: str | None = None, env: dict[str, str] | None = None) -> Path:
    """The per-user log location each platform expects."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return base / "session-control" / "Logs" / "session-control.log"
    if platform == "darwin":
        return home / "Library" / "Logs" / "session-control" / "session-control.log"
    state = Path(env.get("XDG_STATE_HOME") or home / ".local" / "state")
    return state / "session-control" / "session-control.log"


def resolve_log_file(value: str | None, platform: str | None = None) -> Path | None:
    """Map ``--log-file`` / ``SESSION_CONTROL_LOG_FILE`` to a path, or None for no file.

    ``off`` disables, ``default`` uses the platform location, a path is used as given and
    ``auto`` (the unset default) writes the platform location only where there is no
    journal to read: macOS and Windows. Linux relies on systemd's journal.
    """
    raw = (value if value is not None else os.environ.get(FILE_ENV) or "auto").strip()
    lowered = raw.lower()
    platform = platform or sys.platform
    if lowered in {"", "off", "none", "0", "false"}:
        return None
    if lowered == "auto":
        return default_log_path(platform) if platform in {"win32", "darwin"} else None
    if lowered == "default":
        return default_log_path(platform)
    return Path(raw).expanduser()


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that survives a failed rollover.

    Several processes (the web service and timers) share one file. Windows refuses to
    rename a file another process holds open, so a failed rollover keeps appending to the
    current file instead of raising or losing the record.
    """

    def doRollover(self) -> None:  # noqa: N802 - stdlib name
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


def _open_private_file(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return _SafeRotatingFileHandler(
        path, maxBytes=MAX_FILE_BYTES, backupCount=FILE_BACKUPS, encoding="utf-8", delay=True
    )


def _mark(handler: logging.Handler, kind: str) -> logging.Handler:
    handler._session_control = kind  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RedactingFilter())
    return handler


def configure_logging(level: str | None = None, log_file: str | None = None) -> None:
    """Install redacting stderr (and optionally file) handlers on the root logger.

    Idempotent: a handler of each kind is installed at most once per process. A log file
    that cannot be opened is reported on stderr and skipped, never fatal.
    """
    root = logging.getLogger()
    root.setLevel(resolve_level(level))
    have = {getattr(h, "_session_control", None) for h in root.handlers}
    if "stderr" not in have:
        root.addHandler(_mark(logging.StreamHandler(sys.stderr), "stderr"))
    path = resolve_log_file(log_file)
    if path is not None and "file" not in have:
        try:
            root.addHandler(_mark(_open_private_file(path), "file"))
        except OSError as exc:
            logging.getLogger(__name__).warning("log file %s unavailable: %s", path, exc)
