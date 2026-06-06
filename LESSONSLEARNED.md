# LESSONSLEARNED.md

Tracked durable lessons for `session-control`.
Unlike `CHATHISTORY.md`, this file should keep only reusable lessons that should
change how future sessions work in this repo.

## How To Use

- Read this file after `AGENTS.md` and before `CHATHISTORY.md` when resuming work.
- Add lessons that generalize beyond a single session.
- Keep entries concise and action-oriented.
- Do not use this file for transient status updates or full session logs.

## Lessons

- Document the repository around its real execution, curation, or integration flow instead of only the top-level folder list.
- Keep local-only, private, reference-only, or generated boundaries explicit so published or runtime behavior is not confused with offline material or non-committable inputs.
- Keep tracked examples, fixtures, and `.example` templates scrubbed of real paths, usernames, hostnames, account identifiers, or other instance-specific values; real operator data belongs only in gitignored local config.
- If the repo exposes a dashboard or admin surface, keep loopback-safe defaults in the app itself and treat wider network exposure as an explicit trust-boundary decision rather than a documentation assumption.
- Re-run repo-appropriate validation after changing generated artifacts, diagrams, workflows, or other CI-facing files so formatting and compatibility issues are caught before push.
- AI assistant session files may contain secrets, copied logs, personal data, and private paths; tests must use minimized synthetic fixtures rather than copied real transcripts.
- Codex web resume commands should preserve the session's recorded model or use an explicit local fallback, because inheriting the current global Codex default can break old sessions when auth-mode/model availability changes.
- Webterm-backed Open actions should create, select, and keep visible the target tmux window; if a launched command exits quickly, leave the error visible instead of letting tmux close the window and dropping the user back on the base shell.
- Claude Code local JSONL can expose assistant `message.usage` token counts, model, cache, and service-tier metadata, but not plan limit windows, reset times, or usage percentages; label Claude limits as unavailable rather than estimating them from tokens.
- Claude CLI `auth status --json` is safe for subscription/auth polling but includes account-identifying fields and still does not expose plan limit windows on current releases; sanitize output before caching or rendering and keep the poller opt-in.
