# BACKLOG.md

Portfolio backlog for this repository. Pending items are candidates for
execution manually or via crew-chief. Entries sourced from archility audit are
tagged `[archility:YYYY-MM-DD]`; manual entries use `[manual:YYYY-MM-DD]`.

The archility twice-weekly job populates this file automatically via
`archility audit --write-backlog`. To execute a backlog item with crew-chief:
`crew-chief agent "Work on item: <item text>"`. Mark items `[x]` when complete
and move them to Done.

## Pending

- [ ] [manual:2026-05-03] Add provider format probes that report unsupported schema drift in the web UI.

## In Progress

## Done

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
