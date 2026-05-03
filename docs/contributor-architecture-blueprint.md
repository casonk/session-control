# Contributor Architecture Blueprint

## System Context

`session-control` is a local-only web and CLI utility. It reads session stores
owned by AI assistant tools and presents a normalized index for review, resume,
and delete-to-trash workflows.

## Main Flow

1. `session-control scan` or the Flask index route loads `AppConfig`.
2. `SessionScanner` scans configured provider roots.
3. Provider-specific parsers normalize sessions into `SessionRecord` objects.
4. The web UI renders sortable, searchable session cards.
5. Delete requests pass CSRF and same-origin checks.
6. `SessionActionService` moves provider files into the configured trash
   directory and updates provider indexes where applicable.

## Trust Boundary

The Flask app performs state-changing local filesystem operations. It must bind
to loopback by default. Phone access should go through `wiring-harness` Caddy
with mTLS or a WireGuard-only path.

## Provider Boundaries

- Codex: JSONL files under `sessions/` and `archived_sessions/`; optional
  `session_index.jsonl` cleanup on delete.
- Claude Code: JSONL files under `projects/`.
- Continue: JSON files under `sessions/`; `sessions.json` cleanup on delete.
- GitHub Copilot CLI: directories under `session-state/`; live `inuse` locks
  block deletion.
