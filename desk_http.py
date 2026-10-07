"""Call a Project Desk tool with a single HTTP POST (MCP Streamable HTTP, stateless).

Hooks run on every prompt and tool call, so spawning a Python interpreter that
imports the MCP SDK and opens an MCP session (about a second) is too slow. The
desk is stateless, so one JSON-RPC `tools/call` POST is enough. Standard library
only, so it runs under any `python3` without the desk's virtualenv.

Errors never carry the token or any credential argument (session_key, token, secret, key):
callers log them and those arguments hold session keys.
"""
import contextlib
import errno
import fcntl
import http.client
import json
import os
import pwd
import re
import secrets
import socket
import stat
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from desk_loopback import bearer_parts, loopback_host

MAX_REPLY = 4 * 1024 * 1024
PROTOCOL_VERSION = '2025-06-18'
USER_AGENT_FMT = 'project-desk/{version} ({component})'
PLUGIN_JSON = Path(__file__).resolve().parent / '.claude-plugin' / 'plugin.json'


class DeskError(RuntimeError):
    """A desk call failed. The message is safe to log: no token, no credential argument."""


class DeskAuthError(DeskError):
    """The desk refused the credentials (401/403)."""


class DeskTimeout(DeskError):
    """The desk did not answer within the client's timeout."""


class DeskUnreachable(DeskError):
    """Refused connection, DNS failure or a bad URL."""


class DeskInsecure(DeskError):
    """A credential would have gone over plain http to a host that is not this machine; nothing was sent."""


class DeskRedirect(DeskError):
    """The desk answered with a redirect, which is never followed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_DIRECT = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
SECRET_KEYS = ('session_key', 'token', 'secret', 'key')


def _token(text, fallback):
    """A User-Agent product token: letters, digits and . _ + - only, so nothing read from a file can break a header."""
    return re.sub(r'[^A-Za-z0-9._+-]', '', str(text)) or fallback


@lru_cache(maxsize=None)
def version():
    """The plugin's version, read once from .claude-plugin/plugin.json; '0' when it cannot be read."""
    try:
        found = json.loads(PLUGIN_JSON.read_text()).get('version')
    except (OSError, ValueError, AttributeError):
        return '0'
    return _token(found, '0') if isinstance(found, str) else '0'


def user_agent(component='hooks'):
    return USER_AGENT_FMT.format(version=version(), component=_token(component, 'hooks'))


def _open(request, timeout):
    return (_DIRECT if _loopback(request.full_url) else _OPENER).open(request, timeout=timeout)


def _secrets(args, token):
    """The token plus every string under a credential-named key, at any depth.

    Other argument values stay readable: redacting them all blanked section
    names and tool names in the desk's own error text.
    """
    found = {token} if token else set()
    stack, hidden = [(args, False)], []
    while stack:
        value, secret = stack.pop()
        if isinstance(value, dict):
            stack.extend((v, secret or str(k).lower() in SECRET_KEYS) for k, v in value.items())
        elif isinstance(value, (list, tuple)):
            stack.extend((v, secret) for v in value)
        elif secret and isinstance(value, str) and len(value) >= 4:
            hidden.append(value)
    found.update(hidden)
    return sorted(found, key=len, reverse=True)


def _clean(text, secrets, limit=600):
    text = str(text)
    for secret in secrets:
        text = text.replace(secret, '[redacted]')
    for secret in secrets:
        if len(secret) >= 24:
            for part in (secret[:20], secret[-20:]):
                text = text.replace(part, '[redacted]')
    return ' '.join(text.split())[:limit]


DEFAULT_PORTS = {'http': 80, 'https': 443}
DEFAULT_URL = 'http://127.0.0.1:7331'


def credentials_path(environ=os.environ):
    """Where per-origin tokens are cached (the git guard runs outside Claude, so it cannot see plugin options)."""
    given = environ.get('PROJECT_DESK_CREDENTIALS') or os.environ.get('PROJECT_DESK_CREDENTIALS')
    if given:
        return Path(given)
    home = _account_home()
    if not home:
        raise UnsafeCredentialsPath(errno.EPERM, 'account home is unavailable')
    return Path(home) / '.local/state/project-desk/credentials.json'


def origin(url):
    """scheme://host:port with the default port explicit and the host lowercased; '' when malformed."""
    try:
        parts = urlsplit(str(url).strip())
        scheme, host = parts.scheme.lower(), (parts.hostname or '').lower()
        port = DEFAULT_PORTS.get(scheme) if parts.port is None else parts.port
        userinfo = parts.username is not None or parts.password is not None or '@' in parts.netloc
    except ValueError:
        return ''
    if scheme not in DEFAULT_PORTS or not host or not port or userinfo:
        return ''
    return f'{scheme}://[{host}]:{port}' if ':' in host else f'{scheme}://{host}:{port}'


_UNSAFE_URL = re.compile(r'[\s\x00-\x1f\x7f\\]')


def _connect_host(url):
    """The lowercased host urllib will connect to, or None when the URL is ambiguous: control characters, userinfo,
    a bad port, or urlsplit and urllib/http.client reading different hosts. Every credential rule is judged on this."""
    url = str(url)
    if _UNSAFE_URL.search(url):
        return None
    try:
        parts = urlsplit(url)
        parts.port
        request = urllib.request.Request(url)
    except ValueError:
        return None
    if not parts.hostname or '@' in parts.netloc:
        return None
    host = request.host
    i, j = host.rfind(':'), host.rfind(']')
    if i > j:
        host = host[:i]
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    return parts.hostname.lower() if host.lower() == parts.hostname.lower() else None


def _loopback(url):
    """True only for the local machine, judged on the host urllib connects to (desk_loopback.loopback_host is the
    one strict rule: exact localhost / localhost., or an ipaddress-parsed 127/8 or ::1; no shorthand, zone id or
    userinfo)."""
    host = _connect_host(url)
    return bool(host) and loopback_host(host)


class UnsafeCredentialsPath(OSError):
    """A credentials path component that is a symlink, owned by someone else, or writable by others. Callers treat it
    as "no cache" (reads give nothing, writes do not happen); the message names the component, never a token."""


def _account_home():
    """The account's home from the account database, never $HOME. None when the account is unknown."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return None


def _refuse(why, where):
    raise UnsafeCredentialsPath(errno.EPERM, f'credentials path refused: {why}', str(where))


def _open_private_dir(directory, create=False):
    """An O_DIRECTORY fd for DIRECTORY, reached the one safe way and never any other way: from `/`, one component
    at a time, each opened relative to the fd of the one before with O_DIRECTORY|O_NOFOLLOW, after an lstat of the
    same name; the fstat of the opened fd must be that same inode (a component swapped in between fails closed).
    Refused (UnsafeCredentialsPath): any symlink or non-directory, an owner other than root or this user, a directory
    others can write to (except a root-owned sticky one such as /tmp, and never under the account's home), and a last
    directory that is not ours with no group/other access. `create` makes missing components 0700 (mkdirat).
    os.stat is not used on any component (it follows symlinks)."""
    absolute = os.path.abspath(os.fspath(directory))
    uid, home = os.getuid(), _account_home()
    home = os.path.abspath(home).rstrip('/') + '/' if home else None
    names = [part for part in absolute.split('/') if part]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    where = '/'
    try:
        for index, name in enumerate([''] + names):
            if name:
                where = where.rstrip('/') + '/' + name
                try:
                    seen = os.lstat(name, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(name, 0o700, dir_fd=fd)
                    seen = os.lstat(name, dir_fd=fd)
                if stat.S_ISLNK(seen.st_mode):
                    _refuse('a symlink', where)
                if not stat.S_ISDIR(seen.st_mode):
                    _refuse('not a directory', where)
                try:
                    child = os.open(name, flags, dir_fd=fd)
                except OSError:
                    _refuse('could not be opened without following links', where)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if name and not os.path.samestat(info, seen):
                _refuse('replaced while it was being opened', where)
            last = index == len(names)
            if info.st_uid not in (0, uid):
                _refuse('owned by another user', where)
            if info.st_mode & 0o022:
                inside_home = bool(home) and (where.rstrip('/') + '/').startswith(home)
                sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX and not inside_home and not last
                if not sticky_root:
                    _refuse('writable by others', where)
            if last and (info.st_uid != uid or info.st_mode & 0o077) and not (create and info.st_uid == uid):
                _refuse('not a private directory of this user', where)
        done, fd = fd, -1
        return done
    finally:
        if fd >= 0:
            os.close(fd)


def _read_at(dfd, name):
    """The cache document read from NAME inside the opened directory DFD: one no-follow open, and the checks run on
    that descriptor, so nothing can be swapped in between. Raises OSError/ValueError on anything not usable."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UnsafeCredentialsPath(errno.EPERM, 'credentials file is not a private regular file of this user', name)
        return json.load(stream)


def _parse_doc(doc):
    """Keep joined and manually paired credentials through legacy cache rewrites."""
    if not isinstance(doc, dict):
        return {'tokens': {}, 'joined': {}, 'advanced': {}}
    tokens = doc.get('tokens')
    return {'tokens': {k: v for k, v in tokens.items() if isinstance(v, str)} if isinstance(tokens, dict) else {},
            'joined': doc.get('joined', {}), 'advanced': doc.get('advanced', {})}


def _load(path):
    """(doc, error): the cache document as _parse_doc gives it, and the exception that stopped the read (None when it
    was read). A refused or unreadable cache gives the empty document and its error, so a caller can tell "nothing
    there" (FileNotFoundError) from "could not look"."""
    path = Path(path)
    try:
        dfd = _open_private_dir(path.parent)
        try:
            return _parse_doc(_read_at(dfd, path.name)), None
        finally:
            os.close(dfd)
    except (OSError, ValueError, AttributeError) as error:
        return {'tokens': {}, 'joined': {}, 'advanced': {}}, error


def _read_doc(path):
    """The cache file as {'tokens': {origin: token}, 'joined': {origin: {project: entry}}}. Every directory on the way
    is walked with a no-follow rule (_open_private_dir); a file that is not a private regular file owned by this
    user is ignored, as for session bindings (codex_hooks.private_read). Anything refused reads as an empty cache.
    A file with no `joined` key (older) reads as no joined entry."""
    return _load(path)[0]


def _read_cache(path):
    """{origin: token}: the per-origin tokens only."""
    return _read_doc(path)['tokens']


def _write_cache(path, tokens, joined=None):
    """True when written. `joined` (what `desk join` saved) is kept as it is on disk unless given; a file that is there
    but refused (not a private regular file of this user) or unreadable is never replaced (False), because the joined
    entries it holds cannot be carried over, and losing them would re-enable the legacy fallback. Every directory is
    walked with a no-follow rule and created 0700 when missing; the last one is made 0700 when this user owns it
    (one that another user owns, a symlink or a shared directory is never written to). The temp file is created
    O_EXCL|O_NOFOLLOW 0600 inside the opened directory, fsynced, renamed with os.replace(src_dir_fd=, dst_dir_fd=)
    and the directory fsynced: no step re-resolves the path."""
    path = Path(path)
    name = path.name
    try:
        dfd = _open_private_dir(path.parent, create=True)
    except OSError:
        return False
    tmp = f'.desk-{secrets.token_hex(8)}'
    try:
        try:
            os.fchmod(dfd, 0o700)
            try:
                if stat.S_ISLNK(os.lstat(name, dir_fd=dfd).st_mode):
                    return False
            except FileNotFoundError:
                pass
            if joined is None:
                try:
                    joined = _parse_doc(_read_at(dfd, name))['joined']
                except FileNotFoundError:
                    joined = {}
                except ValueError:
                    joined = {}
                except OSError:
                    return False
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'tokens': tokens, 'joined': joined if joined is not None else {}}, stream)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            os.fsync(dfd)
            return True
        except OSError:
            return False
        finally:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
    finally:
        os.close(dfd)


LOCK_NAME = '.credentials.lock'
LOCK_WAIT = 5.0


class CredentialsLockBusy(OSError):
    """Another writer held the credentials lock for longer than LOCK_WAIT."""


@contextlib.contextmanager
def _credentials_lock(path):
    """The writer lock every read-modify-write of credentials.json takes: `.credentials.lock` in the same private
    directory, exactly as the joined writer (desk_pocket._credential_lock) takes it. The directory is walked
    with a no-follow rule; the lock file is created O_CREAT|O_EXCL|O_NOFOLLOW 0600 (a lost create race falls to
    the existing file); an existing one must be a regular file of this user with mode exactly 0600 and is reopened
    O_NOFOLLOW and checked to be the inode that was inspected; then flock(LOCK_EX), polled with LOCK_NB for at most
    LOCK_WAIT seconds (CredentialsLockBusy afterwards). Anything unsafe raises UnsafeCredentialsPath."""
    path = Path(path)
    dfd = _open_private_dir(path.parent, create=True)
    lock = -1
    try:
        os.fchmod(dfd, 0o700)
        try:
            info = os.stat(LOCK_NAME, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is None:
            try:
                lock = os.open(LOCK_NAME, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
            except FileExistsError:
                info = os.stat(LOCK_NAME, dir_fd=dfd, follow_symlinks=False)
        if lock == -1:
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                _refuse(f'{LOCK_NAME} is not a private regular file of this user', path.parent)
            try:
                lock = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
            except OSError:
                _refuse(f'{LOCK_NAME} could not be opened without following links', path.parent)
            opened = os.fstat(lock)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                _refuse(f'{LOCK_NAME} changed while it was being opened', path.parent)
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CredentialsLockBusy(errno.EAGAIN, f'credentials lock busy: another desk process has held '
                                              f'{LOCK_NAME} for more than {LOCK_WAIT:g}s', str(path.parent)) from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        if lock >= 0:
            os.close(lock)
        os.close(dfd)


def remember_token(url, token, path=None):
    """Cache TOKEN for URL's origin. True only when the file changed. Never for a loopback desk (a local desk
    needs no cached token), an empty token or a malformed URL. The read-modify-write runs under the writer lock, so
    a concurrent `desk join` (or another hook) never loses its entry."""
    key = origin(url)
    if not token or not key or _loopback(url):
        return False
    path = Path(path) if path else credentials_path()
    if _read_cache(path).get(key) == token:
        return False
    try:
        with _credentials_lock(path):
            tokens = _read_cache(path)
            if tokens.get(key) == token:
                return False
            tokens[key] = token
            return _write_cache(path, tokens)
    except OSError:
        return False


def forget_token(url, path=None):
    key = origin(url)
    path = Path(path) if path else credentials_path()
    doc, error = _load(path)
    if not key or isinstance(error, FileNotFoundError) or (error is None and key not in doc['tokens']):
        return False
    try:
        with _credentials_lock(path):
            tokens = _read_cache(path)
            if key not in tokens:
                return False
            del tokens[key]
            return _write_cache(path, tokens)
    except OSError:
        return False


_HINTS = (('writable by others', 'run chmod go-w on it'),
          ('owned by another user', 'use a directory you own (set PROJECT_DESK_CREDENTIALS)'),
          ('a symlink', 'replace it with a real directory'),
          ('not a directory', 'replace it with a real directory'),
          ('not a private directory', 'run chmod 700 on it'),
          ('not a private regular file', 'run chmod 600 on it'))


def credentials_refusal(path=None):
    """(what, fix) when the walk or a file check refuses the credentials path, else None. WHAT names the component
    ("credentials path refused: writable by others: ~/.local/state"), FIX says what to do; neither holds a token.
    A missing directory or file is not a refusal (nothing was saved yet). Read-only: nothing is created or changed."""
    path = Path(path) if path else credentials_path()
    why = where = None
    try:
        dfd = _open_private_dir(path.parent)
    except FileNotFoundError:
        return None
    except UnsafeCredentialsPath as error:
        why, where = str(error.strerror), str(error.filename or '')
    except OSError:
        return None
    else:
        try:
            for name in (path.name, LOCK_NAME):
                try:
                    info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    continue
                exact = stat.S_IMODE(info.st_mode) != 0o600 if name == LOCK_NAME else info.st_mode & 0o077
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or exact:
                    why, where = 'credentials path refused: not a private regular file of this user', str(path.parent / name)
                    break
        finally:
            os.close(dfd)
    if why is None:
        return None
    home = _account_home()
    if home and home.rstrip('/') and (where == home or where.startswith(home.rstrip('/') + '/')):
        where = '~' + where[len(home.rstrip('/')):]
    what = f'{why}: {where}' if where else why
    fix = next((hint for key, hint in _HINTS if key in why), 'fix it, or set PROJECT_DESK_CREDENTIALS to a private directory')
    return what, fix


def credentials_problem(path=None):
    """The one-line reason the credentials path is refused ("what; fix"), or '' when it is usable or not there yet."""
    refusal = credentials_refusal(path)
    return f'{refusal[0]}; {refusal[1]}' if refusal else ''


def joined_projects(url, path=None):
    """The project names `desk join` saved a token for at URL's origin (names only, never a token); [] when none."""
    key = origin(url)
    joined = _read_doc(Path(path) if path else credentials_path())['joined']
    entries = joined.get(key) if key and isinstance(joined, dict) else None
    return sorted(name for name in entries if isinstance(name, str)) if isinstance(entries, dict) else []


def env_token_for(url, environ):
    """The token the environment holds for URL's origin, or ''. A token is only ever valid with the desk URL from its
    OWN source: PROJECT_DESK_TOKEN with PROJECT_DESK_URL (the default desk when no URL is exported, never the plugin's
    URL). The deprecated plugin token option is never sent. A token whose
    source names another origin is not used for this one."""
    key = origin(url)
    if not key:
        return ''
    pairs = ((environ.get('PROJECT_DESK_TOKEN'), environ.get('PROJECT_DESK_URL')),)
    for token, own_url in pairs:
        token = (token or '').strip()
        if token and origin(own_url or DEFAULT_URL) == key:
            return token
    return ''


def token_for(url, environ, path=None, project=None):
    """The token for URL: the environment's when the URL of its own source has the same origin (env_token_for); else the one `desk join` saved for
    exactly this origin and PROJECT (the repo's declared project); else the legacy per-origin token, but only when
    nothing was joined at this origin; else ''. A token never goes to another origin, and a project that holds no
    entry at a joined origin gets nothing (never a sibling project's token)."""
    key = origin(url)
    if not key:
        return ''
    env_token = env_token_for(url, environ)
    if env_token:
        return env_token
    doc = _read_doc(Path(path) if path else credentials_path(environ))
    joined = doc['joined']
    if not isinstance(joined, dict):
        return ''
    if key in joined:
        entries = joined[key]
        entry = entries.get(project) if isinstance(entries, dict) and isinstance(project, str) and project else None
        token = entry.get('token') if isinstance(entry, dict) else None
        return token.strip() if isinstance(token, str) else ''
    advanced = doc.get('advanced')
    manual = advanced.get(key) if isinstance(advanced, dict) else None
    if isinstance(manual, dict) and manual.get('project', '') in ('', project):
        saved = manual.get('token')
        if isinstance(saved, str) and saved.startswith('pdr_'):
            return saved
    return doc['tokens'].get(key, '')


def _payload(raw):
    """The JSON-RPC reply, whether the desk answered as JSON or as one SSE event."""
    text = raw.decode('utf-8', 'replace').strip()
    if text.startswith('{'):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith('data:') and line[5:].strip().startswith('{'):
            return json.loads(line[5:].strip())
    raise ValueError('no JSON-RPC reply')


def _secure_for_token(url):
    """https, or http to this machine (desk_loopback's one rule), and in both cases a URL whose host is unambiguous
    (no userinfo, query or fragment). A URL with no http(s) scheme is refused too: it makes no request, and an
    ambiguous one must not pass as if it were safe."""
    try:
        scheme = urlsplit(str(url).strip()).scheme.lower()
    except ValueError:
        return False
    if scheme not in ('http', 'https') or _connect_host(url) is None or bearer_parts(url) is None:
        return False
    return scheme == 'https' or _loopback(url)


PROTECTED_HEADERS = ('authorization', 'proxy-authorization', 'content-type', 'content-length', 'transfer-encoding',
                     'host', 'accept', 'mcp-protocol-version', 'user-agent')
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_VALUE_BAD = re.compile(r'[\x00-\x08\x0a-\x1f\x7f]')


def _extra_headers(headers):
    extra = {}
    for name, value in (headers or {}).items():
        name, value = str(name).strip(), str(value)
        if _HEADER_NAME.fullmatch(name) and name.lower() not in PROTECTED_HEADERS and not _HEADER_VALUE_BAD.search(value):
            extra[name] = value
    return extra


def call(base_url, tool, args, token='', timeout=6, headers=None, component='hooks'):
    """One tools/call. `headers` are extra request headers (the watcher's X-Project-Desk-Passive: 1), merged after
    the defaults and never allowed to override Authorization, Content-Type or User-Agent. `component` names the
    caller in the User-Agent (hooks, guard, cli, ...)."""
    if tool == 'wait_for':
        try:
            wait = int(args.get('timeout', 60))
        except (TypeError, ValueError):
            wait = 60
        timeout = min(310, max(timeout, wait + 10))
    secrets = _secrets(args, token)
    if secrets and not _secure_for_token(base_url):
        raise DeskInsecure(f'{tool}: refusing to send a credential over plain http to a remote host, or to a URL with '
                           'userinfo, a query, a fragment or no scheme; use an https desk URL, or a loopback address (an SSH-forwarded port works)')
    headers = {'Content-Type': 'application/json', 'User-Agent': user_agent(component), 'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': PROTOCOL_VERSION, **_extra_headers(headers)}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': tool, 'arguments': args}}).encode()
    try:
        request = urllib.request.Request(base_url.rstrip('/') + '/mcp', data=body, headers=headers, method='POST')
        with _open(request, timeout) as response:
            raw = response.read(MAX_REPLY)
            status = response.status
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400 and error.code != 304:
            raise DeskRedirect(f'{tool}: desk redirected (HTTP {error.code}); not followed, so no credential was '
                               're-sent. Point the desk URL at its final address.') from None
        if error.code in (401, 403):
            raise DeskAuthError(f'{tool}: desk refused the credentials (HTTP {error.code})') from None
        raise DeskError(f'{tool}: desk answered HTTP {error.code}') from None
    except (OSError, ValueError, http.client.HTTPException) as error:
        reason = getattr(error, 'reason', None)
        if isinstance(error, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
            raise DeskTimeout(f"{tool}: no reply within {timeout}s (the desk may be busy, or a long wait_for "
                              "outlived this client's timeout)") from None
        raise DeskUnreachable(f'{tool}: desk unreachable ({type(error).__name__})') from None
    if status != 200:
        raise DeskError(f'{tool}: desk answered HTTP {status}')
    try:
        reply = _payload(raw)
    except ValueError:
        raise DeskError(f'{tool}: desk sent an unreadable reply') from None
    if 'error' in reply:
        raise DeskError(f"{tool}: {_clean((reply['error'] or {}).get('message', 'error'), secrets)}")
    result = reply.get('result') or {}
    content = result.get('content') or []
    if result.get('isError'):
        text = content[0].get('text', '') if content else ''
        raise DeskError(f'{tool}: {_clean(text, secrets) or "tool error"}')
    if result.get('structuredContent') is not None:
        return result['structuredContent']
    try:
        return json.loads(content[0]['text'])
    except (IndexError, KeyError, TypeError, ValueError):
        raise DeskError(f'{tool}: desk sent no result') from None
