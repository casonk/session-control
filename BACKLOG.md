# BACKLOG.md

Portfolio backlog for this repository. Pending items are candidates for
execution manually or via crew-chief. Entries sourced from archility audit are
tagged `[archility:YYYY-MM-DD]`; manual entries use `[manual:YYYY-MM-DD]`.

The archility twice-weekly job populates this file automatically via
`archility audit --write-backlog`. To execute a backlog item with crew-chief:
`crew-chief agent "Work on item: <item text>"`. Mark items `[x]` when complete
and move them to Done.

## Pending

- [ ] [manual:2026-08-23] Name `CHATHISTORY.md` in `AGENTS.md` as the
  local-only, gitignored session memory, alongside the existing
  `LESSONSLEARNED.md` reference. This is the one shared convention the repo is
  missing — `scripts/check_agents_md.py` in traction-control flags it. It
  matters more here than elsewhere: this repo parses AI assistant session
  files, which can carry credentials, copied logs, and private paths, so the
  local-only boundary should be stated where an agent reads it first.
  Template: `traction-control/docs/templates/AGENTS.md`.

- [ ] [manual:2026-05-03] Add a restore command for sessions moved into the local trash directory.
- [ ] [manual:2026-05-03] Add provider format probes that report unsupported schema drift in the web UI.

## In Progress

## Done
