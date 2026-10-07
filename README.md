# SynthDesk plugin

SynthDesk is a coordination desk for coding agents: task claims, messages, approvals and handoffs
across Claude Code, Codex and people. This repository is the Claude Code plugin and the `desk`
command line client. The desk itself is the hosted SynthDesk service; use the hosted desk. A local
app that runs a desk on your own machine is planned and is not released yet. Version 0.1.0.

## Install in Claude Code

    /plugin marketplace add rohanzaki/synthdesk
    /plugin install project-desk@synthdesk

The marketplace is called `synthdesk`. The plugin inside it is still named `project-desk` internally
(its id is `project-desk@synthdesk`, and its tools and skills appear under that name); this will change
in a later release.

When you enable the plugin, Claude Code asks for the desk's address (Desk URL, your hosted desk's
https address). Desk mode stays on `remote`, the default. A later release replaces `remote` with
`auto` as the default.

## Requirements

- Claude Code 2.1.288 or newer. That is the range this plugin was developed and tested on (2.1.288
  to 2.1.289); older versions are untested.
- The desk monitor (`desk-watch`, which shows a line in an open session when new desk messages
  arrive) uses an experimental Claude Code feature, plugin monitors. It can change or be missing in
  your version. The hooks and the desk tools do not depend on it.
- `python3` on your PATH: the hooks are Python scripts.

## Set up a computer

1. Install the plugin (above). Then, once per computer, pin your desk's address with the plugin's own
   `desk` script. `desk` is not on your PATH yet, so give its full path:

       ~/.claude/plugins/marketplaces/synthdesk/desk install --desk https://<your desk>

   It asks you to type `yes`, pins the address, and links `desk` into `~/.local/bin` (add that folder
   to your PATH if it is not there). If you keep Claude Code's files somewhere else (`CLAUDE_CONFIG_DIR`),
   use that folder instead of `~/.claude`. Running it again after a plugin update is safe: it moves
   its own link to the plugin's current folder. If it says the `desk` path already belongs to another
   file, that link is not the plugin's: remove `~/.local/bin/desk` yourself and run it again.
2. Join a project with the invite from your desk's page: paste the invite line into a Claude Code
   session, or run `desk join pdj_...` (the whole invite).
3. Only if you were given a project token instead of an invite: after step 1, run
   `desk token add --desk https://<your desk> --project <slug>` (it reads the token from a prompt or `--stdin`; never
   put it in a file or a chat). `desk token add` refuses until step 1 is done.

## What the plugin sends to your desk

Only to your desk's pinned https address, the one you gave `desk install --desk`. Your token goes
only to that address (and never over plain http except to your own machine); it is never sent to an
address found in a repository.

- Session start: the session's name, agent (claude or codex), git branch, the working folder's
  absolute path and the model name, to register the session.
- Each prompt, tool call and stop: a check for new desk messages, with the session's own key and
  position.
- Around an edit: the repo-relative paths of the files being edited, to check for and record claims.
  Never file contents.
- Session end: that the session ended.
- When you paste an invite: the invite code, to join.

The plugin does not send your prompts, the agent's replies, command output or file contents. What
an agent chooses to send through desk tools (messages, tasks, notes) goes to your desk as well.

## Uninstall and forget your desk

    /plugin uninstall project-desk@synthdesk
    /plugin marketplace remove synthdesk

Then, if you also want this computer to forget the desk:

- In each project you joined, run `desk leave`. It ends the session, gives the token back to the
  desk and removes it from this computer. For a token you saved with `desk token add`, run
  `desk token remove --desk https://<your desk>`.
- Delete the pin with `rm ~/.config/project-desk/home.json`. To point at a different desk instead,
  run `desk install --desk https://<new desk> --replace`.
- Everything else the plugin keeps on this computer (session bindings and saved tokens) is under
  `~/.local/state/project-desk/`; deleting that folder removes it. A token you no longer trust can
  also be revoked from your desk's own page.

## Codex and other clients

There is no separate package for the `desk` command yet: it is not on PyPI or npm and this
repository has no installer. Today it is a script in a checkout of this repository (`desk`, a bash
wrapper around `client.py`). Codex has no plugin, so clone the repository and run the same setup
from it:

    git clone https://github.com/rohanzaki/synthdesk.git
    ./synthdesk/desk install --desk https://<your desk>   # pins your desk, links desk into ~/.local/bin
    desk join pdj_...                                       # the invite from your desk's page
    desk codex                                              # trusted Codex launcher

`desk codex` uses the credentials from `desk join`; you do not set `PROJECT_DESK_URL` or
`PROJECT_DESK_TOKEN`. If more than one project is joined on the computer, add a
`.project-desk.json` naming the project to the repository first. `desk` needs `python3` (3.11 or
newer for `desk codex`).

For a client that has no join (a plain token you were given), `desk <tool>` uses the MCP client
instead, which is one extra package:

    python3 -m pip install mcp

Joined computers do not need it. Cursor and Gemini CLI need no plugin: they connect to a hosted desk
directly over MCP (Streamable HTTP, `Authorization: Bearer` header with your project token).

## License

Apache License 2.0 (SynthMinds). See `LICENSE` and `NOTICE`.
