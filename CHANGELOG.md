# Changelog

## Unreleased

- Ignore hidden directories in Copilot's session state, so its operation-lock
  directory no longer fails the scan and the daily prune job.
- Add a **Machine load** page (`/ecu`, JSON at `/api/ecu`) showing the held lease
  (command, priority, mode, GPU size), GPU memory and processes, and each timer
  job's run/defer verdict from `ecu status --json`. It explains itself when the
  `ecu` command is missing (`SESSION_CONTROL_ECU_BIN` overrides the lookup).
- Claude Code sessions launched with `crew-chief code` are read from the
  crew-chief session registry, labelled `local · <model>`, and resumed through
  `crew-chief code` so they reopen on the same local backend.

## 0.2.0 - 2026-10-16

- Added a `session-control prune` command for delete-to-trash cleanup of
  inactive sessions older than a configured age.
- Added a clockwork manifest template and installer for daily 180-day pruning.

## 0.1.0 - 2026-05-03

- Initial local scanner and Flask web UI for Codex, Claude Code, Continue, and
  GitHub Copilot CLI sessions.
- Added delete-to-trash behavior with CSRF protection and loopback-safe defaults.
