# Changelog

## Unreleased

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
