"""Codex permission preset helpers."""

from __future__ import annotations

from typing import Any

CODEX_PERMISSION_PRESET_CHOICES = (
    {
        "value": "default",
        "label": "Use Codex config",
        "description": "~/.codex/config.toml and project config decide.",
    },
    {
        "value": "recorded",
        "label": "Recorded",
        "description": "Reuse the session's last recorded sandbox and approval policy.",
    },
    {
        "value": "read-only",
        "label": "Read only",
        "description": "Inspect files only; ask before changes or commands.",
    },
    {
        "value": "auto",
        "label": "Auto",
        "description": "Workspace write with on-request approvals.",
    },
    {
        "value": "full-auto",
        "label": "Full auto",
        "description": "Danger full access with no approval prompts.",
    },
)

_PRESET_VALUES = {choice["value"] for choice in CODEX_PERMISSION_PRESET_CHOICES}
_PRESET_ALIASES = {
    "": "default",
    "config": "default",
    "codex-config": "default",
    "current": "default",
    "session": "recorded",
    "previous": "recorded",
    "workspace": "auto",
    "workspace-write": "auto",
    "full": "full-auto",
    "danger": "full-auto",
    "danger-full-access": "full-auto",
    "yolo": "full-auto",
}
_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}


def normalize_codex_permission_preset(value: str | None) -> str:
    """Return a supported Codex permission preset."""
    raw_value = str(value or "").strip().lower()
    preset = _PRESET_ALIASES.get(raw_value, raw_value)
    if preset in _PRESET_VALUES:
        return preset
    return "default"


def codex_permission_args(preset: str | None, metadata: dict[str, Any]) -> list[str]:
    """Return Codex CLI permission flags for *preset*."""
    normalized = normalize_codex_permission_preset(preset)
    sandbox_mode = ""
    approval_policy = ""
    if normalized == "recorded":
        sandbox_mode = _valid_sandbox(metadata.get("sandbox_mode"))
        approval_policy = _valid_approval(metadata.get("approval_policy"))
    elif normalized == "read-only":
        sandbox_mode = "read-only"
        approval_policy = "on-request"
    elif normalized == "auto":
        sandbox_mode = "workspace-write"
        approval_policy = "on-request"
    elif normalized == "full-auto":
        sandbox_mode = "danger-full-access"
        approval_policy = "never"

    args: list[str] = []
    if sandbox_mode:
        args.extend(["--sandbox", sandbox_mode])
    if approval_policy:
        args.extend(["--ask-for-approval", approval_policy])
    return args


def codex_permission_summary(metadata: dict[str, Any]) -> str:
    """Return a compact recorded permission summary for display."""
    sandbox_mode = _valid_sandbox(metadata.get("sandbox_mode"))
    approval_policy = _valid_approval(metadata.get("approval_policy"))
    if sandbox_mode and approval_policy:
        return f"{sandbox_mode} / {approval_policy}"
    return sandbox_mode or approval_policy


def _valid_sandbox(value: Any) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in _SANDBOX_MODES else ""


def _valid_approval(value: Any) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in _APPROVAL_POLICIES else ""
