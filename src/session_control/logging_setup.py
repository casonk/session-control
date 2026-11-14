"""Process logging for session-control, following the portfolio Python logging convention.

One ``session_control`` logger tree, plain stdlib ``logging`` to stderr (systemd captures
that in the journal), level from ``--log-level`` or ``SESSION_CONTROL_LOG_LEVEL``. Every
record passes through ``redact`` before it is emitted: log lines can carry exception text
and request URLs, and neither may leak a token or session content.
"""

from __future__ import annotations

import logging
import os
import sys

from session_control.redaction import redact

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LEVEL_ENV = "SESSION_CONTROL_LOG_LEVEL"
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


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


def configure_logging(level: str | None = None) -> None:
    """Install one redacting stderr handler on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(resolve_level(level))
    for handler in root.handlers:
        if getattr(handler, "_session_control", False):
            return
    handler = logging.StreamHandler(sys.stderr)
    handler._session_control = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
