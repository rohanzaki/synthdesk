#!/usr/bin/env python3
"""Diagnose why a session cannot reach Project Desk or is not seeing its messages.

    desk_doctor.py

    desk_doctor.py --forget-token [URL]   drop the cached token for a desk (default: the configured one)

One line per check; a warn or fail adds an indented `fix:` line. Exit code 1 when a check
failed or the arguments were wrong, 0 otherwise (a warn does not fail). Never prints the token,
a session_key or a response body. Standard library plus the desk's own modules, so it runs under a
plain python3."""
import http.client
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import codex_hooks
import desk_git
import desk_http
from desk_launch import LOCAL_APP, _option, can_start_local, desk_url, should_launch
HOOK_MARKS = ('codex_hooks.py', 'desk_launch.py')
PROBE_TIMEOUT = 3
PASSIVE = {'X-Project-Desk-Passive': '1'}
USAGE = 'usage: desk doctor [--forget-token [URL]]'


def http_get(url, headers, data=None, timeout=3):
    """(status, body); (None, '') when nothing answers. A `data` body makes it a JSON POST.
    A redirect is reported as its 3xx status, never followed. Goes through desk_http._open (no redirects, and no
    proxy for a loopback desk); a bearer is never sent over plain http to a remote host."""
    if any(str(k).lower() == 'authorization' for k in headers) and not desk_http._secure_for_token(url):
        return None, ''
    try:
        request = urllib.request.Request(url, data=data, headers={**headers, 'User-Agent': desk_http.user_agent('doctor')})
        with desk_http._open(request, timeout) as reply:
            return reply.status, reply.read(65536).decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        return error.code, ''
    except (OSError, ValueError, http.client.HTTPException):
        return None, ''


def _item(name, status, detail, fix=''):
    return {'name': name, 'status': status, 'detail': detail, 'fix': fix}


def _shown(url):
    """scheme://host[:port] only: a URL may carry userinfo."""
    parts = urlsplit(url)
    host = parts.hostname or ''
    host = f'[{host}]' if ':' in host else host
    return f'{parts.scheme}://{host}' + (f':{parts.port}' if parts.port else '')


def _ask(fetch, url, headers, data=None):
    try:
        return fetch(url, headers, data) if data is not None else fetch(url, headers)
    except (OSError, ValueError, http.client.HTTPException):
        return None, ''


def _wellformed(url):
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return False
    return parts.scheme in ('http', 'https') and bool(parts.hostname)


_loopback = desk_http._loopback


def _reachable(url, fetch, cloud, source='', option=False):
    status, _ = _ask(fetch, url + '/health', {})
    if status == 200:
        return _item('desk reachable', 'ok', _shown(url) + source), True
    host = urlsplit(url).hostname or ''
    if cloud:
        fix = f"add {host} to the cloud environment's network allowlist, then restart the session"
    elif option:
        fix = "start the desk, or check the plugin's desk URL option"
    else:
        fix = 'start the desk, or check PROJECT_DESK_URL'
    detail = f'{_shown(url)} did not answer' if status is None else f'{_shown(url)} answered {status}'
    if status is not None and 300 <= status < 400:
        detail, fix = f'{_shown(url)} redirected ({status}); not followed', 'set PROJECT_DESK_URL to the final URL'
    return _item('desk reachable', 'fail', detail + source, fix), False


def _token(url, token, headers, fetch, reachable, elsewhere=False):
    if _loopback(url) and not token:
        return _item('token', 'ok', 'not needed for a local desk')
    if not token and elsewhere:
        return _item('token', 'fail', 'the token you set belongs to another desk, so none is sent to this one',
                     'set PROJECT_DESK_URL with PROJECT_DESK_TOKEN for the same desk, or run desk token add')
    if not token:
        return _item('token', 'fail', 'no token set for a remote desk', 'set PROJECT_DESK_TOKEN to the desk token')
    if not desk_http._secure_for_token(url):
        return _item('token', 'fail', 'not sent: the desk URL is plain http to a remote host (or carries userinfo)',
                     'use an https desk URL, or a loopback address (an SSH-forwarded port works)')
    if not reachable:
        return _item('token', 'warn', 'not verified, the desk did not answer', 'fix the desk reachability above, then run desk doctor again')
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode()
    status, _ = _ask(fetch, url + '/mcp', {**headers, 'Content-Type': 'application/json',
                                          'Accept': 'application/json, text/event-stream'}, body)
    if status is not None and 300 <= status < 400:
        return _item('token', 'warn', 'redirected: not verified', 'set PROJECT_DESK_URL to the final URL')
    if status == 200:
        return _item('token', 'ok', 'accepted by the desk')
    if status in (401, 403):
        return _item('token', 'fail', 'token rejected', 'set PROJECT_DESK_TOKEN to a current desk token')
    return _item('token', 'fail', 'could not verify the token' if status is None else f'desk answered {status}',
                 'check PROJECT_DESK_URL and that the desk serves /mcp')


def _has_hooks(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return False
    text = json.dumps(data.get('hooks', {})) if isinstance(data, dict) else ''
    return any(mark in text for mark in HOOK_MARKS)


def _codex_note(home):
    path = Path(home) / '.codex/hooks.json'
    try:
        found = any(mark in path.read_text() for mark in HOOK_MARKS)
    except OSError:
        found = False
    return 'codex hooks: yes' if found else 'codex hooks: no'


def _plugin_enabled(home, cwd):
    """The plugin is on in a settings file. A Bash command does not inherit CLAUDE_PLUGIN_ROOT."""
    for base in (Path(home) / '.claude', Path(cwd) / '.claude'):
        for name in ('settings.json', 'settings.local.json'):
            try:
                enabled = json.loads((base / name).read_text()).get('enabledPlugins')
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(enabled, dict) and any(k.startswith('project-desk@') and v is True for k, v in enabled.items()):
                return True
    return False


def _hooks(environ, home, cwd):
    note = _codex_note(home)
    if environ.get('CLAUDE_PLUGIN_ROOT'):
        return _item('hooks installed', 'ok', f'plugin; {note}')
    if _plugin_enabled(home, cwd):
        return _item('hooks installed', 'ok', f'plugin enabled in settings; {note}')
    if _has_hooks(Path(home) / '.claude/settings.json'):
        return _item('hooks installed', 'ok', f'claude settings; {note}')
    root = Path(codex_hooks.__file__).resolve().parent
    return _item('hooks installed', 'warn', f'no Project Desk hook in ~/.claude/settings.json; {note}',
                 f'install the plugin, or: python3 {root}/codex_hooks.py install --agent claude')


def _find_binding(cwd, state_root):
    """(the newest binding whose folder holds cwd or is above it, count of bindings with no folder)."""
    here = Path(cwd).resolve()
    roots = [state_root] if isinstance(state_root, (str, Path)) else list(state_root)
    folderless, best = 0, None
    for root in roots:
        for path in sorted(Path(root).glob('*.json')):
            try:
                binding = codex_hooks.private_read(path)
                if not isinstance(binding, dict):
                    continue
                if not binding.get('cwd'):
                    folderless += 1
                    continue
                folder = Path(binding['cwd']).resolve()
            except (OSError, ValueError, TypeError):
                continue
            if (folder == here or folder in here.parents) and (
                    best is None or (binding.get('bound_at') or 0) > (best.get('bound_at') or 0)):
                best = binding
    return best, folderless


def _bound(binding, folderless):
    if binding:
        return _item('session bound', 'ok', f"session {binding.get('session_id', '?')}")
    fix = 'set PROJECT_DESK_AUTO_REGISTER=1 before starting the session (it records the folder)'
    if folderless:
        return _item('session bound', 'warn', f'{folderless} binding(s) without a folder (made by enable_notifications); '
                     'cannot tell if one is this session', fix)
    return _item('session bound', 'warn', 'no desk session is bound to this folder', fix)


def _project(binding, url):
    if not binding:
        return _item('project', 'ok', 'not checked, no session bound')
    if codex_hooks.guards_off(binding):
        return _item('project', 'warn', "this folder's session is in the undeclared default project: "
                     'edit and commit guards are off',
                     f'connect the repo at {_shown(url)}/projects, or run desk join <link>')
    return _item('project', 'ok', str(binding.get('project') or 'unknown')[:60])


def _guard(cwd):
    """(the check, whether the pre-commit guard is installed)."""
    try:
        out = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--git-path', 'hooks/pre-commit'],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return _item('git guard', 'ok', 'git not available, skipped'), False
    if out.returncode:
        return _item('git guard', 'ok', 'not inside a git repository'), False
    hook = Path(cwd) / out.stdout.strip()
    try:
        installed = 'project-desk guard' in hook.read_text()
    except OSError:
        installed = False
    if installed:
        try:
            rewrite = 'project-desk guard' in (hook.parent / 'post-rewrite').read_text()
        except OSError:
            rewrite = False
        if not rewrite:
            return _item('git guard', 'warn', 'pre-commit guard installed, but the post-rewrite hook is missing (rebased and amended '
                         'commits are not re-recorded)', f'python3 "{Path(__file__).resolve().parent}/desk_git.py" install'), True
        return _item('git guard', 'ok', 'pre-commit and post-rewrite guards installed'), True
    return _item('git guard', 'warn', 'no Project Desk pre-commit guard in this repo',
                 f'python3 "{Path(__file__).resolve().parent}/desk_git.py" install'), False


ROLE_SHADOWS = ('.claude/skills/desk-role', '.claude/skills/project-desk-role', '.agents/skills/desk-role',
                '.agents/skills/project-desk-role', '.codex/skills/desk-role')


def _role_skill_shadow(cwd):
    """A warning when this repo ships a skill that could pass for the Project Desk role skill (F5), else None.
    A repo skill is text the repo's author wrote; the role comes only from the desk."""
    try:
        out = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, timeout=5)
        top = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else Path(cwd)
    except (OSError, subprocess.TimeoutExpired):
        top = Path(cwd)
    found = [rel for rel in ROLE_SHADOWS if os.path.lexists(top / rel)]
    if not found:
        return None
    return _item('role skill', 'warn', f"{', '.join(found)} could pass for the Project Desk role skill",
                 'remove it; your role comes only from desk(tool="role")')


def _token_source(binding, environ, url):
    """Where the guard's token comes from, never its value."""
    if not binding.get('desk'):
        return 'from environment' if desk_http.env_token_for(url, environ) else 'none'
    token = desk_http.token_for(url, environ, project=binding.get('project'))
    if not token:
        refusal = desk_http.credentials_refusal(desk_http.credentials_path(environ))
        return f'none ({refusal[0]})' if refusal else 'none'
    return 'from environment' if desk_http.env_token_for(url, environ) else 'cached'


def _guard_auth(binding, environ):
    """Can the commit guard authenticate? One passive check_in, resolved exactly as the guard resolves it."""
    url = str(binding.get('desk') or codex_hooks.DEFAULT_DESK_URL).rstrip('/')
    shown, note = _shown(url), f'token: {_token_source(binding, environ, url)}'
    if not binding.get('session_key'):
        return _item('guard auth', 'ok', f'not checked, the binding holds no session key; {note}')
    if not binding.get('desk') and os.environ.get('PROJECT_DESK_HOOK_TRANSPORT') == 'cli':
        return _item('guard auth', 'ok', 'not checked, the cli hook transport is not passive and would count as a '
                     f'check-in; {note}')
    try:
        desk_git.desk_call(binding, environ, headers=PASSIVE, timeout=PROBE_TIMEOUT)(
            'check_in', {'session_key': binding['session_key'], 'include': ['counts']})
    except desk_http.DeskAuthError:
        refusal = desk_http.credentials_refusal(desk_http.credentials_path(environ))
        return _item('guard auth', 'fail', f'the commit guard cannot authenticate to {shown}: commits are unguarded; {note}',
                     f'{refusal[1]}, then run desk token add for the pinned desk' if refusal else
                     'run desk token add for the pinned desk, or export PROJECT_DESK_TOKEN')
    except desk_http.DeskRedirect:
        return _item('guard auth', 'fail', f'{shown} redirected the guard\'s check; not followed: commits are unguarded; {note}',
                     f'the desk this folder is bound to moved: point PROJECT_DESK_URL at its final address (now {shown}), '
                     'then start a new session here')
    except desk_http.DeskError as error:
        if isinstance(error, (desk_http.DeskUnreachable, desk_http.DeskTimeout)):
            return _item('guard auth', 'warn', f'could not be checked, {shown} did not answer; {note}',
                         'check the desk is running, then run desk doctor again')
        return _item('guard auth', 'warn', f'could not be checked, {shown} answered an error; commits may be '
                     f'unguarded for this binding; {note}',
                     'start a new session in this folder so it binds again, then run desk doctor again')
    except Exception as error:
        return _item('guard auth', 'warn', f'could not be checked, the check failed ({type(error).__name__}); {note}',
                     'run desk doctor again; if it repeats, check the desk transport settings')
    return _item('guard auth', 'ok', f'commit guard authenticates to {shown}; {note}')


def checks(environ, cwd, state_root, http_get=http_get, home=None):
    home = Path(home) if home else Path.home()
    binding, folderless = _find_binding(cwd, state_root)
    url, source = desk_url(environ), ''
    if not (environ.get('PROJECT_DESK_URL') or _option(environ, 'DESK_URL')) and binding and binding.get('desk'):
        url, source = str(binding['desk']).rstrip('/'), " (from this folder's binding)"
    token = desk_http.token_for(url, environ, project=binding.get('project') if binding else None)
    if not token and not desk_http.origin(url):
        for own_token, own_url in ((environ.get('PROJECT_DESK_TOKEN'), environ.get('PROJECT_DESK_URL')),):
            if own_token and (own_url or '').strip().rstrip('/') == url:
                token = own_token.strip()
                break
    elsewhere = not token and bool(environ.get('PROJECT_DESK_TOKEN'))
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    up = False
    if _wellformed(url):
        from_option = not (environ.get('PROJECT_DESK_URL') or source) and bool(_option(environ, 'DESK_URL'))
        reach, up = _reachable(url, http_get, environ.get('CLAUDE_CODE_REMOTE') == 'true', source, from_option)
        first = [reach, _token(url, token, headers, http_get, up, elsewhere)]
    else:
        first = [_item('desk reachable', 'fail', 'URL is malformed: expected http(s)://host[:port]',
                       'set PROJECT_DESK_URL (or the plugin desk URL option) to http(s)://host[:port]'),
                 _item('token', 'warn', 'not verified, the desk URL is malformed', 'fix the desk URL above, then run desk doctor again')]
    refusal = desk_http.credentials_refusal(desk_http.credentials_path(environ))
    if refusal:
        first.append(_item('credentials', 'warn', f'{refusal[0]}; no token is cached or read from it', refusal[1]))
    if should_launch(environ) and not up and not can_start_local():
        first.append(_item('local app', 'fail', f'the SynthDesk local app ({LOCAL_APP}) was not found on PATH',
                           'install the SynthDesk local app, or set Desk mode to remote and use the hosted desk'))
    guard, installed = _guard(cwd)
    last = [guard] + ([_guard_auth(binding, environ)] if installed and binding else [])
    shadow = _role_skill_shadow(cwd)
    return (first + [_hooks(environ, home, cwd), _bound(binding, folderless), _project(binding, url)] + last
            + ([shadow] if shadow else []))


def render(results):
    lines = []
    for r in results:
        lines.append(f"{r['status']:<6}{r['name']:<17}{r['detail']}")
        if r['status'] != 'ok' and r['fix']:
            lines.append(f"      fix: {r['fix']}")
    return '\n'.join(lines)


def _forget(url, environ):
    key = desk_http.origin(url)
    if not key:
        print('--forget-token: the desk URL is malformed: expected http(s)://host[:port]', file=sys.stderr)
        return 1
    path = desk_http.credentials_path(environ)
    refusal = desk_http.credentials_refusal(path)
    if refusal:
        print(f'--forget-token: {refusal[0]}; {refusal[1]}', file=sys.stderr)
        return 1
    joined = [''.join(c for c in name if c.isprintable())[:40] for name in desk_http.joined_projects(url, path)]
    left = (f'; tokens saved by `desk join` for {", ".join(joined)} at {key} are kept and still sent '
            '(`desk leave` in that project removes one)') if joined else ''
    if desk_http.forget_token(url, path):
        print(f'forgot the cached token for {key}{left}')
    elif key in desk_http._read_cache(path):
        print(f'--forget-token: the cached token for {key} could not be removed (is {path.parent} writable?)', file=sys.stderr)
        return 1
    else:
        print(f'no cached token for {key}{left}')
    return 0


def main(argv=None, environ=None, cwd=None, state_root=None, http_get=http_get, home=None):
    argv = list(argv or [])
    environ = os.environ if environ is None else environ
    if argv and (argv[0] != '--forget-token' or len(argv) > 2):
        print(USAGE, file=sys.stderr)
        return 1
    if argv:
        return _forget(argv[1].rstrip('/') if len(argv) == 2 else desk_url(environ), environ)
    state = state_root or [codex_hooks.STATE_ROOT.parent / 'claude', codex_hooks.STATE_ROOT]
    results = checks(environ, cwd or os.getcwd(), state, http_get, home)
    print(render(results))
    return 1 if any(r['status'] == 'fail' for r in results) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
