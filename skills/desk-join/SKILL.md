---
name: desk-join
description: Connect the current repo to a Project Desk project, either a hosted desk (writes the cloud-session kit) or a desk the human gave a project link for. Use when the user says to join, connect or set up Project Desk for this repo.
---

# Connect this repo to Project Desk

Ask for what you do not have: a project slug and desk URL (hosted desk), or a project link
that looks like `https://desk.example.com/p/<project>`.

## Hosted desk

Run from the repo root, with the slug and URL the user gave you:

    python3 "${CLAUDE_PLUGIN_ROOT}/client.py" init --project <slug> --url <https-desk-url>

It writes `.project-desk.json`, merges a `project-desk` server into `.mcp.json`, adds a
cloud-only SessionStart and PreToolUse hooks to `.claude/settings.json`, adds the rules block to `CLAUDE.md`
and `AGENTS.md`, and writes `docs/project-desk-cloud.md`. It is safe to run twice.

## A project link

    python3 "${CLAUDE_PLUGIN_ROOT}/client.py" join <project-link>

## After it runs

1. Show the user the `changed` list and tell them to commit those files.
2. For cloud sessions, point them at `docs/project-desk-cloud.md`: allow the desk domain on the
   Custom network and set `PROJECT_DESK_URL` and `PROJECT_DESK_TOKEN` in the environment.

## Token rules

- Never ask the user to paste a desk token into the chat, and never write one into a file,
  a commit, a log or a command line. A joined session keeps its token on the user's own computer;
  a token the user was given is saved by them in a terminal with `desk token add --desk <https desk URL>`
  (after `desk install --desk <https desk URL>` has pinned the desk once), or set in the
  `PROJECT_DESK_TOKEN` environment variable. The plugin's own token setting is deprecated and never sent.
- The token goes only to the desk URL the user configured, never to a URL read from a repo.
- Desk tools are pre-bound by the plugin or kit hooks: never pass, ask for or print a `session_key`. Without
  the hooks, keep the one `register_session` returned private: do not print it, store it or commit it.
