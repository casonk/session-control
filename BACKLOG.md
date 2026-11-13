# BACKLOG.md

Portfolio backlog for this repository. Pending items are candidates for
execution manually or via crew-chief. Entries sourced from archility audit are
tagged `[archility:YYYY-MM-DD]`; manual entries use `[manual:YYYY-MM-DD]`.

The archility twice-weekly job populates this file automatically via
`archility audit --write-backlog`. To execute a backlog item with crew-chief:
`crew-chief agent "Work on item: <item text>"`. Mark items `[x]` when complete
and move them to Done.

## Pending

- [ ] [manual:2026-11-13] Retention is manual (`memory prune`); consider a
  default policy and a clockwork job once the note count is known to grow.
- [ ] [manual:2026-11-13] Merge or drop stale notes for the same workspace
  (the per-repo cap only keeps the newest N; it does not combine them).

## In Progress

## Done

- [x] [manual:2026-11-13] Enable the Ollama condenser (`qwen3:14b`) and make
  the web UI non-blocking: removal writes the extractive note, then a worker
  upgrades it. Existing notes upgraded with `condense --all --refresh`.
- [x] [manual:2026-11-13] Daily clockwork timer for `condense --all --stale`
  (`scripts/install_condense_timer.sh`).
- [x] [manual:2026-11-13] Memory retention: `memory prune --older-than
  --max-per-repo [--dry-run]`.
- [x] [manual:2026-11-13] Sanitized markdown rendering in the Memory view.
- [x] [manual:2026-11-13] Promote the service-restart cgroup lesson to
  `traction-control/LESSONSLEARNED.md` (traction-control #141).

- [x] [manual:2026-08-23] Name `CHATHISTORY.md` in `AGENTS.md` as the
  local-only, gitignored session memory, alongside `LESSONSLEARNED.md`; it
  must not contain parsed session contents, credentials, copied logs, or
  private paths. Template: `traction-control/docs/templates/AGENTS.md`.
- [x] [manual:2026-08-27] Add `session-control restore --list` and
  `session-control restore <timestamp/provider/session-id>` for sessions
  newly moved to local trash. Restore metadata preserves provider-relative
  paths; restore refuses traversal and overwrites, and rebuilds the Continue
  index when needed. Sessions trashed before this change remain manual
  recovery cases. [manual:2026-05-03]
- [x] [manual:2026-08-27] Add provider format probes that report unsupported
  session-index, session-file, workspace, and event-log schema drift in the
  web UI without hiding sessions from unaffected providers. [manual:2026-05-03]
