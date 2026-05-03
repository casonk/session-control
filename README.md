# session-control

Private web control plane for local AI assistant session files.

`session-control` scans local Codex, Claude Code, Continue, and GitHub Copilot
session stores, summarizes what each session was about, shows the command to
resume it, and moves old sessions into a local trash directory from a small
Flask web UI.

## Prerequisites

- Python 3.10 or newer
- Flask, installed through the package metadata
- Local session files from one or more supported tools:
  - Codex: `~/.codex/sessions`
  - Claude Code: `~/.claude/projects`
  - Continue: `~/.continue/sessions`
  - GitHub Copilot CLI: `~/.copilot/session-state`

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
session-control scan
session-control web
```

The web UI binds to `127.0.0.1:5420` by default. For phone access, keep the app
on loopback and expose it through a trusted local proxy such as
`wiring-harness` Caddy with mTLS or a WireGuard-only route.

## Configuration

Copy `config/session-control.env.example` to
`config/session-control.env.local` and adjust local paths or proxy origins.
The `.local` file is gitignored.

Common variables:

| Variable | Default | Purpose |
|---|---|---|
| `SESSION_CONTROL_HOST` | `127.0.0.1` | Flask bind host |
| `SESSION_CONTROL_PORT` | `5420` | Flask bind port |
| `SESSION_CONTROL_PUBLIC_ORIGIN` | unset | External origin used by a trusted proxy |
| `SESSION_CONTROL_ALLOWED_ORIGINS` | unset | Comma-separated extra origins for CSRF checks |
| `SESSION_CONTROL_TRASH_DIR` | `~/.local/share/session-control/trash` | Delete destination |

Set `SESSION_CONTROL_ALLOW_REMOTE=1` only when intentionally binding the Flask
app outside loopback. The preferred phone path is still a loopback app behind
`wiring-harness`/Caddy or WireGuard.

## Resume Commands

The scanner emits provider-specific resume commands:

- Codex: `codex resume <session-id>`
- Claude Code: `claude --resume <session-id>`
- Continue: `cn --fork <session-id>`
- GitHub Copilot CLI: `copilot --resume=<session-id>`

When a session records a workspace, the command is prefixed with `cd <workspace>`.

## Delete Behavior

Delete actions move session files or directories into the configured trash
directory instead of hard-deleting them immediately. Continue and Codex index
files are updated when a session is removed. Copilot sessions with a live
`inuse.<pid>.lock` are not deleted.

## Development

```bash
pytest -q
ruff check .
ruff format --check .
pre-commit run --all-files
```

## Contributing

See `CONTRIBUTING.md`.

## License

MIT. See `LICENSE`.
