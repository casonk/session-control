"""Read the machine's lease and load state from the ``ecu`` command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _binary() -> str | None:
    configured = os.environ.get("SESSION_CONTROL_ECU_BIN", "").strip()
    for candidate in (configured, shutil.which("ecu") or "", str(Path.home() / ".local/bin/ecu")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def read_status(timeout: int = 20) -> tuple[dict[str, Any] | None, str]:
    """``ecu status --json`` as a document, or ``(None, reason)`` when unavailable."""
    binary = _binary()
    if binary is None:
        return None, "the ecu command was not found (set SESSION_CONTROL_ECU_BIN)"
    try:
        result = subprocess.run(
            [binary, "status", "--json"], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return None, f"ecu status timed out after {timeout}s"
    try:
        document = json.loads(result.stdout)
    except ValueError:
        document = None
    if result.returncode != 0 or not isinstance(document, dict):
        lines = (result.stderr or result.stdout).strip().splitlines()
        return None, lines[-1] if lines else f"ecu status exited {result.returncode}"
    return document, ""
