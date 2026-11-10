# Changelog

## Unreleased

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
