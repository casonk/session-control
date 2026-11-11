from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from session_control import web_chats
from session_control.scanner import PROVIDERS, SessionScanner


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def chat(chat_id, *, name="Plan", updated="2026-11-01T10:00:00Z", email="me@example.com", **extra):
    return {
        "id": chat_id,
        "name": name,
        "created_at": "2026-11-01T09:00:00Z",
        "updated_at": updated,
        "deleted_at": None,
        "href": f"https://claude.ai/chat/{chat_id}",
        "model": "claude-x",
        "user": {"id": "u1", "email_address": email},
        **extra,
    }


def messages_for(chat_id):
    return {
        "id": chat_id,
        "has_more": False,
        "chat_messages": [
            {
                "id": "m1",
                "role": "user",
                "created_at": "2026-11-01T09:00:01Z",
                "content": [
                    {
                        "type": "text",
                        "text": "How do I rotate my token sk-ant-api01-abcdefghijklmnop?",
                    }
                ],
            },
            {
                "id": "m2",
                "role": "assistant",
                "created_at": "2026-11-01T09:00:05Z",
                "content": [
                    {"type": "tool_use", "name": "search", "input": "{}"},
                    {"type": "text", "text": "Create a new one."},
                ],
            },
        ],
    }


class Server:
    """Answers the Compliance API paths the sync uses."""

    def __init__(self, chats, pages=None):
        self.chats = chats
        self.requests: list[tuple[str, dict]] = []
        self.pages = pages

    def __call__(self, request, timeout=None):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(request.full_url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.requests.append((parsed.path, params))
        assert request.headers["X-api-key"] == "key"
        assert request.headers["Anthropic-version"] == web_chats.API_VERSION
        if parsed.path.endswith("/messages"):
            return FakeResponse(json.dumps(messages_for(parsed.path.split("/")[-2])).encode())
        items = self.chats
        after = params.get("after_id")
        if self.pages:
            index = int(after) if after else 0
            body = {
                "data": self.pages[index],
                "has_more": index + 1 < len(self.pages),
                "last_id": str(index + 1),
            }
        else:
            body = {"data": items, "has_more": False, "last_id": None}
        return FakeResponse(json.dumps(body).encode())


def client(server):
    return web_chats.ComplianceClient("key", opener=server, sleep=lambda s: None)


def test_sync_writes_chats_that_the_scanner_lists(tmp_path, app_config):
    server = Server([chat("claude_chat_1")])
    result = web_chats.sync(client(server), tmp_path)
    assert result.fetched == 1
    cached = tmp_path / "claude-web" / "claude_chat_1.json"
    assert cached.stat().st_mode & 0o777 == 0o600
    stored = json.loads(cached.read_text())
    assert [m["role"] for m in stored["messages"]] == ["user", "assistant"]
    assert "search" not in cached.read_text()  # tool calls are not kept

    config = replace(app_config, web_chat_dir=tmp_path)
    report = SessionScanner(config).scan(providers=("claude-web",))
    (record,) = report.sessions
    assert record.provider == "claude-web" and record.title == "Plan"
    assert record.message_count == 2
    assert "sk-ant-api01-abcdefghijklmnop" not in record.preview  # redaction still applies
    assert record.resume_command == "xdg-open https://claude.ai/chat/claude_chat_1"


def test_a_second_sync_reads_only_what_changed(tmp_path):
    server = Server([chat("claude_chat_1")])
    web_chats.sync(client(server), tmp_path, now=lambda: 1_000_000.0)
    server.requests.clear()
    result = web_chats.sync(client(server), tmp_path)
    assert result.fetched == 0 and result.skipped == 1
    list_call = server.requests[0][1]
    assert list_call["updated_at.gte"].startswith("1970-01-1")  # 1,000,000 s minus the overlap
    assert not any(path.endswith("/messages") for path, _ in server.requests)


def test_full_ignores_the_saved_cursor(tmp_path):
    server = Server([chat("claude_chat_1")])
    web_chats.sync(client(server), tmp_path)
    server.requests.clear()
    web_chats.sync(client(server), tmp_path, full=True)
    assert "updated_at.gte" not in server.requests[0][1]


def test_paging_follows_last_id(tmp_path):
    server = Server([], pages=[[chat("claude_chat_1")], [chat("claude_chat_2")]])
    result = web_chats.sync(client(server), tmp_path)
    assert result.fetched == 2


def test_other_users_chats_are_skipped_when_an_email_is_set(tmp_path):
    server = Server([chat("claude_chat_1"), chat("claude_chat_2", email="other@example.com")])
    result = web_chats.sync(client(server), tmp_path, user_email="ME@example.com")
    assert result.fetched == 1 and result.skipped == 1


def test_a_deleted_chat_is_removed_from_the_cache(tmp_path):
    web_chats.sync(client(Server([chat("claude_chat_1")])), tmp_path)
    gone = chat("claude_chat_1", name="", deleted_at="2026-11-02T00:00:00Z")
    result = web_chats.sync(client(Server([gone])), tmp_path)
    assert result.removed == 1
    assert not (tmp_path / "claude-web" / "claude_chat_1.json").exists()


def test_unsafe_chat_ids_never_become_paths(tmp_path):
    result = web_chats.sync(client(Server([chat("../../escape")])), tmp_path)
    assert result.fetched == 0
    assert not (tmp_path.parent / "escape.json").exists()


def test_a_non_claude_link_is_not_offered(tmp_path, app_config):
    directory = tmp_path / "claude-web"
    directory.mkdir()
    (directory / "c.json").write_text(
        json.dumps({"id": "c", "name": "x", "href": "javascript:alert(1)", "messages": []})
    )
    config = replace(app_config, web_chat_dir=tmp_path)
    (record,) = SessionScanner(config).scan(providers=("claude-web",)).sessions
    assert record.resume_command == ""


def http_error(code, retry_after=None):
    headers = {"retry-after": retry_after} if retry_after else {}
    body = io.BytesIO(json.dumps({"error": {"message": "nope"}}).encode())
    return urllib.error.HTTPError("u", code, "x", headers, body)


def test_rate_limits_wait_and_retry():
    waits, calls = [], []

    def opener(request, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise http_error(429, "7")
        return FakeResponse(b'{"data": []}')

    c = web_chats.ComplianceClient("key", opener=opener, sleep=waits.append)
    assert c.get("/v1/compliance/apps/chats") == {"data": []}
    assert waits == [7.0]


def test_an_admin_key_gets_a_helpful_error():
    def opener(request, timeout=None):
        raise http_error(403)

    c = web_chats.ComplianceClient("key", opener=opener, sleep=lambda s: None)
    with pytest.raises(web_chats.ComplianceError, match="Compliance Access Key"):
        c.get("/v1/compliance/apps/chats")


def test_the_key_comes_from_env_or_a_file(tmp_path):
    assert web_chats.access_key({}) == ""
    assert web_chats.access_key({"SESSION_CONTROL_ANTHROPIC_COMPLIANCE_KEY": " k "}) == "k"
    key_file = tmp_path / "key"
    key_file.write_text("filekey\n")
    assert (
        web_chats.access_key({"SESSION_CONTROL_ANTHROPIC_COMPLIANCE_KEY_FILE": str(key_file)})
        == "filekey"
    )


def test_claude_web_is_a_known_provider(app_config):
    assert "claude-web" in PROVIDERS
    config = replace(app_config, web_chat_dir=Path("/x"))
    assert config.provider_root("claude-web") == Path("/x/claude-web")
