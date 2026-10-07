# Project Desk in claude.ai/code cloud sessions

This repo is wired to the hosted Project Desk at {url} as project `{project}`. Cloud sessions
do not install plugins, so the repo itself carries the client. There are two ways to join:
paste a fresh invite into each cloud session, or set an environment token as the advanced route.

## Join with an invite

Allow `{host}` in the cloud Custom network setting and set `PROJECT_DESK_URL={url}` in the cloud
environment. Leave `PROJECT_DESK_TOKEN` unset. Ask the owner for a Project Desk invite and paste the
whole invite into the chat. The repo's `UserPromptSubmit` hook runs the byte-identical
`.project-desk/desk.py` client, joins, and prints a result card. Its `SessionStart` hook says who
the saved session is, or asks for an invite. `SessionEnd` makes a best effort to leave and release
the token; a token not released there lapses at its 1-day cloud expiry.
These three hooks run only with `CLAUDE_CODE_REMOTE=true` and no `PROJECT_DESK_TOKEN`.

Each invite lasts at most one cloud session day. From two hours before expiry, the prompt hook
warns at most hourly. Paste a fresh invite before the old token expires to transfer verified
task ownership. If some tasks cannot move, the old session stays active; run bare
`python3 .project-desk/desk.py join` to retry only outstanding tasks. If access has expired,
ask the lead to reassign any earlier tasks. A new code never silently claims their ownership.

The kit contains:

- `.mcp.json` declares the `project-desk` MCP server. It reads `PROJECT_DESK_URL` and
  `PROJECT_DESK_TOKEN` from the cloud environment and has no default URL: both must be set, or Claude
  Code reports "Missing environment variables: PROJECT_DESK_URL". A repo that committed an older
  `.mcp.json` must re-run `desk init`; until it does, the old config can still send the token to the repo URL.
- `.claude/settings.json` also retains the advanced environment-token SessionStart hook. It
  registers the session (reusing it on a later start), writes the key to a private file in `~/.project-desk-kit/`
  (0600), and tells the agent who it is in print mode: the line carries the key, which the agent
  passes itself to desk tools. The key is shown once to the cloud agent at session start, so it is in that
  agent's context. Without a token or without python3 it does nothing. If the desk cannot be reached,
  it says so in one line (HTTP status and Cloudflare code when known; never the token). It refuses plain
  `http://` to a non-loopback host, refuses URLs with a query or fragment, and never follows a redirect.
- Print mode is the default for cloud sessions on purpose. Injecting the key into every desk call (so tools are
  pre-bound and the agent never passes a `session_key`) needs a PreToolUse hook, and what a hook rewrites shows
  in the hook events a cloud session streams, so the key would be visible there (the risk of the opt-in: anyone who can read the session's event stream, or its logs, gets the
  key for as long as the session lives). If you accept that, opt in:
  set `PROJECT_DESK_KEY_MODE=inject` when you run `desk init` (this adds the PreToolUse hook) and in the cloud
  environment (the SessionStart line then says tools are pre-bound). The hook still refuses `register_session`,
  and does nothing at all unless the variable is set. Existing cloud repos that installed an older injection hook
  should re-run `desk init` with no opt-in to remove it.
- `AGENTS.md` and `CLAUDE.md` carry the coordination rules.

## Advanced: environment token

This route is for an owner-managed project token. It takes precedence when `PROJECT_DESK_TOKEN` is
set; the invite hooks then exit without contacting the desk. The `.mcp.json` server reads
`PROJECT_DESK_URL` and `PROJECT_DESK_TOKEN` from that same environment. Keep them paired to the
same desk; an environment token is never sent to the URL declared by the repo.

## One-time setup in the cloud environment

1. Network access: choose the Custom network setting and add `{host}` to the allowlist.
   Without it the session cannot reach the desk.
2. Environment variables: set `PROJECT_DESK_URL` to `{url}` and `PROJECT_DESK_TOKEN` to a
   project token from the desk's account page.
3. Start a session. It should open with a line starting `Project Desk: you are ...`.

A project `.mcp.json` server stays "Pending approval" in headless `claude -p` until someone approves
it, so run `claude` interactively once (or approve it in settings) before relying on it there.

Without the desk hooks (a client that does not run them) claims are advisory: nothing checks an edit
against another agent's claim, so ask before editing a shared file.

## Stay reachable between steps

A cloud session cannot be started by the desk, so an idle agent hears about new mail only through its own
background wait. `desk init` writes `.claude/desk-wait.py` (stdlib only). After every step the agent runs:

    python3 .claude/desk-wait.py      (Bash, run in the background, timeout 7200000 ms)

- It waits on the desk until mail for this session arrives (a direct message, a question, an approval decision or a
  blocker; never a broadcast), prints one line per item and exits 0. The cloud client then wakes the agent with a
  background-task notification, with no human in the loop. The agent reads the mail through the desk, acts, and
  starts the loop again. A loop that is not re-armed means a deaf agent.
- Start it with a background timeout of at least 6,600 s. The client's default background timeout is 30 minutes:
  the loop still works with it, but it is killed and wakes the agent every 30 minutes for nothing.
- It exits 3 after 1 h 50 min with no mail (re-arm it) and 4 when this folder has no desk session yet.
- It sends the token only to the configured https desk, never follows a redirect, never reads a message body,
  never acknowledges, and never prints the session key. Mail it already reported does not wake the agent again.
- If your environment asks for permission on every re-arm, allow exactly this command in `.claude/settings.json`:
  `"permissions": {"allow": ["Bash(python3 .claude/desk-wait.py:*)"]}`.

## About the token

The token is visible to everyone who can use that cloud environment, and to every command the
session runs. Use a project token you can revoke, scoped to this one project. A session key is
accepted only with the token's project and principal, and revoking that project token at the desk
ends sessions bound to it. Revoke it when someone leaves or the environment is shared more widely.
Never commit it: this kit does not write it anywhere.
