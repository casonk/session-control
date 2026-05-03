from __future__ import annotations

import json
from pathlib import Path


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def seed_codex(root: Path, session_id: str = "019d016b-30c2-7992-970a-b6082c1a2723") -> Path:
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
    write_jsonl(
        session_path,
        [
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
            {
                "timestamp": "2026-04-01T12:05:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the downloader tests"}],
                },
            },
        ],
    )
    return session_path


def seed_claude(root: Path, session_id: str = "c7df09f0-b9f2-4563-853e-f64fd095128f") -> Path:
    session_path = root / "projects" / "-workspace-crew-chief" / f"{session_id}.jsonl"
    write_jsonl(
        session_path,
        [
            {"type": "permission-mode", "permissionMode": "default", "sessionId": session_id},
            {
                "type": "user",
                "timestamp": "2026-04-02T10:00:00Z",
                "sessionId": session_id,
                "cwd": "/workspace/crew-chief",
                "message": {"role": "user", "content": "Create the local LLM service"},
            },
        ],
    )
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
