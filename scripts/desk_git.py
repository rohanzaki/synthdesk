"""Git hooks for Project Desk: refuse a commit that touches a path another session holds
(pre-commit, pre-merge-commit), and log each commit on the task that owns it (post-commit,
post-merge). While a merge, cherry-pick, revert or rebase is concluded only paths the committer
changed relative to both sides are checked. A binding's session_key goes only to the desk the
binding records.

Standard library plus codex_hooks and desk_http. Every hook fails open: a desk that
is down, or a bug here, must never stop a commit. Nothing printed carries a session key.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import codex_hooks
import desk_http
import safe_text
from desk_shared import scopes

MARK = 'project-desk guard'
HOOKS = ('pre-commit', 'pre-merge-commit', 'post-commit', 'post-merge', 'post-rewrite')
GATED = ('pre-commit', 'pre-merge-commit')
REFUSED = 3
SHIM_HEAD = '''#!/bin/sh
# project-desk guard (installed by `desk guard install`)
python3 "{root}/scripts/desk_git.py" {name}
'''
SHIM_TAIL = '''if [ -x "$0.pre-desk" ]; then exec "$0.pre-desk" "$@"; fi
exit 0
'''
REWRITE_HEAD = '''#!/bin/sh
# project-desk guard (installed by `desk guard install`)
PAIRS=$(cat)
printf '%s\\n' "$PAIRS" | python3 "{root}/scripts/desk_git.py" {name} "$@"
'''
REWRITE_TAIL = '''if [ -x "$0.pre-desk" ]; then printf '%s\\n' "$PAIRS" | "$0.pre-desk" "$@"; fi
exit 0
'''
LIVE_WINDOW = 900
UNSEEN_WINDOW = 86400
CHUNK = 100
INCOMING = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'REBASE_HEAD')
SHIM_GATE = '[ $? -eq 3 ] && exit 1\n'


def _git(repo, *args, check=True):
    out = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=15)
    if check and out.returncode:
        raise ValueError(out.stderr.strip() or f'git {args[0]} failed')
    return out.stdout


def _git_status(repo, *args):
    """(exit code, stdout) without raising: some callers need git's exit code (merge-tree answers 1 for conflicts).
    A git that timed out or cannot run is (None, ''): the callers fall back to the strict rule, never to "allowed"."""
    try:
        out = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None, ''
    return out.returncode, out.stdout


def _names(text):
    return [name for name in text.split('\0') if name]


def staged_paths(repo):
    return _names(_git(repo, 'diff', '--cached', '--name-only', '-z', '--no-renames', '--diff-filter=ACMRD'))


def _state_roots():
    base = codex_hooks.STATE_ROOT.parent
    return [base / 'codex', base / 'claude']


def _fresh(state, now):
    if state.get('ended_at'):
        return False
    seen = state.get('last_check')
    if isinstance(seen, (int, float)):
        return now - max(seen, state.get('bound_at') or 0) <= LIVE_WINDOW
    return now - (state.get('bound_at') or 0) <= UNSEEN_WINDOW


def bindings_for(repo, state_root=None, now=None):
    """Live, non-superseded bindings whose cwd is inside this working tree (not a nested linked worktree), newest first.
    state_root is one directory or a list; the default searches the codex and claude roots."""
    repo = Path(repo).resolve()
    now = time.time() if now is None else now
    top = _git(repo, 'rev-parse', '--show-toplevel', check=False).strip()
    top = Path(top).resolve() if top else repo
    roots = _state_roots() if state_root is None else [state_root] if isinstance(state_root, (str, Path)) else state_root
    found = []
    for root in roots:
        for path in sorted(Path(root).glob('*.json')):
            try:
                state = codex_hooks.private_read(path)
                cwd = Path(state['cwd']).resolve()
                if (not (cwd == repo or repo in cwd.parents) or not state['session_key'] or not _fresh(state, now)
                        or state.get('superseded_by')):
                    continue
                owner = _git(cwd, 'rev-parse', '--show-toplevel').strip()
                if Path(owner).resolve() == top:
                    found.append(state)
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return sorted(found, key=lambda b: max(b.get('bound_at') or 0, b.get('last_check') or 0), reverse=True)


def binding_for(repo, state_root=None):
    found = bindings_for(repo, state_root)
    return found[0] if found else None


def _named(live, environ, state_roots=None, now=None):
    """The distinct bindings the session variables name: from `live` first, else any fresh, non-superseded
    binding in the state roots (an agent bound in ANOTHER checkout still says who it is)."""
    now = time.time() if now is None else now
    roots = _state_roots() if state_roots is None else [state_roots] if isinstance(state_roots, (str, Path)) else state_roots
    named = []
    for field, value in (('session_id', environ.get('PROJECT_DESK_SESSION')), ('thread_id', environ.get('CLAUDE_CODE_SESSION_ID'))):
        if not value:
            continue
        hit = [b for b in live if b.get(field) == value]
        if not hit:
            for root in roots:
                for path in sorted(Path(root).glob('*.json')):
                    try:
                        state = codex_hooks.private_read(path)
                        if (state[field] == value and state['session_key'] and state['thread_id'] and _fresh(state, now)
                                and not state.get('superseded_by')):
                            hit.append(state)
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
        if len(hit) == 1 and not any(n is not None and (n is hit[0] or (n.get('thread_id') and n['thread_id'] == hit[0].get('thread_id')))
                                     for n in named):
            named.append(hit[0])
        elif len(hit) > 1:
            named.append(None)
    return named


def committer(live, environ, state_roots=None, named=None):
    """(binding, ambiguous): who is committing. Same checkout is not same agent, so a variable that names nobody
    here is never read as "us". PROJECT_DESK_SESSION (the plugin, or `desk codex`) names the desk session;
    CLAUDE_CODE_SESSION_ID (Claude Code exports it to Bash, so to this hook) names the agent thread.
    Both are inherited by every child of a Bash command (a Codex started from Claude's shell carries Claude's), so
    only a variable that names ONE live binding in this checkout identifies the committer; one naming a binding
    elsewhere, or two different bindings, identifies nobody. Never a Codex binding's agent_proc: Codex threads
    share one app-server process. (None, False) means no live session here: a human.
    `named` is _named(...) when the caller has it already."""
    if not live:
        return None, False
    named = _named(live, environ, state_roots) if named is None else named
    if len(named) == 1 and any(named[0] is b for b in live):
        return named[0], False
    if not named and len(live) == 1 and not (environ.get('PROJECT_DESK_SESSION') or environ.get('CLAUDE_CODE_SESSION_ID')):
        return live[0], False
    return None, True


def desk_call(binding, environ, headers=None, timeout=None):
    """A call to the desk this binding was made with, never to another: its session_key goes nowhere else.
    The token is the configured one when that desk is the configured one (PROJECT_DESK_*, then plugin options), else the
    one saved for exactly this origin and the binding's project (desk_http.token_for). `headers` and `timeout` shape every call it makes (the
    doctor's probe: passive and short)."""
    extra = {**({} if timeout is None else {'timeout': timeout}), **({} if headers is None else {'headers': headers})}
    recorded = binding.get('desk')
    if not recorded:
        if headers is None and timeout is None:
            return lambda tool, args: codex_hooks.call_desk(tool, args)
        base = codex_hooks.DEFAULT_DESK_URL
        return lambda tool, args: desk_http.call(base, tool, args, token=desk_http.env_token_for(base, environ),
                                                 component='guard', **extra)
    url = str(recorded).rstrip('/')
    if not codex_hooks._origin(url):
        def refuse(tool, args):
            raise desk_http.DeskUnreachable(f'{tool}: the binding records an unusable desk URL')
        return refuse
    token = desk_http.token_for(url, environ, project=binding.get('project'))
    return lambda tool, args: desk_http.call(url, tool, args, token=token, component='guard', **extra)


def _soft(blocker, now):
    """Why this blocker warns instead of refusing, or '': its holder went quiet (codex_hooks.soft_reason), or ended
    (it has since ended). Local, so the guard does not depend on a newer desk."""
    reason = codex_hooks.soft_reason(blocker, now, None)
    ended, seen = codex_hooks._epoch_of(blocker.get('holder_ended_at') or ''), codex_hooks._epoch_of(blocker.get('holder_last_seen') or '')
    if not reason and ended is not None and (seen is None or ended >= seen):
        reason = f"held by ended session {blocker.get('owner')} (ended {blocker.get('holder_ended_at')})"
    return reason


_known = []


def _know(*values):
    for value in values:
        if isinstance(value, str) and len(value) >= 8 and value not in _known:
            _known.append(value)


def _redact(text):
    """[private] for every known secret: the whole value, then any 20-character run of it, so a key that a length
    cap cut in half still disappears."""
    text = str(text)
    for secret in sorted(_known, key=len, reverse=True):
        text = text.replace(secret, '[private]')
    for secret in _known:
        for i in range(max(len(secret) - 19, 0) if len(secret) >= 20 else 0):
            text = text.replace(secret[i:i + 20], '[private]')
    return text


def _unsafe(text, limit):
    """Peer or file-name text made safe to print (safe_text.line), with every known secret removed before and
    after the length cap."""
    return _redact(_redact(safe_text.line(text, 100000))[:limit])


def _quoted(text, limit):
    return json.dumps(_unsafe(text, limit), ensure_ascii=False)


def _err(message):
    print(_redact(message), file=sys.stderr)


def _who(blocker):
    name, owner = blocker.get('owner_name'), blocker.get('owner')
    return f'{_unsafe(name, 60)} ({_unsafe(owner, 60)})' if name else _unsafe(owner, 60)


def _outage(error):
    """(what the desk did, whether `desk doctor` helps), in the typed error's own words: a rejected token or a
    redirect leaves the commit UNGUARDED, which is not "unreachable"."""
    if isinstance(error, desk_http.DeskAuthError):
        code = re.search(r'HTTP (\d+)', str(error))
        return f"the desk refused this machine's credentials (HTTP {code.group(1) if code else '401'})", True
    if isinstance(error, desk_http.DeskRedirect):
        return _unsafe(error, 300), True
    if isinstance(error, desk_http.DeskTimeout) or (isinstance(error, desk_http.DeskError)
                                                     and not isinstance(error, desk_http.DeskUnreachable)):
        return _unsafe(error, 300), False
    return 'desk unreachable', False


def _guardable(paths):
    """(paths the desk accepts, paths the desk would refuse: ':' '*' '?', over 500 characters)."""
    ok, skipped = [], []
    for path in paths:
        try:
            scopes([path])
            ok.append(path)
        except ValueError:
            skipped.append(path)
    return ok, skipped


def _steps(repo):
    """(marker, commit) for each commit the step being concluded brings in; [] outside such a step."""
    steps = []
    for name in INCOMING:
        code, where = _git_status(repo, 'rev-parse', '--git-path', name)
        if code != 0:
            continue
        try:
            lines = (Path(repo) / where.strip()).read_text().splitlines()
        except OSError:
            continue
        steps += [(name, line.split()[0]) for line in lines if line.strip() and re.fullmatch(r'[0-9a-f]{40}([0-9a-f]{24})?', line.split()[0])]
    return steps


def _incoming(repo):
    """Commits the step being concluded brings in (MERGE_HEAD, CHERRY_PICK_HEAD, ...); [] outside such a step."""
    return [head for _, head in _steps(repo)]


def _auto_tree(repo, kind, head):
    """The tree git itself produced for this merge-like step, or None when it cannot be known (an old git, an
    octopus merge): what the committer changed by hand is whatever differs from it, not from the two sides."""
    code, name = _git_status(repo, 'rev-parse', '--symbolic-full-name', 'AUTO_MERGE')
    if code == 0 and name.strip() == 'AUTO_MERGE':
        code, tree = _git_status(repo, 'rev-parse', '-q', '--verify', 'AUTO_MERGE^{tree}')
        if code == 0 and re.fullmatch(r'[0-9a-f]{40}([0-9a-f]{24})?', tree.strip()):
            return tree.strip()
    sides = {'MERGE_HEAD': ['HEAD', head], 'REVERT_HEAD': ['--merge-base', head, 'HEAD', head + '^']}.get(kind)
    if sides is None:
        sides = ['--merge-base', head + '^', 'HEAD', head]
    code, out = _git_status(repo, 'merge-tree', '--write-tree', *sides)
    first = out.splitlines()[0].strip() if out.strip() else ''
    return first if code in (0, 1) and re.fullmatch(r'[0-9a-f]{40}([0-9a-f]{24})?', first) else None


def _own_changes(repo, paths, steps):
    """(A squash merge, or `cherry-pick -n` of several commits, leaves no marker: its held files are refused, strictly.)
    Staged paths that differ from HEAD (they are in `paths`), from the other side of every step, and from what git
    itself merged: what the committer changed by hand while concluding it. Passing through the other side's work, or
    git's own merge of both, is not theirs. A hand edit on top of either still differs, so it is checked."""
    mine = set(paths)
    for kind, head in steps:
        other = head + '^' if kind == 'REVERT_HEAD' else head
        code, out = _git_status(repo, 'diff', '--cached', '--name-only', '-z', '--no-renames', other)
        if code == 0:
            mine &= set(_names(out))
    if len(steps) == 1:
        tree = _auto_tree(repo, *steps[0])
        if tree:
            code, out = _git_status(repo, 'diff', '--cached', '--name-only', '-z', '--no-renames', tree)
            if code == 0:
                mine &= set(_names(out))
    return [p for p in paths if p in mine]


_unconnected = codex_hooks.guards_off


def _own_blockers(ask, asker, paths):
    """Blocker-shaped entries for staged paths the asker itself holds (a task that is not done), as post_commit finds
    its task: check_in's my_tasks and _covers. They look like would_conflict's, so the caller treats them alike."""
    tasks = ask('check_in', {'session_key': asker['session_key'], 'include': ['my_tasks']}).get('my_tasks') or []
    own = []
    for task in tasks:
        if task.get('status') in ('DONE', 'CANCELLED'):
            continue
        for resource in task.get('resources') or []:
            if any(_covers(resource, path) for path in paths):
                own.append({'resource': resource, 'task_id': task.get('id'), 'title': task.get('title'),
                            'owner': asker.get('session_id')})
    return own


GUARD_NOTICE = 'Project Desk guard (automatic): a commit bypassed the guard on paths you hold'
HEX = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
MAX_NOTICES = 8


def _notice(task_id, who, paths):
    """What a blocked owner is sent. Fixed guard text first, marked automatic, so nothing the committer chose (a file
    name) can read as a message from a session; every name is JSON-quoted and comes last."""
    shown = ', '.join(_quoted(p, 80) for p in paths[:5]) + (f' (+{len(paths) - 5} more)' if len(paths) > 5 else '')
    return f'{GUARD_NOTICE}: task {_unsafe(task_id, 40)}, committed by {who}. Files: {shown}'


def _tell_owners(committer_binding, finders, blockers, live, call, environ):
    """A bypass is not silent: each blocked owner is sent what was committed. With an identified committer it
    is the sender (never its own holder); otherwise the session that found the blocker, or the first live binding on
    that desk that is not the owner. Best effort: returns (sent, not_told). A desk that does not answer ends the
    round; any other error (an unknown or ended recipient) only skips that owner."""
    groups = {}
    for blocker in blockers:
        owner = blocker.get('owner')
        if not owner:
            continue
        group = groups.setdefault((owner, blocker.get('task_id')), {'finder': finders.get(id(blocker)), 'paths': []})
        group['paths'].extend(blocker.get('your_resources') or [blocker.get('resource')])
    sent, down = 0, False
    for (owner, task_id), group in list(groups.items())[:MAX_NOTICES]:
        if down:
            break
        if committer_binding:
            sender = committer_binding if committer_binding.get('session_id') != owner else None
        else:
            finder = group['finder']
            desk = str((finder or {}).get('desk') or '')
            usable = [b for b in [finder] + live if b and b.get('session_id') != owner and not _unconnected(b)
                      and str(b.get('desk') or '') == desk]
            sender = usable[0] if usable else None
        if not sender:
            continue
        paths = list(dict.fromkeys(str(r) for r in group['paths']))
        who = _quoted(sender.get('callsign') or sender.get('session_id') or 'a session', 60) if committer_binding \
            else 'an unidentified session in this checkout'
        try:
            (call or desk_call(sender, environ))('send_message', {'session_key': sender['session_key'], 'recipient': owner,
                                                                  'body': _redact(_notice(task_id, who, paths))})
            sent += 1
        except (desk_http.DeskUnreachable, desk_http.DeskTimeout, OSError):
            down = True
        except (desk_http.DeskError, ValueError):
            continue
    return sent, len(groups) - sent


def pre_commit(repo, call, state_root, environ, hook='pre-commit'):
    """0 allows the commit, 1 refuses it. `hook` is the git hook that ran this. With PROJECT_DESK_GUARD=off it still
    checks, only to tell each blocked owner, and then allows."""
    bypass = environ.get('PROJECT_DESK_GUARD') == 'off'
    say = (lambda message: None) if bypass else _err
    live = bindings_for(repo, state_root)
    if not live:
        return 0
    del _known[:]
    _know(environ.get('PROJECT_DESK_TOKEN'), environ.get('CLAUDE_PLUGIN_OPTION_TOKEN'), *(b.get('session_key') for b in live))
    paths = staged_paths(repo)
    steps = _steps(repo)
    if steps:
        paths = _own_changes(repo, paths, steps)
    elif hook == 'pre-merge-commit':
        return 0
    paths, unguardable = _guardable(paths)
    if unguardable:
        shown = ', '.join(_unsafe(p, 80) for p in unguardable[:3]) + (f' (+{len(unguardable) - 3} more)' if len(unguardable) > 3 else '')
        say(f'project-desk guard: unguardable path(s) not checked: {shown}')
    if not paths:
        return 0
    named = _named(live, environ, state_root)
    binding, ambiguous = committer(live, environ, state_root, named)
    elsewhere = [b for b in named if b and not any(b is l for l in live)]
    if ambiguous:
        askers = [b for b in elsewhere + live if not _unconnected(b)][:4]
    else:
        askers = [binding] if binding else []
    if binding and _unconnected(binding) or not askers:
        say('project-desk guard: this repo is not connected to a desk project; the commit guard is off here (desk doctor explains).')
        return 0
    for b in askers + elsewhere:
        _know(b.get('session_key'), desk_http.token_for(str(b.get('desk') or desk_http.DEFAULT_URL).rstrip('/'), environ, project=b.get('project')))
    ours = {b.get('session_id') for b in live if b.get('session_id')}
    callsigns = {b.get('session_id'): b.get('callsign') for b in live if b.get('callsign')}
    found, finders, down, now = {}, {}, {}, time.time()
    for asker in askers:
        desk = str(asker.get('desk') or '')
        if desk in down:
            continue
        ask = call or desk_call(asker, environ)
        done = 0
        try:
            for done in range(0, len(paths), CHUNK):
                reply = ask('would_conflict', {'session_key': asker['session_key'], 'resources': paths[done:done + CHUNK]})
                for b in reply.get('blockers') or []:
                    if found.setdefault((b.get('resource'), b.get('task_id')), b) is b:
                        finders[id(b)] = asker
            done = len(paths)
            if ambiguous:
                for b in _own_blockers(ask, asker, paths):
                    if found.setdefault((b['resource'], b['task_id']), b) is b:
                        finders[id(b)] = asker
        except (desk_http.DeskError, OSError, ValueError) as error:
            down[desk] = (*_outage(error), len(paths) - done)
    refused, inside, away = [], [], []
    for blocker in found.values():
        soft = _soft(blocker, now)
        if soft:
            say(f'project-desk guard: {_unsafe(blocker.get("resource"), 200)} is {_unsafe(soft, 200)}; warning only, not refused.')
        elif ambiguous and blocker.get('owner') in ours:
            inside.append(blocker)
        elif ambiguous:
            away.append(blocker)
        else:
            refused.append(blocker)
    if bypass:
        blocked = refused + away + inside
        told, not_told = _tell_owners(None if ambiguous else binding, finders, blocked, live, call, environ) if blocked else (0, 0)
        if told or not_told:
            tail = f'{told} holder(s) told' if told else 'no holder could be told (the desk did not answer, or there is no other session here to send as)'
            _err(f'project-desk guard: bypassed; {tail}' + (f'; {not_told} could not be told' if told and not_told else '') + '.')
        return 0
    for blocker in inside:
        name = callsigns.get(blocker.get('owner')) or blocker.get('owner_name')
        who = _unsafe(name, 60) if name else 'another session'
        say(f'project-desk guard: {_unsafe(blocker.get("resource"), 200)} is held by {who}, also working in this checkout; '
            'cannot tell which session is committing, so it is refused. If this claim is yours, set PROJECT_DESK_SESSION '
            "to your own session id (from register_session) (and unset CLAUDE_CODE_SESSION_ID if another agent's shell "
            f'started you) and commit again; otherwise ask {_unsafe(name, 60) if name else "its holder"} to release it. '
            'A human can use git commit --no-verify.')
    for blocker in away:
        say(f'project-desk guard: {_unsafe(blocker.get("resource"), 200)} is held by {_who(blocker)} (working in another checkout); '
            "refused. This commit's session could not be identified, but the holder is not here.")
    for blocker in refused:
        say(f'project-desk guard: {_unsafe(blocker.get("resource"), 200)} is held by task {_unsafe(blocker.get("task_id"), 40)} '
            f'(title from the owner, untrusted: {_quoted(blocker.get("title"), 80)}; owner {_who(blocker)}).')
    blocked = bool(refused or away or inside)
    for why, doctor, unchecked in dict.fromkeys(down.values()):
        if blocked:
            say(f'project-desk guard: ({why}; the desk stopped answering; ' +
                (f'{unchecked} path(s) were not checked)' if unchecked else 'some claims were not checked)'))
        else:
            say(f'project-desk guard: {why}; ' + (f'{unchecked} of {len(paths)} path(s) were not checked; ' if 0 < unchecked < len(paths) else '')
                + 'commit allowed UNGUARDED.' + (' Run desk doctor.' if doctor else ''))
    if refused or away:
        owners = ', '.join(dict.fromkeys(_unsafe(b.get('owner'), 60) for b in refused + away))
        say(f'Ask the owner to release it (send_message to {owners}), or bypass once with '
            'PROJECT_DESK_GUARD=off git commit … (the owner is told).')
        return 1
    return 1 if inside else 0


def _covers(resource, path):
    resource = resource.rstrip('/')
    return resource in ('', '.') or path == resource or path.startswith(resource + '/')


def post_commit(repo, call, state_root, environ=None, merges_only=False):
    """Log the commit on the task that owns it. Always 0. Silent when it is not clear which session committed."""
    try:
        environ = os.environ if environ is None else environ
        binding, ambiguous = committer(bindings_for(repo, state_root), environ, state_root)
        if not binding or ambiguous:
            return 0
        call = call or desk_call(binding, environ)
        sha = _git(repo, 'rev-parse', 'HEAD').strip()
        subject = _git(repo, 'log', '-1', '--format=%s').strip()
        merge = len(_git(repo, 'log', '-1', '--format=%P').split()) > 1
        if merges_only and not merge:
            return 0
        files = _names(_git(repo, 'diff' if merge else 'show', *(['--name-only', '-z', 'HEAD^1', 'HEAD'] if merge
                                                              else ['--name-only', '-z', '--format=', 'HEAD'])))
        key = binding['session_key']
        tasks = call('check_in', {'session_key': key, 'include': ['my_tasks']}).get('my_tasks') or []
        running = [t for t in tasks if t.get('status') == 'RUNNING']
        task = next((t for t in running if any(_covers(r, f) for r in t.get('resources') or [] for f in files)),
                    running[0] if running else None)
        if not task:
            return 0
        more = ' …' if len(files) > 5 else ''
        call('log_progress', {'session_key': key, 'task_id': task['id'],
                              'entry': f'commit {sha[:10]}: {subject} ({len(files)} files: {", ".join(files[:5])}{more})'})
    except Exception:
        pass
    return 0


def post_rewrite(repo, call, state_root, pairs, environ=None):
    """Log `rewrite <old10> -> <new10>` (at most 20 pairs) on the task that owns each rewritten commit, so
    a rebased or amended commit does not orphan the journal. Always 0, silent when the committer is unclear."""
    try:
        environ = os.environ if environ is None else environ
        binding, ambiguous = committer(bindings_for(repo, state_root), environ, state_root)
        if not binding or ambiguous:
            return 0
        call = call or desk_call(binding, environ)
        key = binding['session_key']
        tasks = call('check_in', {'session_key': key, 'include': ['my_tasks']}).get('my_tasks') or []
        running = [t for t in tasks if t.get('status') == 'RUNNING']
        for old, new in list(pairs)[:20]:
            if not (HEX.fullmatch(str(old)) and HEX.fullmatch(str(new))):
                continue
            try:
                files = _names(_git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z', '--root', new))
            except ValueError:
                files = []
            task = next((t for t in running if any(_covers(r, f) for r in t.get('resources') or [] for f in files)),
                        running[0] if running else None)
            if task:
                call('log_progress', {'session_key': key, 'task_id': task['id'], 'entry': f'rewrite {old[:10]} -> {new[:10]}'})
    except Exception:
        pass
    return 0


def _shim(name):
    if name == 'post-rewrite':
        return REWRITE_HEAD.format(root=ROOT, name=name) + REWRITE_TAIL
    return SHIM_HEAD.format(root=ROOT, name=name) + (SHIM_GATE if name in GATED else '') + SHIM_TAIL


def install(repo):
    """Write the guard shims (pre-commit, pre-merge-commit, post-commit, post-merge, post-rewrite), chaining any hook
    already there."""
    repo = Path(repo).resolve()
    configured = _git(repo, 'config', 'core.hooksPath', check=False).strip()
    if configured:
        lines = '; '.join(f'{name}: python3 "{ROOT}/scripts/desk_git.py" {name}' for name in HOOKS)
        raise ValueError(f'core.hooksPath is set to {configured}; add one line to each of its hooks ({lines})')
    hooks_dir = (repo / _git(repo, 'rev-parse', '--git-path', 'hooks').strip()).resolve()
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in HOOKS:
        hook = hooks_dir / name
        if hook.exists() and MARK not in hook.read_text(errors='replace') and (hooks_dir / f'{name}.pre-desk').exists():
            raise ValueError(f'{hooks_dir / (name + ".pre-desk")} already exists; resolve it by hand before installing the guard')
    installed, chained = [], []
    for name in HOOKS:
        hook = hooks_dir / name
        shim = _shim(name)
        if hook.exists() and MARK not in hook.read_text(errors='replace'):
            hook.rename(hooks_dir / f'{name}.pre-desk')
            chained.append(name)
        if not hook.exists() or hook.read_text() != shim:
            hook.write_text(shim)
            installed.append(name)
        hook.chmod(0o755)
    return {'installed': installed, 'chained': chained, 'hooks_dir': str(hooks_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='desk_git', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in (*HOOKS, 'install'):
        sub.add_parser(name).add_argument('--repo')
    sub.choices['post-rewrite'].add_argument('kind', nargs='?')
    args = parser.parse_args(argv)
    try:
        repo = Path(args.repo or _git('.', 'rev-parse', '--show-toplevel').strip())
        if args.command == 'install':
            print(json.dumps(install(repo), indent=2))
            return 0
        if args.command in GATED:
            return REFUSED if pre_commit(repo, None, None, os.environ, args.command) else 0
        if args.command == 'post-rewrite':
            pairs = [line.split()[:2] for line in sys.stdin.read().splitlines() if len(line.split()) >= 2]
            return post_rewrite(repo, None, None, pairs)
        return post_commit(repo, None, None, merges_only=args.command == 'post-merge')
    except Exception as error:
        if args.command == 'install':
            print(f'desk guard: {error}', file=sys.stderr)
            return 1
        print(f'project-desk guard: internal error ({type(error).__name__}); commit allowed', file=sys.stderr)
        return 0


if __name__ == '__main__':
    sys.exit(main())
