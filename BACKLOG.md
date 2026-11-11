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

- [ ] [manual:2026-11-11] **Request your own web-chat exports (a person has to do this).** Put each
  file in an owner-only folder outside any repository, for example `~/private/chat-exports/`, and
  do not commit or paste them anywhere. Some take a day, so request them all first:
  1. ChatGPT: chatgpt.com, Settings, Data controls, Export data. An emailed link gives a zip with
     `conversations.json`.
  2. claude.ai: Settings, Privacy, Export data. An emailed link gives the download.
  3. Gemini: takeout.google.com, deselect all, choose My Activity, open "All activity data
     included", pick only Gemini Apps, and choose the JSON format. First check that Gemini Apps
     activity is turned on in myactivity.google.com; chats made while it was off, and deleted
     chats, are not in the export. Workspace-managed accounts often get an empty file; if so, note
     it here.
  4. Grok: accounts.x.ai/data, Download account data (a zip or JSON by emailed link, reportedly
     valid 24 hours). If that page is missing, try grok.com Settings, Data, or the X account
     archive.
  Then write each file's path and date next to its importer item below, so the parser can be built
  against the real format.
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
  web-chat cache, keyed by conversation id, and write each parser against a real export. Gemini's
  Takeout is a per-prompt activity log (JSON or `MyActivity.html`), so grouping prompts into chats
  needs care. Grok has no history API; use the account data download. Copilot web chats have no
  known export or API yet. No provider listed here has a personal-account history API.

- [ ] [manual:2026-11-11] **Bring your own session-control checkout up to date.** Its `main` shows
  "ahead 3" because the remote is SSH and cannot be fetched from the agent environment. Run
  `git fetch origin` from your own shell, check `git log origin/main..main` is only commits that
  are already merged upstream under new hashes, then reset or fast-forward `main` to `origin/main`.
  Do this before the first `session-control sync-web`, so the command exists in your install
  (`pip install -e .` again if the entry point predates it).

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
