"""Pull claude.ai chats into a local cache through Anthropic's Compliance API.

The Compliance API is available to Claude Enterprise organizations. Chats are read with a
Compliance Access Key created in claude.ai (Organization settings, API); an Admin API key from
the Console is rejected. Each chat is stored as one owner-only JSON file under
``<web_chat_dir>/claude-web/`` so the scanner reads it like any other session store.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API_BASE = "https://api.anthropic.com"
API_VERSION = "2026-10-21"
PROVIDER_DIR = "claude-web"
STATE_FILE = ".sync-state.json"
PAGE_SIZE = 1000
MAX_RETRIES = 5
# Re-read a little before the last sync so a chat updated mid-sync is never missed.
OVERLAP_SECONDS = 3600
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

Opener = Callable[[urllib.request.Request], Any]


class ComplianceError(RuntimeError):
    """The Compliance API refused or failed a request."""


def access_key(env: dict[str, str] | None = None) -> str:
    """The Compliance Access Key from ``SESSION_CONTROL_ANTHROPIC_COMPLIANCE_KEY`` or the file
    named by ``..._KEY_FILE``; empty when neither is set."""
    env = os.environ if env is None else env
    key = env.get("SESSION_CONTROL_ANTHROPIC_COMPLIANCE_KEY", "").strip()
    if key:
        return key
    path = env.get("SESSION_CONTROL_ANTHROPIC_COMPLIANCE_KEY_FILE", "").strip()
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ComplianceError(f"cannot read the key file: {exc.strerror}") from exc
    return ""


@dataclass
class ComplianceClient:
    key: str
    base_url: str = API_BASE
    opener: Opener = field(default=urllib.request.urlopen, repr=False)
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def get(self, path: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(  # noqa: S310 - fixed https base URL
            url,
            headers={
                "x-api-key": self.key,
                "anthropic-version": API_VERSION,
                "accept": "application/json",
            },
        )
        for attempt in range(MAX_RETRIES):
            try:
                with self.opener(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < MAX_RETRIES - 1:
                    self.sleep(_retry_after(exc, attempt))
                    continue
                raise ComplianceError(_http_message(exc)) from exc
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                raise ComplianceError(f"request failed: {exc}") from exc
        raise ComplianceError("rate limited too many times")  # pragma: no cover

    def pages(self, path: str, params: list[tuple[str, str]], item_key: str = "data"):
        """Yield items across ``after_id`` pages."""
        cursor = ""
        while True:
            page = self.get(path, params + ([("after_id", cursor)] if cursor else []))
            yield from page.get(item_key) or []
            if not page.get("has_more") or not page.get("last_id"):
                return
            cursor = str(page["last_id"])


def _retry_after(exc: urllib.error.HTTPError, attempt: int) -> float:
    try:
        return max(1.0, min(120.0, float(exc.headers.get("retry-after", ""))))
    except (TypeError, ValueError):
        return float(min(60, 2**attempt))


def _http_message(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8"))
        detail = str((body.get("error") or {}).get("message") or "")
    except (OSError, ValueError, AttributeError):
        pass
    hints = {
        401: "the key is invalid or deactivated",
        403: "the key lacks read:compliance_user_data, or is an Admin API key "
        "(use a Compliance Access Key from claude.ai)",
        404: "not found; the key did not authenticate, or the Compliance API is not enabled",
    }
    hint = hints.get(exc.code, "")
    return f"HTTP {exc.code}: {detail or exc.reason}" + (f" ({hint})" if hint else "")


def store_dir(web_chat_dir: Path) -> Path:
    return web_chat_dir / PROVIDER_DIR


def message_text(message: dict[str, Any]) -> str:
    """The plain text of one message; tool calls and results are left out."""
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts)


def _write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def _read_state(directory: Path) -> dict[str, Any]:
    try:
        return json.loads((directory / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@dataclass
class SyncResult:
    fetched: int = 0
    skipped: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)


def sync(
    client: ComplianceClient,
    web_chat_dir: Path,
    *,
    user_email: str = "",
    full: bool = False,
    now: Callable[[], float] = time.time,
) -> SyncResult:
    """Fetch chats updated since the last sync (all of them with ``full``)."""
    directory = store_dir(web_chat_dir)
    state = {} if full else _read_state(directory)
    params = [("order_by", "updated_at"), ("limit", str(PAGE_SIZE))]
    since = state.get("synced_at")
    if since:
        params.append(("updated_at.gte", _rfc3339(float(since) - OVERLAP_SECONDS)))
    started = now()
    result = SyncResult()
    wanted = user_email.strip().lower()
    for chat in client.pages("/v1/compliance/apps/chats", params):
        chat_id = str(chat.get("id") or "")
        if not SAFE_ID.match(chat_id):
            continue
        owner = ((chat.get("user") or {}).get("email_address") or "").lower()
        if wanted and owner != wanted:
            result.skipped += 1
            continue
        target = directory / f"{chat_id}.json"
        if chat.get("deleted_at"):
            if target.exists():
                target.unlink()
                result.removed += 1
            continue
        try:
            cached = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if cached.get("updated_at") == chat.get("updated_at") and cached.get("messages"):
            result.skipped += 1
            continue
        try:
            messages = _fetch_messages(client, chat_id)
        except ComplianceError as exc:
            result.errors.append(f"{chat_id}: {exc}")
            continue
        _write_private(
            target,
            {
                "id": chat_id,
                "name": chat.get("name") or "",
                "created_at": chat.get("created_at") or "",
                "updated_at": chat.get("updated_at") or "",
                "href": chat.get("href") or "",
                "model": chat.get("model") or "",
                "messages": messages,
            },
        )
        result.fetched += 1
    if not result.errors:
        _write_private(directory / STATE_FILE, {"synced_at": started})
    return result


def _fetch_messages(client: ComplianceClient, chat_id: str) -> list[dict[str, str]]:
    path = f"/v1/compliance/apps/chats/{chat_id}/messages"
    messages: list[dict[str, str]] = []
    cursor = ""
    while True:
        params = [("limit", str(PAGE_SIZE))] + ([("after_id", cursor)] if cursor else [])
        page = client.get(path, params)
        for message in page.get("chat_messages") or []:
            text = message_text(message)
            if text:
                messages.append(
                    {
                        "role": str(message.get("role") or ""),
                        "created_at": str(message.get("created_at") or ""),
                        "text": text,
                    }
                )
        if not page.get("has_more") or not page.get("last_id"):
            return messages
        cursor = str(page["last_id"])


def _rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(0.0, epoch)))
