# Changelog

## Unreleased

- Add `session-control memory restore <id>`, which copies a note (or a merged original) back from the cold store into the live notes. The cold copy is kept, and a warning says when retention will move the restored note back at the next run.

- Retention now moves instead of deletes. Expired notes and rollups, and originals folded into a rollup, are appended to a compressed monthly bundle (`cold/cold-YYYY-MM.tar.gz`, stdlib `tarfile`, owner-only, verified by reading it back before the live file is removed). Bundles are deleted after `SESSION_CONTROL_MEMORY_COLD_RETENTION_DAYS` (default 365, counted from the end of the bundle's month; replaces `..._ARCHIVE_RETENTION_DAYS`). `memory search --cold` and `memory show --cold` read the cold store; it is not searched by default. Rollups record every id they cover (`merged_from`), so a backfill cannot recreate a merged session's note once its cold copy is gone. The loose `archive/` directory from the previous change is gone.

- Add a default retention policy for shared-memory notes. `session-control memory retention [--dry-run]` removes notes and rollups whose session was last active more than `SESSION_CONTROL_MEMORY_RETENTION_DAYS` ago (default 180), archived originals more than `SESSION_CONTROL_MEMORY_ARCHIVE_RETENTION_DAYS` after they were archived (default 30; the age restarts when a note is archived), and failure-log entries for notes that are gone; `0` disables a limit. The daily condense timer runs it after the merge (re-run `scripts/install_condense_timer.sh`). Rollups now remember the ids they cover, so backfills neither recreate notes for merged sessions after their archive expires nor write notes retention would delete again.

- Merge stale notes per workspace. `session-control memory merge [--older-than 60d] [--min-notes 5] [--dry-run]` folds the notes of sessions idle past the cutoff into one `rollup-<repo>-<hash>` note per workspace (a line per session with date, provider, title, gist and files; newest 100 kept), and moves the originals to `archive/` instead of deleting them. Later runs extend the same rollup, and `condense --all --refresh` skips archived sessions so they are not recreated. The daily condense timer template now runs the merge after the stale refresh (re-run `scripts/install_condense_timer.sh` to pick it up).

- Make the prune timer template match what runs on the maintainer's machine: each run now deduplicates sessions (`deduplicate --min-age 50d --max-age <older-than>`) before pruning, as a second `ExecStart` through Clockwork's `exec_start_then`, and the cron fallback chains the same two commands. Re-running `install_prune_timer.sh` no longer drops the deduplicate step.

- Add unified logging, following the portfolio Python logging convention (`traction-control/docs/python-logging.md`). Everything logs through one `session_control` logger tree to stderr, so the systemd journal has it (`journalctl --user -t session-control`; the web unit and both timers now set `SyslogIdentifier=session-control`). Verbosity comes from `--log-level` or `SESSION_CONTROL_LOG_LEVEL`. Every record is redacted before it is emitted, including exception text and request URLs. Optional rotating log file for platforms without a journal (`--log-file` / `SESSION_CONTROL_LOG_FILE`: a path, `default` or `off`; unset means on for macOS and Windows, off on Linux; 5 MB x 3 backups, owner-only, never fatal if unwritable). Logged: web start-up, session removals and their failures, refused removals, condensation fallbacks and background upgrade failures, backfill and prune summaries, and rejected requests (unexpected Host, cross-origin, CSRF).

- Log failed model condensations. When the local model errors, times out or returns nothing, the extractive note is kept as before, and the failure is now recorded in `failures.json` in the memory directory (error redacted, attempt count; cleared when a later run succeeds or the note is deleted). `session-control memory failures` lists them and `session-control condense --failed` redoes only those sessions, dropping entries whose session no longer exists.

- Finish the shared-memory follow-ups. Web delete, bulk delete and **Condense to memory** no longer block on a model: they write the extractive note first and upgrade it on a background worker (`SessionActionService(background_condense=True)`), keeping the fast note if the model fails. `condense --all --stale` rewrites notes whose session changed since the note was written, and `scripts/install_condense_timer.sh` installs a daily clockwork timer for it. `memory prune --older-than DURATION --max-per-repo N [--dry-run]` expires notes. The note page renders a sanitized markdown subset (all text escaped first; no links or raw HTML).

- Add a `session-memory` agent skill (`skills/session-memory/`) that tells assistants to search the shared memory before related work and to treat notes as unverified data. `session-control condense --model <name>` summarizes one run with a local Ollama model without enabling it globally; model replies lose `<think>` blocks and have their headings demoted. A tmux server that session-control has to create is now started in its own systemd scope, so redeploying the web service no longer closes the web terminal's windows.

- List claude.ai web chats as provider `claude-web`. `session-control sync-web` reads them through Anthropic's Compliance API (Claude Enterprise, a Compliance Access Key) into an owner-only local cache, fetching only chats updated since the last run, honouring rate limits and dropping chats deleted in claude.ai. Titles and previews are redacted like other providers. ChatGPT is not covered yet.
- Add shared agent memory. Before any removal (delete, bulk delete, prune, terminate-current) session-control now condenses the session into a redacted markdown note under `~/.local/share/session-control/memory/` (`entries/<provider>-<id>.md` plus a generated `INDEX.md`), so work survives trash expiry and can be read by every assistant on the machine. Query it with `session-control memory list|search|show|index`; write a note without removing anything with `session-control condense <id>`. The default condenser is deterministic and offline; set `SESSION_CONTROL_CONDENSER=ollama` with a loopback URL and model for an LLM summary (non-loopback hosts are refused, and any failure falls back to the extractive note). A failed condensation blocks removal; skip it with `prune --no-condense` or `SESSION_CONTROL_CONDENSE_ON_REMOVE=0`. `session-control condense --all` backfills notes for existing sessions (`--older-than`, `--provider`, `--refresh`). Short subagent sessions are not condensed. The web UI gets a **Memory** page (search, note view, delete note, `/api/memory`), a "memory note" badge on session rows, and a **Condense to memory** / **View memory note** control on the session page.

- Mask secrets in session titles and previews everywhere they appear. `SessionRecord` now redacts `title` and `preview` when it is built, so the session list, detail page, `scan --json`, search and deduplication never see a pasted token or OAuth callback URL, whichever provider scanner produced the record. Search runs over the masked text, so a secret cannot be probed for.

- Read Grok sessions as a handoff source. A handoff from Grok now carries the recent conversation instead of only the opening message: streamed `user_message_chunk` and `agent_message_chunk` events in `updates.jsonl` are merged into one turn per speaker, tool work separates paragraphs, and thoughts and tool output are left out. Continue, Copilot and AGY sources still hand off the opening message only.

- Redact secrets from handoff briefs. OAuth callback `code=`/`state=` values, bearer and basic auth values, `user:password@` URLs, private-key blocks, well-known token shapes (Google, GitHub, GitLab, OpenAI/Anthropic, Slack, AWS, npm, JWT) and the values of `*_SECRET`/`*_TOKEN`/`password`-style fields are replaced with `[REDACTED]` before the brief is written, ahead of truncation so a cut cannot leave half a secret. The brief notes how many values were masked. Briefs written before this change are not rewritten.
- Offer the handoff on every row of the session list, not only on the session page: a target picker and a **Hand off** button beside Open, Copy, Review and Delete. The row's actions are now a fixed-width grid (full-width Open, a joined picker and button, Copy and Review side by side, full-width Delete) instead of a ragged column, and stay two-column on phones.
- Match the portfolio type scale: the body takes `--font`, `--font-size` and `--mono` from the shared theme (so the top bar's font choice now applies here) and the page heading drops from a 25-35px display size to the home page's ~17px.
- Add a cross-provider **handoff**: continue any session in a different provider (Claude Code, Codex, Grok, Continue, Copilot or Antigravity). It writes a private context brief (recent conversation for Codex and Claude Code sources, the opening message otherwise) and starts a new session in the target CLI that reads it. Use the "Continue in" control on the session page or `session-control handoff <id> --to <provider>`.
- Add multi-account launchers for the remaining session providers.
  `codex-profile`, `claude-profile`, `continue-profile`, and `copilot-profile`
  isolate each login in `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
  `CONTINUE_GLOBAL_DIR`, and `COPILOT_HOME`. The default account stays in
  `~/.codex`, `~/.claude`, `~/.continue`, or `~/.copilot`. Added accounts live
  under `<home>/profiles/<name>`. Directory routes, token totals, and control
  plane resume use the same shape as `grok-profile`.

- Add Grok multi-account support alongside AGY. `grok-profile` (and
  `session-control grok-profile`) isolates each login in its own `GROK_HOME`,
  routes directories to an account, and totals tokens from each session's
  `usage.json`. The default account stays at `~/.grok`. Added accounts live
  under `~/.grok/profiles/<name>` until you sign in there.

- Add multi-user profile management, directory-aware routing, and token usage
  tracing for Google Antigravity CLI (AGY). Provides the `session-control profile`
  subcommand and `agy-profile` launcher executable, isolates user credentials in
  `~/.gemini/profiles/<user>`, harvests turn tokens into a local SQLite telemetry
  store (`~/.gemini/usage_tracking.sqlite`), and scans profile sessions in the
  control plane.

- Add Grok Build and Google Antigravity CLI (AGY) session scanning, resume,
  active-session protection, delete-to-trash, and restore support. AGY's shared
  SQLite summary/search rows and Grok's optional search row are maintained with
  their session payloads.

- Refuse every request whose `Host` header is not a loopback name, this site's registered hostname (from the portfolio site store), or the host of `SESSION_CONTROL_PUBLIC_ORIGIN` / `SESSION_CONTROL_ALLOWED_ORIGINS`, before the origin and CSRF checks. A DNS-rebinding page sent its own hostname as both `Host` and `Origin`, passed the same-origin check, and could fetch the CSRF token to delete or open sessions. The shared check lives in the vendored portfolio site seam (re-vendored here). **Upgrade note:** a deployment reached through a proxy under a name that is neither registered nor listed in `SESSION_CONTROL_ALLOWED_ORIGINS` will get 403.

- Re-vendor the portfolio theme fallback: the top bar's theme menu offers This site / Everywhere and "Follow the global theme".
- Answer `/_portfolio/*` itself from a vendored portfolio site seam (`session_control/portfolio/`), so the theme and top bar work with no Caddy in front: the owner-only site store first, then a pinned fallback copy. Add `/.portfolio` (ignored) and `config/portfolio-site.example.toml`. See traction-control `docs/portfolio-sites.md`.

- Follow the portfolio system themes: pages take their colours from the shared tokens (`/_portfolio/theme.css`), so the theme chosen in the top bar restyles them.

- Load the portfolio top bar (`<script src="/_portfolio/chrome.js" defer></script>`, served by the shared Caddy) on every page, so the global navigation and theme rotator appear here too.
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
