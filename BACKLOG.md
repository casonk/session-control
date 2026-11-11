# BACKLOG.md

Portfolio backlog for this repository. Pending items are candidates for
execution manually or via crew-chief. Entries sourced from archility audit are
tagged `[archility:YYYY-MM-DD]`; manual entries use `[manual:YYYY-MM-DD]`.

The archility twice-weekly job populates this file automatically via
`archility audit --write-backlog`. To execute a backlog item with crew-chief:
`crew-chief agent "Work on item: <item text>"`. Mark items `[x]` when complete
and move them to Done.

## Pending

- [ ] [manual:2026-10-11] After merging the multi-machine inventory PR,
  deploy the opt-in federation receiver on the server and publisher on each
  approved client. Update both checkouts to `main`; configure the server's
  private federation store and a distinct owner-only token per node; expose
  only the ingestion/read routes through the existing Caddy mTLS boundary;
  configure the Mac publisher with its client certificate and a scheduled
  `session-control publish-inventory`; then verify **All machines** shows both
  hosts. Keep inventories metadata-only and remote sessions read-only—never
  sync provider directories, transcripts, private paths, or action commands.

- [ ] [manual:2026-11-11] **Get the Anthropic Compliance Access Key and run the first live
  `session-control sync-web`.** The importer (`web_chats.py`) was built from Anthropic's docs and
  tested only against a fake server. A primary owner of the Claude Enterprise organization enables
  the Compliance API at claude.ai, Organization settings, API, then creates a key with the
  `read:compliance_user_data` scope (an Admin API key from the Console is refused with 403). Store
  it in an owner-only file named by `SESSION_CONTROL_ANTHROPIC_COMPLIANCE_KEY_FILE`, set
  `SESSION_CONTROL_ANTHROPIC_USER_EMAIL`, and run `sync-web`. Confirm the `anthropic-version`
  value: the guide uses `2026-10-21`, the API reference examples show `2023-06-01`.
- [ ] [manual:2026-11-11] **Get an OpenAI Enterprise Compliance API key, then add ChatGPT.** Needs
  a ChatGPT Enterprise or Edu workspace; access is requested from OpenAI. OpenAI's docs
  (learn.chatgpt.com/docs/enterprise/compliance-api) and download script give the base
  `https://api.chatgpt.com/v1/compliance`, `Authorization: Bearer <key>`, and
  `GET /{workspaces|organizations}/{id}/logs?event_type=conversation_message&after=<ISO time>&limit=`
  (an `org-` id selects `organizations`), paged by `last_end_time` while `has_more`, with each file
  fetched from `.../logs/{id}` as JSONL. The message record schema (conversation id, title, role,
  text) is in the Admin API reference at chatgpt.com/public/admin/api-reference and was not
  readable, and older `/conversations` routes are unconfirmed. Fetch one real log file first, then
  write the parser against it; do not guess the fields.
- [ ] [manual:2026-11-11] **Import personal-account chats from the data exports.** Personal ChatGPT,
  claude.ai and Gemini accounts have no history API. ChatGPT: Settings, Data controls, Export data
  (emailed zip with `conversations.json`). claude.ai: Settings, Privacy, Export data. Gemini: Google
  Takeout. Add `session-control import <provider> <zip>` that unpacks into the same owner-only
  web-chat cache, keyed by conversation id, and write each parser against a real export. Grok and
  Copilot web chats have no known export or API yet.

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
- [x] [manual:2026-08-27] Add provider format probes that report unsupported
  session-index, session-file, workspace, and event-log schema drift in the
  web UI without hiding sessions from unaffected providers. [manual:2026-05-03]
