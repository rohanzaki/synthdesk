---
name: desk-doctor
description: Diagnose why this session is not seeing Project Desk messages or cannot reach the desk. Use when the user asks why the desk is silent, when desk tools fail, or after installing the plugin.
---

# Project Desk doctor

1. Run the checks:

       python3 "${CLAUDE_PLUGIN_ROOT}/scripts/desk_doctor.py"

   A Bash command carries no plugin options, so the script reads the desk URL from this folder's session
   binding when no URL is set, and says so on the `desk reachable` line.
   The script prints one line per check (`ok`, `warn` or `fail`). A `warn` or `fail` line is
   followed by an indented `fix:` line.
2. Report every `warn` and `fail` line to the user together with its `fix:` line, verbatim.
   If every line is `ok`, say so in one sentence.
3. Do not apply a fix that changes settings, hooks or git hooks unless the user asks.

The script never prints the desk token or a `session_key`. Never add them to your report.
