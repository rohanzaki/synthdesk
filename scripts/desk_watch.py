#!/usr/bin/env python3
"""desk watch: one stdout line per new message for this session, for a plugin monitor.

Claude Code plugin monitors turn each stdout line of a long-running command into a
notification, so an idle session hears about mail without anyone polling. The wait
happens on the desk (wait_for), so an idle watcher costs the agent nothing.

It finds this session's binding (written by the hooks, with `cwd`), waits, prints
`DESK: <kind> from <sender>: <first line> (ref <id>)` (at most 200 chars), and never
acknowledges or reads a body: the agent reads the message through the desk, and the
sender stays untrusted peer content, exactly as in the hook output.
"""
import argparse
import collections
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_hooks as hooks
import desk_http
import desk_pocket

LINE_MAX = 200
WAIT_SECONDS = 50
CALL_TIMEOUT = 60
NO_BINDING_SLEEP = 30
BACKOFF_START, BACKOFF_CAP = 5, 300
ERROR_EVERY = 600
SEEN_MAX = 1000
PASSIVE = {'X-Project-Desk-Passive': '1'}
EXIT_MAIL, EXIT_DEADLINE, EXIT_NO_BINDING, EXIT_ALREADY = 0, 3, 4, 5
WAKE_DEADLINE = 6600
FIRED_MAX = 500
KIT_DIR = Path.home() / '.project-desk-kit'


def find_binding(state_dir, cwd):
    """The newest private binding under `state_dir` whose recorded cwd is `cwd`; failing that, the session `desk join`
    made for this folder (desk_pocket.joined_binding: no hooks binding exists until the first hook runs), or None."""
    newest = None
    for path in Path(state_dir).glob('*.json'):
        try:
            binding = hooks.private_read(path)
            stamp = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if (binding.get('cwd') == str(cwd) and binding.get('session_key')
                and (newest is None or stamp > newest[0])):
            newest = (stamp, binding)
    if newest:
        return newest[1]
    try:
        joined = desk_pocket.joined_binding(desk_pocket._thread_id(os.environ), cwd, home=hooks.POCKET_HOME)
    except Exception:
        return None
    return {**joined, 'joined': True} if joined else None


def line_for(kind, sender, first_line, ref):
    tail = f' (ref {ref})'
    head = f'DESK: {hooks.compact(kind, 20)} from {hooks.compact(sender, 60)}: '
    room = LINE_MAX - len(head) - len(tail)
    text = hooks.first_line(first_line, 10000)
    if len(text) > room:
        text = text[:max(0, room - 1)] + '…'
    return head + text + tail


class Seen:
    """Ids already announced, oldest forgotten first once SEEN_MAX is reached."""
    def __init__(self, limit=SEEN_MAX):
        self.limit, self.order, self.members = limit, collections.deque(), set()

    def add(self, key):
        """True when `key` was new."""
        if key in self.members:
            return False
        self.members.add(key)
        self.order.append(key)
        while len(self.order) > self.limit:
            self.members.discard(self.order.popleft())
        return True


def watch(call, find, emit, clock=time.monotonic, sleep=time.sleep, stop=lambda: False):
    """Run until `stop()`. `call(binding, tool, args)`, `find()` -> binding or None, `emit(line)` prints."""
    seen, session, failures, last_error = Seen(), None, 0, None
    while not stop():
        binding = find()
        if not binding:
            sleep(NO_BINDING_SLEEP)
            continue
        key = binding['session_key']
        try:
            digest = call(binding, 'check_in', {'session_key': key, 'include': ['inbox_digest']}).get('inbox_digest', [])
            first = binding['session_id'] != session
            for item in digest:
                if item.get('to') == 'you' and seen.add(('m', item['id'])) and not first:
                    emit(line_for(item.get('kind') or 'message', item.get('sender_name') or item.get('sender', '?'),
                                  item.get('first_line', ''), item['id']))
            session = binding['session_id']
            waited = call(binding, 'wait_for', {'session_key': key, 'timeout': WAIT_SECONDS})
            if waited.get('restarting'):
                sleep(retry_delay(waited))
                failures = 0
                continue
            for change in waited.get('changed', []):
                if change.get('kind') == 'decision':
                    for ref in change.get('ids', []):
                        if seen.add(('d', ref)):
                            emit(line_for('decision', 'the human', 'a decision on your approval request', ref))
                elif change.get('kind') == 'meeting' and change.get('new_posts'):
                    ref = change.get('meeting_id', '?')
                    if seen.add(('r', ref, change.get('last_seq'))):
                        emit(line_for('meeting', ref, f"{change['new_posts']} new post(s); read_meeting since {change.get('since', 0)}", ref))
            failures = 0
        except (desk_http.DeskError, OSError, ValueError, KeyError, TypeError) as error:
            now = clock()
            if last_error is None or now - last_error >= ERROR_EVERY:
                last_error = now
                emit(hooks.compact(error_line(error), LINE_MAX))
            sleep(min(BACKOFF_START * 2 ** failures, BACKOFF_CAP))
            failures += 1


class FiredStore:
    """Ids this standing loop already woke the agent for, kept across re-arms in a private file (oldest dropped
    first). Mail the agent saw but did not acknowledge must not wake it again on every re-arm."""
    def __init__(self, path, limit=FIRED_MAX):
        self.path, self.limit = Path(path), limit
        try:
            self.ids = [str(ref) for ref in json.loads(self.path.read_text())][-limit:]
        except (OSError, ValueError, TypeError):
            self.ids = []

    def __contains__(self, ref):
        return ref in self.ids

    def lock(self):
        '''Hold the per-session loop lock (a file beside the store); False when another loop holds it.'''
        if getattr(self, '_lock', None) is not None:
            return True
        fd = os.open(str(self.path) + '.lock', os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock = fd
        return True

    def unlock(self):
        if getattr(self, '_lock', None) is not None:
            os.close(self._lock)
            self._lock = None

    def add(self, ref):
        if ref in self.ids:
            return
        self.ids = (self.ids + [ref])[-self.limit:]
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(self.ids, out)
        os.chmod(self.path, 0o600)


def priority_of(item):
    """The desk class as an int: it sends P0..P4 strings (an int is accepted too); None when absent or malformed."""
    value = item.get('priority')
    if isinstance(value, str) and len(value) == 2 and value[0] in 'Pp' and value[1] in '01234':
        value = int(value[1])
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 4 else None


def wakes(item, max_priority):
    """Mail addressed to this session; once the desk sends a priority class (P0 blocker .. P4 broadcast), only classes
    up to max_priority. A broadcast never wakes anyone."""
    if item.get('to') != 'you' or not item.get('id'):
        return False
    priority = priority_of(item)
    return True if priority is None and 'priority' not in item else priority is not None and priority <= max_priority and priority < 4


def label(item):
    priority = priority_of(item)
    kind = item.get('kind') or 'message'
    return 'P%d %s' % (priority, kind) if priority is not None else kind


def until_mail(call, find, emit, fired, deadline=WAKE_DEADLINE, max_priority=3, clock=time.monotonic, sleep=time.sleep):
    """See _until_mail. One loop per session: a second one exits EXIT_ALREADY without calling the desk, so two
    armed loops never wake the agent twice for the same mail; the lock is released when the loop ends."""
    locked = []
    try:
        return _until_mail(call, find, emit, fired, deadline, max_priority, clock, sleep, locked)
    finally:
        for store in locked:
            store.unlock()


def _until_mail(call, find, emit, fired, deadline=WAKE_DEADLINE, max_priority=3, clock=time.monotonic, sleep=time.sleep, locked=None):
    """Wait until priority mail for this session arrives, print one line per item and return EXIT_MAIL; return
    EXIT_DEADLINE after `deadline` seconds, or EXIT_NO_BINDING when no session was ever found. `fired` is a
    FiredStore, or a callable binding -> FiredStore. Reads only the digest and wait_for; never a body, never an ack."""
    end = clock() + deadline
    found, failures, last_error = False, 0, None
    need_digest, direct = True, 0
    while clock() < end:
        binding = find()
        if not binding:
            sleep(max(0, min(NO_BINDING_SLEEP, end - clock())))
            continue
        found, key = True, binding['session_key']
        store = fired(binding) if callable(fired) else fired
        if locked is not None and store not in locked:
            if not store.lock():
                emit(' DESK: a wait loop is already armed for this session; not starting a second one.')
                return EXIT_ALREADY
            locked.append(store)
        say = lambda line: emit(line.replace(key, '[private]'))
        try:
            digest = (call(binding, 'check_in', {'session_key': key, 'include': ['inbox_digest']}).get('inbox_digest', [])
                      if need_digest else [])
            need_digest = False
            fresh = [item for item in digest if wakes(item, max_priority) and item['id'] not in store]
            if fresh:
                for item in fresh:
                    say(line_for(label(item), item.get('sender_name') or item.get('sender', '?'),
                                 item.get('first_line', ''), item['id']))
                    store.add(item['id'])
                return EXIT_MAIL
            timeout = max(1, min(WAIT_SECONDS, int(end - clock())))
            waited = call(binding, 'wait_for', {'session_key': key, 'timeout': timeout})
            if waited.get('restarting'):
                sleep(retry_delay(waited))
                need_digest = True
                continue
            unread_direct = waited.get('unread_direct') if isinstance(waited.get('unread_direct'), int) else 0
            need_digest = (any(change.get('kind') != 'decision' for change in waited.get('changed', []))
                           or unread_direct > direct)
            direct = unread_direct
            decisions = [ref for change in waited.get('changed', []) if change.get('kind') == 'decision'
                         for ref in change.get('ids', []) if 'd:' + ref not in store]
            for ref in decisions:
                say(line_for('decision', 'the human', 'a decision on your approval request', ref))
                store.add('d:' + ref)
            if decisions:
                return EXIT_MAIL
            failures = 0
        except (desk_http.DeskError, OSError, ValueError, KeyError, TypeError) as error:
            now = clock()
            need_digest = True
            if last_error is None or now - last_error >= ERROR_EVERY:
                last_error = now
                emit(hooks.compact(error_line(error), LINE_MAX))
            sleep(max(0, min(BACKOFF_START * 2 ** failures, BACKOFF_CAP, end - clock())))
            failures += 1
    if not found:
        emit('DESK: no Project Desk session found for this folder; register first.')
        return EXIT_NO_BINDING
    span = f'{int(deadline // 60)} min' if deadline >= 120 else f'{int(deadline)} s'
    emit(f'DESK: no priority mail in {span}; re-arm the wait loop.')
    return EXIT_DEADLINE


def desk_base(url):
    """The desk base URL: cloud environments set PROJECT_DESK_URL with /mcp already on the end."""
    url = (url or '').rstrip('/')
    return url[:-len('/mcp')] if url.endswith('/mcp') else url


def binding_from_file(path):
    """A session file written by the join helper (session_id, session_key, ...), or None when missing or not
    private. The desk URL comes from PROJECT_DESK_URL when the file does not carry one."""
    try:
        binding = hooks.private_read(Path(path))
    except (OSError, ValueError):
        return None
    if not binding.get('session_key'):
        return None
    return {**binding, 'url': binding.get('url') or os.environ.get('PROJECT_DESK_URL', '')}


def find_kit_binding(folder, cwd, url):
    """The cloud kit's binding for `cwd` (the longest matching folder, then the newest), as session_start writes it."""
    best = None
    for path in Path(folder).glob('s-*.json'):
        binding = binding_from_file(path)
        where = (binding or {}).get('cwd') or ''
        if not binding or desk_base(binding.get('url')) != desk_base(url) or not where:
            continue
        if cwd == where or cwd.startswith(where.rstrip(os.sep) + os.sep):
            rank = (len(where), path.stat().st_mtime)
            if best is None or rank > best[0]:
                best = (rank, binding)
    return best[1] if best else None


def retry_delay(reply):
    """The desk returns retry_in during restart; malformed values fall back to the contract default."""
    try:
        delay = float(reply.get('retry_in', 5))
    except (TypeError, ValueError):
        return 5
    return max(0, delay)


def error_line(error):
    """What to tell the human about a failed wait: a refusal or a redirect is not an outage, so it never says
    "cannot reach the desk". Uses the typed error's own text (safe: no arguments, no token); it also recognises the
    HTTP status in the message."""
    kind, text = type(error).__name__, str(error) if isinstance(error, desk_http.DeskError) else ''
    status = re.search(r'HTTP (\d{3})', text)
    code = int(status.group(1)) if status else 0
    detail = hooks.compact(text.split(': ', 1)[-1], 90) if text else kind
    if kind == 'DeskAuthError' or code in (401, 403):
        return f'DESK: watch was refused by the desk ({detail}); check PROJECT_DESK_TOKEN. Retrying.'
    if kind == 'DeskRedirect' or 300 <= code < 400:
        return f'DESK: the desk redirected the watcher ({detail}); check PROJECT_DESK_URL. Retrying.'
    return f'DESK: watch cannot reach the desk, retrying ({kind})'


def passive_call(base, tool, args, token, timeout):
    """desk_http.call with the passive header: this long poll is not agent activity. desk_http never follows
    a redirect, never sends the token over plain http to a remote host, and names this caller in the User-Agent."""
    return desk_http.call(base, tool, args, token=token, timeout=timeout, headers=PASSIVE, component='watch')


def joined_token_for(binding, base):
    """The joined token for a watcher BINDING, only when the binding IS the joined session: the same session id, key
    and project as the join this computer holds for that thread and folder, resolved from the pocket's own verified
    state (never from where the binding file happened to be found). A binding planted anywhere else, or left over from
    an earlier join, gets no bearer."""
    joined = desk_pocket.joined_binding(binding.get('thread_id') or None, binding.get('cwd'), home=hooks.POCKET_HOME)
    if not joined or any(joined[key] != binding.get(key) for key in ('session_id', 'session_key', 'project')):
        return ''
    return desk_pocket.joined_token(base, joined['project'], home=hooks.POCKET_HOME)


def make_call(configured):
    def call(binding, tool, args):
        base = desk_base(binding.get('desk') or binding.get('url') or configured)
        token = desk_http.env_token_for(base, os.environ)
        if not token and binding.get('joined'):
            token = joined_token_for(binding, base)
            if not token:
                raise desk_http.DeskAuthError(f'{tool}: joined session not verified; no call sent')
        return passive_call(base, tool, args, token, CALL_TIMEOUT)
    return call


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=('claude', 'codex'), default='claude')
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--until-mail', action='store_true',
                        help='exit on the first priority item (the cloud standing loop): 0 mail, 3 deadline, 4 no session')
    parser.add_argument('--deadline', type=int, default=WAKE_DEADLINE, help='seconds before --until-mail gives up')
    parser.add_argument('--max-priority', type=int, default=3, help='highest class that wakes (0 blocker .. 3 direct)')
    parser.add_argument('--binding', help='a private session file (join helper); default: the hooks or cloud kit binding')
    args = parser.parse_args()
    state_dir = hooks.STATE_ROOT.parent / args.agent
    out = lambda line: print(line, flush=True)
    if args.until_mail:
        configured = os.environ.get('PROJECT_DESK_URL', hooks.DEFAULT_DESK_URL)
        if args.binding:
            find = lambda: binding_from_file(args.binding)
            fired = FiredStore(Path(args.binding).with_name(Path(args.binding).stem + '.fired.json'))
        else:
            find = lambda: (find_kit_binding(KIT_DIR, args.cwd, configured) or find_binding(state_dir, args.cwd))
            fired = lambda binding: FiredStore(KIT_DIR / f"fired-{binding['session_id']}.json")
            KIT_DIR.mkdir(mode=0o700, exist_ok=True)
        try:
            sys.exit(until_mail(make_call(configured), find, out, fired, args.deadline, args.max_priority))
        except KeyboardInterrupt:
            sys.exit(EXIT_DEADLINE)
    try:
        watch(make_call(hooks.DEFAULT_DESK_URL), lambda: find_binding(state_dir, args.cwd), out)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
