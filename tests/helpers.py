from __future__ import annotations

import json
from pathlib import Path


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def seed_codex(
    root: Path,
    session_id: str = "019d016b-30c2-7992-970a-b6082c1a2723",
    model: str = "",
    token_count: bool = False,
) -> Path:
    write_jsonl(
        root / "session_index.jsonl",
        [
            {
                "id": session_id,
                "thread_name": "Fix statement downloader",
                "updated_at": "2026-04-01T12:30:00Z",
            }
        ],
    )
    session_path = root / "sessions" / "2026" / "04" / "01" / f"rollout-{session_id}.jsonl"
    rows = [
        {
            "timestamp": "2026-04-01T12:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-04-01T12:00:00Z",
                "cwd": "/workspace/personal-finance",
                "originator": "codex_cli",
            },
        },
    ]
    if model:
        rows.append(
            {
                "timestamp": "2026-04-01T12:01:00Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "019d016b-30c2-7992-970a-b6082c1a2724",
                    "cwd": "/workspace/personal-finance",
                    "model": model,
                },
            }
        )
    if token_count:
        rows.append(
            {
                "timestamp": "2026-04-01T12:02:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"total_tokens": 125000},
                        "last_token_usage": {"total_tokens": 24000},
                        "model_context_window": 258400,
                    },
                    "rate_limits": {
                        "primary": {
                            "used_percent": 33.0,
                            "window_minutes": 300,
                            "resets_at": 1775048400,
                        },
                        "secondary": {
                            "used_percent": 17.0,
                            "window_minutes": 10080,
                            "resets_at": 1775653200,
                        },
                        "plan_type": "plus",
                    },
                },
            }
        )
    rows.append(
        {
            "timestamp": "2026-04-01T12:05:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Fix the downloader tests"}],
            },
        },
    )
    write_jsonl(session_path, rows)
    return session_path


def seed_claude(
    root: Path,
    session_id: str = "c7df09f0-b9f2-4563-853e-f64fd095128f",
    token_count: bool = False,
) -> Path:
    session_path = root / "projects" / "-workspace-crew-chief" / f"{session_id}.jsonl"
    rows = [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": session_id},
        {
            "type": "user",
            "timestamp": "2026-04-02T10:00:00Z",
            "sessionId": session_id,
            "cwd": "/workspace/crew-chief",
            "message": {"role": "user", "content": "Create the local LLM service"},
        },
    ]
    if token_count:
        rows.append(
            {
                "type": "assistant",
                "timestamp": "2026-04-02T10:03:00Z",
                "sessionId": session_id,
                "cwd": "/workspace/crew-chief",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "Done"}],
                    "usage": {
                        "input_tokens": 12,
                        "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 200,
                        "output_tokens": 34,
                        "service_tier": "standard",
                        "speed": "standard",
                    },
                },
            }
        )
    write_jsonl(session_path, rows)
    return session_path


def seed_continue(root: Path, session_id: str = "9f4b464d-495f-432d-8d16-31aa4e7ac7ea") -> Path:
    sessions_root = root / "sessions"
    write_json(
        sessions_root / "sessions.json",
        [
            {
                "sessionId": session_id,
                "title": "Continue repo summary",
                "dateCreated": "1774494757505",
                "workspaceDirectory": "/workspace/shock-relay",
                "messageCount": 2,
            }
        ],
    )
    session_path = sessions_root / f"{session_id}.json"
    write_json(
        session_path,
        {
            "sessionId": session_id,
            "title": "Continue repo summary",
            "workspaceDirectory": "/workspace/shock-relay",
            "dateCreated": "1774494757505",
            "history": [
                {
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Summarize this repository"}],
                    }
                },
                {"message": {"role": "assistant", "content": "Summary"}},
            ],
        },
    )
    return session_path


def seed_copilot(root: Path, session_id: str = "3e6d83f8-5629-47d2-9a64-2779c509c808") -> Path:
    session_dir = root / "session-state" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "workspace.yaml").write_text(
        "\n".join(
            [
                f"id: {session_id}",
                "cwd: /workspace",
                "summary_count: 0",
                "created_at: 2026-03-21T14:46:48.667Z",
                "updated_at: 2026-03-21T14:48:00.000Z",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "type": "session.start",
                "data": {
                    "sessionId": session_id,
                    "startTime": "2026-03-21T14:46:48.667Z",
                    "context": {"cwd": "/workspace"},
                },
                "timestamp": "2026-03-21T14:46:48.671Z",
            },
            {
                "type": "user.message",
                "data": {"content": "Compress the tool with tar gz"},
                "timestamp": "2026-03-21T14:47:03.904Z",
            },
        ],
    )
    checkpoints = session_dir / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / "index.md").write_text(
        "| # | Title | File |\n|---|-------|------|\n| 1 | Compression checkpoint | 001.md |\n",
        encoding="utf-8",
    )
    return session_dir
