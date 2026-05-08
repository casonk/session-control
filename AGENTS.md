# AGENTS.md - session-control

## Purpose

`session-control` provides a private web UI and CLI for reviewing, resuming,
and deleting local AI assistant sessions across Codex, Claude Code, Continue,
and GitHub Copilot CLI.

The app is intended to bind to localhost and be exposed to trusted devices
through `wiring-harness` Caddy/mTLS or a WireGuard-only route.

## Repository Layout

```text
session-control/
├── pyproject.toml
├── config/session-control.env.example
├── src/session_control/
│   ├── cli.py
│   ├── config.py
│   ├── scanner.py
│   ├── actions.py
│   ├── web.py
│   ├── templates/
│   └── static/
├── tests/
├── scripts/install_web_service.sh
├── scripts/install_prune_timer.sh
└── docs/
```

## Operating Rules

1. Keep local session contents, previews, paths, and trash files out of git.
2. Keep the Flask app bound to `127.0.0.1` unless
   `SESSION_CONTROL_ALLOW_REMOTE=1` is explicitly set for a trusted deployment.
3. Preserve CSRF checks on every state-changing route.
4. Delete by moving provider files into the configured trash directory, not by
   hard-deleting them in the first implementation.
5. Refuse to delete sessions with provider lock files that indicate live use.
6. When provider storage formats change, add or update fixtures in `tests/`
   before changing scanner behavior.

## Setup and Commands

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Run the web UI locally:

```bash
session-control web
```

Run a scan without the web UI:

```bash
session-control scan --json
```

Preview or run retention cleanup:

```bash
session-control prune --older-than 180d --dry-run
session-control prune --older-than 180d
```

Install the user service:

```bash
./scripts/install_web_service.sh
```

Install the daily clockwork prune timer:

```bash
./scripts/install_prune_timer.sh --older-than 180d
```

## Sudo Boundary

Agents will never be able to run `sudo` commands in this environment. If a task requires elevated system changes, make the repo edits and run the validation that can be done without `sudo`, then give the user the exact command(s) to run.

Always require the user to run those commands instead of retrying `sudo`; do not claim a sudo-backed live change was applied until the user shares the result.

## Local CI Verification

Run before every push:

```bash
pre-commit run --all-files
pytest -q
```

Do not push changes that have not passed all checks locally.

## Portfolio Standards Reference

For portfolio-wide repository standards and baseline conventions, consult the
control-plane repo at `./util-repos/traction-control` from the portfolio root.

Start with:

- `./util-repos/traction-control/AGENTS.md`
- `./util-repos/traction-control/README.md`
- `./util-repos/traction-control/LESSONSLEARNED.md`

Shared implementation repos available portfolio-wide:

- `./util-repos/archility` for architecture bootstrap, rendering, and drift checks
- `./util-repos/auto-pass` for KeePassXC-backed secret retrieval
- `./util-repos/clockwork` for cron and systemd scheduler manifests
- `./util-repos/tachometer` for repo and resource profiling
- `./util-repos/nordility` for NordVPN switching
- `./util-repos/shock-relay` for external messaging
- `./util-repos/short-circuit` for WireGuard setup
- `./util-repos/snowbridge` for SMB-backed file sharing
- `./util-repos/dyno-lab` for shared test utilities
- `./util-repos/crew-chief` for local LLM inference
- `./util-repos/windshield` for reusable browser automation
- `./util-repos/wiring-harness` for Caddy, mTLS, and DNS infrastructure
