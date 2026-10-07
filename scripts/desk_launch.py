#!/usr/bin/env python3
"""Start and reach the private local desk for the Claude Code plugin.

    desk_launch.py ensure --data-dir DIR [--port PORT] [--timeout S]
    desk_launch.py hook [--ensure [--quick]] [--agent claude]

`ensure` makes sure a desk answers on 127.0.0.1:PORT, starting the SynthDesk local app
(`synthdesk-local` on PATH) when none does (database, log and pidfile in DIR). It does nothing in
remote mode, and exits 2 when the app is not installed. `hook` is what hooks/hooks.json runs: it turns the plugin's user options into
the PROJECT_DESK_* variables the hook entry reads (so the token never appears in a process
listing), optionally ensures the desk first (hooks of one event run in parallel, so the
ordering has to live in one process), then replaces itself with `codex_hooks.py run`.

Standard library only: it runs before the desk's own dependencies exist."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import desk_http
import desk_pocket
from desk_http import DEFAULT_URL, user_agent
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
POCKET_HOME = None


LOCAL_APP = 'synthdesk-local'
NOT_INSTALLED = 2
NOT_INSTALLED_LINE = (f'SynthDesk local app not found. Install the SynthDesk local app ({LOCAL_APP}) '
                      'or set Desk mode to remote and use the hosted desk.')


def find_app(which=shutil.which):
    return which(LOCAL_APP)


def _extra_command(data_dir, bootstrap):
    return None


def _extra_markers():
    return ()


def start_command(data_dir, bootstrap=None, find=None):
    """argv that starts the local desk: the packaged app, else None."""
    exe = (find or find_app)()
    if exe:
        return [exe]
    return _extra_command(data_dir, bootstrap)


def can_start_local():
    return find_app() is not None or _extra_command(None, None) is not None




def http_probe(port, timeout=0.5):
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{port}/health', headers={'User-Agent': user_agent('launch')})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        markers = (LOCAL_APP.encode(),) + tuple(_extra_markers())
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
        return any(marker in cmdline for marker in markers)
    except OSError:
        return True


def server_env(data_dir, port, base=None):
    env = dict(os.environ if base is None else base)
    env.update(PROJECT_DESK_DB=str(Path(data_dir) / 'desk.sqlite3'), PROJECT_DESK_PORT=str(port),
               PROJECT_DESK_ROSTER=str(Path(data_dir) / 'ROSTER.md'),
               PROJECT_DESK_IDLE_EXIT='1800')
    return env


def spawn(data_dir, port, command):
    log = open(Path(data_dir) / 'desk.log', 'ab')
    cwd = Path(command[-1]).parent if len(command) > 1 else Path(data_dir)
    process = subprocess.Popen(command, cwd=str(cwd), env=server_env(data_dir, port),
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    log.close()
    return process.pid


def _read_pid(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def ensure(data_dir, port, timeout=30, mode=None, probe=http_probe, start=spawn, bootstrap=None,
           alive=pid_alive, sleep=time.sleep, clock=time.monotonic, find=None):
    """0 when a desk answers on PORT (already, or after we started one), NOT_INSTALLED when there is no
    local app to start, else 1."""
    if (mode if mode is not None else os.environ.get('CLAUDE_PLUGIN_OPTION_MODE', 'local')) == 'remote':
        return 0
    if probe(port):
        return 0
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    deadline = clock() + timeout
    missing = False
    with open(data_dir / 'desk.lock', 'a') as lock:
        if fcntl: fcntl.flock(lock, fcntl.LOCK_EX)
        if not probe(port):
            pid = _read_pid(data_dir / 'desk.pid')
            if pid is None or not alive(pid):
                command = start_command(data_dir, bootstrap, find)
                if command is None:
                    missing = True
                else:
                    pid = start(data_dir, port, command)
                    (data_dir / 'desk.pid').write_text(f'{pid}\n')
        if fcntl: fcntl.flock(lock, fcntl.LOCK_UN)
    if missing:
        return NOT_INSTALLED
    while clock() < deadline:
        if probe(port):
            return 0
        sleep(0.25)
    return 1


FAILED_START = 'start-failed'
QUICK_TIMEOUT = 8
START_BACKOFF = 300


def _fresh(stamp):
    try:
        return time.time() - stamp.stat().st_mtime < START_BACKOFF
    except OSError:
        return False


def _option(environ, name, default=''):
    return (environ.get('CLAUDE_PLUGIN_OPTION_' + name) or default).strip()


def mode_for(environ):
    """'local' or 'remote'. The plugin's mode option is local, remote or auto (its default). An explicit local or
    remote always wins. Auto is remote only when home.json pins a hosted desk AND this computer holds a joined entry
    for it (and the desk URL option is still the local default); otherwise local. A join never writes the plugin's
    configuration: this is read at each hook, from the pin and the credentials."""
    mode = _option(environ, 'MODE', 'local')
    if mode != 'auto':
        return 'remote' if mode == 'remote' else 'local'
    if _option(environ, 'DESK_URL', DEFAULT_URL).rstrip('/') != DEFAULT_URL.rstrip('/'):
        return 'local'
    return 'remote' if desk_pocket.auto_remote_url(POCKET_HOME, environ) else 'local'


def desk_url(environ):
    named = environ.get('PROJECT_DESK_URL')
    option = _option(environ, 'DESK_URL', DEFAULT_URL).rstrip('/')
    if not named and option == DEFAULT_URL.rstrip('/') and mode_for(environ) == 'remote':
        return desk_pocket.pinned_url(POCKET_HOME, environ) or option
    return (named or option).rstrip('/')


def port_for(environ):
    try:
        return urlsplit(desk_url(environ)).port or 7331
    except ValueError:
        return 7331


def should_launch(environ):
    """Local mode, and a desk URL that points at this machine. Never boots a desk for a foreign host."""
    if mode_for(environ) == 'remote':
        return False
    try:
        return urlsplit(desk_url(environ)).hostname in LOOPBACK
    except ValueError:
        return False


def hook_env(environ):
    """PROJECT_DESK_* for the hook entry; deprecated plugin tokens never reach it."""
    env = dict(environ)
    env.pop('CLAUDE_PLUGIN_OPTION_TOKEN', None)
    env.setdefault('PROJECT_DESK_URL', desk_url(environ))
    if environ.get('PROJECT_DESK_TOKEN'):
        own = desk_http.origin(environ.get('PROJECT_DESK_URL') or DEFAULT_URL)
        if not own or own != desk_http.origin(env['PROJECT_DESK_URL']):
            del env['PROJECT_DESK_TOKEN']
    env.setdefault('PROJECT_DESK_AUTO_REGISTER', '1')
    return env


def merge_notice(output, line):
    """The hook's own output with `line` shown to the user as a systemMessage. Output that is not a JSON object
    is returned untouched: a notice never corrupts what the hook said."""
    if not output.strip():
        return json.dumps({'systemMessage': line})
    try:
        data = json.loads(output)
    except ValueError:
        return output
    if not isinstance(data, dict):
        return output
    data['systemMessage'] = f"{data['systemMessage']}\n{line}" if isinstance(data.get('systemMessage'), str) and data['systemMessage'] else line
    return json.dumps(data)


def run_hook(argv, environ):
    """(exit code, stdout) of the hook entry, with stdin passed through and stdout captured."""
    result = subprocess.run(argv, env=dict(environ), stdout=subprocess.PIPE, text=True)
    return result.returncode, result.stdout


def main(argv=None, environ=None, ensure_fn=ensure, execv=os.execv, probe_fn=http_probe, run_hook=run_hook,
         write=print):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    e = sub.add_parser('ensure')
    e.add_argument('--data-dir', required=True)
    e.add_argument('--port', type=int)
    e.add_argument('--timeout', type=float, default=30)
    h = sub.add_parser('hook')
    h.add_argument('--ensure', action='store_true')
    h.add_argument('--quick', action='store_true', help='short start timeout; skip the start for 5 minutes after a failed one')
    h.add_argument('--agent', default='claude')
    args = parser.parse_args(argv)
    if args.command == 'ensure':
        mode = 'local' if should_launch(environ) else 'remote'
        code = ensure_fn(args.data_dir, args.port or port_for(environ), timeout=args.timeout, mode=mode)
        if code == NOT_INSTALLED:
            print(NOT_INSTALLED_LINE, file=sys.stderr)
        return code
    same_source = bool(environ.get('PROJECT_DESK_URL')) == bool(environ.get('PROJECT_DESK_TOKEN'))
    env = hook_env(environ)
    if 'PROJECT_DESK_TOKEN' not in env:
        environ.pop('PROJECT_DESK_TOKEN', None)
    environ.update(env)
    environ.pop('CLAUDE_PLUGIN_OPTION_TOKEN', None)
    if mode_for(environ) == 'remote' and same_source:
        try:
            desk_http.remember_token(desk_url(environ), environ.get('PROJECT_DESK_TOKEN', ''),
                                     desk_http.credentials_path(environ))
        except Exception:
            pass
    missing = False
    if args.ensure and should_launch(environ):
        data = environ.get('CLAUDE_PLUGIN_DATA') or str(Path.home() / '.local/share/project-desk-plugin')
        stamp = Path(data) / FAILED_START
        if args.quick and probe_fn(port_for(environ)):
            stamp.unlink(missing_ok=True)
        elif args.quick and _fresh(stamp):
            pass
        else:
            try:
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.write_text(f'{time.time()}\n')
            except OSError:
                pass
            code = ensure_fn(data, port_for(environ), timeout=QUICK_TIMEOUT if args.quick else 45, mode='local')
            if code == 0:
                stamp.unlink(missing_ok=True)
            missing = code == NOT_INSTALLED
    command = [sys.executable, str(ROOT / 'codex_hooks.py'), 'run', '--agent', args.agent]
    if missing:
        code, output = run_hook(command, environ)
        write(merge_notice(output, NOT_INSTALLED_LINE))
        return code
    execv(sys.executable, command)
    return 0


if __name__ == '__main__':
    sys.exit(main())
