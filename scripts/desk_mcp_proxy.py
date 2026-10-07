#!/usr/bin/env python3
"""Private stdio MCP bridge for ``desk codex``.

Codex gets only a command and non-secret arguments. This process reads the
account-owned credential cache, registers once, and holds its session key in
memory while forwarding MCP messages to the pinned Desk origin.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import pwd
import re
import socket
import stat
import struct
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import desk_http
import desk_pocket
import codex_hooks

MAX_LINE = 1024 * 1024
MAX_REPLY = 4 * 1024 * 1024
BRANCH = re.compile(r'[A-Za-z0-9._/-]{1,250}\Z')
HOOK_TOOLS = frozenset({'check_in', 'would_conflict', 'auto_claim', 'end_session', 'role'})


class BrokerRefused(Exception):
    pass


class HookServer:
    """Same-uid hook bridge; only hook operations, never a credential response."""

    def __init__(self, path, origin, token, session_key):
        self.path = Path(path)
        self.origin = origin
        self.token = token
        self.session_key = session_key
        self.socket = None
        self.thread = None
        self.stop = threading.Event()

    def start(self):
        if (not self.path.is_absolute()
                or not re.fullmatch(r'hook-[A-Za-z0-9_-]{3,80}\.sock', self.path.name)):
            raise BrokerRefused('Invalid hook socket path.')
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise BrokerRefused('Hook socket directory is not private.')
        finally:
            os.close(directory)
        self.socket = socket.socket(socket.AF_UNIX)
        try:
            self.socket.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.socket.listen(8)
            self.socket.settimeout(0.2)
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()
        except Exception:
            self.socket.close()
            self.path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _peer_uid(conn):
        return struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(6)
                    if self._peer_uid(conn) != os.getuid():
                        raise BrokerRefused('refused')
                    chunks = bytearray()
                    while b'\n' not in chunks and len(chunks) <= 65536:
                        part = conn.recv(4096)
                        if not part:
                            break
                        chunks.extend(part)
                    if len(chunks) > 65536 or b'\n' not in chunks:
                        raise BrokerRefused('refused')
                    request = json.loads(chunks.split(b'\n', 1)[0])
                    tool = request.get('tool') if isinstance(request, dict) else None
                    args = request.get('args') if isinstance(request, dict) else None
                    if tool not in HOOK_TOOLS or not isinstance(args, dict):
                        raise BrokerRefused('refused')
                    clean_args = {k: v for k, v in args.items() if k != 'session_key'}
                    try:
                        reply = desk_http.call(desk_pocket._url(self.origin), tool,
                                               dict(clean_args, session_key=self.session_key),
                                               token=self.token, timeout=6, component='codex-broker-hook')
                    except desk_http.DeskError as error:
                        if 'unknown tool' not in str(error).lower():
                            raise
                        reply = desk_http.call(desk_pocket._url(self.origin), 'desk',
                                               {'session_key': self.session_key, 'tool': tool,
                                                'args': clean_args}, token=self.token,
                                               timeout=6, component='codex-broker-hook')
                    output = _safe_reply({'result': reply}, self.token, self.session_key)
                except (BrokerRefused, OSError, ValueError, TypeError, desk_http.DeskError):
                    output = '{"error":"refused"}'
                try:
                    conn.sendall(output.encode() + b'\n')
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        if self.socket is not None:
            self.socket.close()
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.path.unlink(missing_ok=True)


def _credential_path():
    """Use the same account-home file as ``desk join``, ignoring path overrides."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/project-desk/credentials.json'


def _args(argv):
    parser = argparse.ArgumentParser(prog='desk mcp-proxy')
    parser.add_argument('--origin', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--worktree', required=True)
    parser.add_argument('--branch', required=True)
    parser.add_argument('--handoff')
    parser.add_argument('--hook-socket')
    parser.add_argument('--test-home')
    parser.add_argument('--test-mode', action='store_true')
    return parser.parse_args(argv)


def _take_handoff(path, origin, project):
    """Read a private one-use token and unlink it before the first network call."""
    path = Path(path)
    if (not path.is_absolute() or not re.fullmatch(r'launch-[A-Za-z0-9_-]{3,80}\.json', path.name)):
        raise BrokerRefused('Invalid handoff path.')
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BrokerRefused('Handoff directory is not private.')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory)
        try:
            file_info = os.fstat(fd)
            if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.getuid()
                    or stat.S_IMODE(file_info.st_mode) != 0o600 or file_info.st_size > 4096):
                raise BrokerRefused('Handoff file is not private.')
            raw = os.read(fd, 4097)
            if len(raw) > 4096:
                raise BrokerRefused('Handoff file is too large.')
            os.unlink(path.name, dir_fd=directory)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError):
        raise BrokerRefused('Invalid handoff.') from None
    if (not isinstance(value, dict) or value.get('origin') != origin
            or value.get('project') != project or not isinstance(value.get('token'), str)
            or not value['token'].startswith('pdr_')):
        raise BrokerRefused('Handoff does not match this Desk and project.')
    return value['token']


def _origin_and_project(options, environ):
    origin = desk_http.origin(options.origin)
    if not origin or options.origin != origin or not desk_http._secure_for_token(origin):
        raise BrokerRefused('Invalid Desk origin.')
    pin_home = None
    if options.test_mode and not options.test_home:
        raise BrokerRefused('Test account home is not allowed.')
    if options.test_home:
        home = Path(options.test_home)
        try:
            info = home.lstat()
        except OSError:
            raise BrokerRefused('Invalid test account home.') from None
        if (not options.test_mode or not options.handoff
                or not desk_http._loopback(origin) or not home.is_absolute()
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise BrokerRefused('Test account home is not allowed.')
        pin_home = home
    try:
        pinned = desk_pocket._pin(pin_home, environ)
    except desk_pocket.Refused as error:
        if str(error) != desk_pocket.NOT_SETUP or (not options.handoff and not desk_http._loopback(origin)):
            raise BrokerRefused('Desk origin is not pinned on this computer.') from None
        pinned = ''
    if pinned and pinned != origin:
        raise BrokerRefused('Desk origin differs from this computer\'s pin.')
    if options.project and not desk_pocket.SLUG.fullmatch(options.project):
        raise BrokerRefused('Invalid project.')
    worktree = Path(options.worktree)
    if not worktree.is_absolute() or not worktree.is_dir() or not BRANCH.fullmatch(options.branch):
        raise BrokerRefused('Invalid worktree or branch.')
    try:
        raw = desk_pocket._declaration_bytes(desk_pocket._repo_root(worktree))
        declaration = json.loads(raw) if raw is not None else {}
    except (desk_pocket.Refused, OSError, ValueError):
        raise BrokerRefused('Cannot trust the worktree declaration.') from None
    declared = declaration.get('project') if isinstance(declaration, dict) else None
    if raw is not None and declared != options.project:
        raise BrokerRefused('Project differs from the worktree declaration.')
    return origin, options.project, worktree


def _forward(base_url, rpc, token, session_key):
    """One MCP JSON-RPC message over the Desk's no-redirect HTTP transport."""
    if token and not desk_http._secure_for_token(base_url):
        raise BrokerRefused('Unsafe Desk transport.')
    headers = {'Content-Type': 'application/json',
               'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': desk_http.PROTOCOL_VERSION,
               'User-Agent': desk_http.user_agent('codex-broker'),
               'X-Project-Desk-Session': session_key}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(base_url.rstrip('/') + '/mcp',
                                     data=json.dumps(rpc).encode(), headers=headers,
                                     method='POST')
    with desk_http._open(request, 60) as response:
        if response.status not in (200, 202):
            raise OSError('Desk refused the MCP request.')
        body = response.read(MAX_REPLY + 1)
        if len(body) > MAX_REPLY:
            raise OSError('Desk reply too large.')
        if response.status == 202 or not body:
            return None
        return desk_http._payload(body)


def _safe_reply(reply, token, session_key):
    """Remove the registration tool and any accidentally echoed credential."""
    if isinstance(reply, dict):
        result = reply.get('result')
        tools = result.get('tools') if isinstance(result, dict) else None
        if isinstance(tools, list):
            reply = dict(reply)
            result = dict(reply['result'])
            result['tools'] = [tool for tool in tools if not isinstance(tool, dict)
                               or tool.get('name') != 'register_session']
            reply['result'] = result
    raw = json.dumps(reply, separators=(',', ':'))
    for secret in (session_key, token):
        if secret:
            for fragment in ((secret, secret[:20], secret[-20:]) if len(secret) >= 24 else (secret,)):
                raw = raw.replace(fragment, '[redacted]')
    return raw


def _error_reply(id_value, message):
    return {'jsonrpc': '2.0', 'id': id_value,
            'error': {'code': -32000, 'message': message}}


def run(argv, *, environ=None, input_stream=None, output_stream=None):
    if sys.platform == 'linux':
        try:
            ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
        except (OSError, AttributeError):
            pass
    environ = dict(os.environ if environ is None else environ)
    source = sys.stdin if input_stream is None else input_stream
    target = sys.stdout if output_stream is None else output_stream
    options = _args(argv)
    hook_server = None
    try:
        token = _take_handoff(options.handoff, options.origin, options.project) if options.handoff else ''
        origin, project, worktree = _origin_and_project(options, environ)
        base_url = desk_pocket._url(origin)
        if not options.handoff:
            token = desk_http.token_for(origin, {}, path=_credential_path(), project=project)
        if not token and not desk_http._loopback(origin):
            raise BrokerRefused('No joined token for this project and origin.')
        if options.hook_socket:
            hook_server = HookServer(options.hook_socket, origin, token, '')
            hook_server.start()
        registration = {'agent': 'codex', 'worktree': str(worktree),
                        'branch': options.branch,
                        'name': f'codex {worktree.name} {options.branch}',
                        'project': project}
        codex_home = Path(environ.get('CODEX_HOME') or
                          Path(pwd.getpwuid(os.getuid()).pw_dir) / '.codex')
        if hook_server is not None and codex_hooks.hooks_installed(codex_home / 'hooks.json'):
            registration['hooked'] = True
        result = desk_http.call(base_url, 'register_session', registration, token=token, timeout=6,
                                component='codex-broker')
        if not isinstance(result, dict):
            raise BrokerRefused('Malformed Desk registration reply.')
        session_key = result.get('session_key')
        if (project and result.get('project') != project) or not isinstance(session_key, str) or not session_key:
            if isinstance(session_key, str) and session_key:
                try:
                    desk_http.call(base_url, 'end_session',
                                   {'reason': 'broker project mismatch', 'session_key': session_key},
                                   token=token, timeout=6, component='codex-broker')
                except Exception:
                    pass
            raise BrokerRefused('Desk registration did not match this project.')
        if hook_server is not None:
            hook_server.session_key = session_key
    except (BrokerRefused, desk_pocket.Refused, OSError, ValueError, desk_http.DeskError):
        if hook_server is not None:
            hook_server.close()
        print('desk mcp-proxy: could not establish the pinned Desk session.', file=sys.stderr)
        return 2

    try:
        for line in source:
            if len(line) > MAX_LINE:
                print('desk mcp-proxy: MCP input too large.', file=sys.stderr)
                return 2
            try:
                rpc = json.loads(line)
                if not isinstance(rpc, dict) or not isinstance(rpc.get('method'), str):
                    raise ValueError('bad MCP message')
            except ValueError:
                print('desk mcp-proxy: invalid MCP input.', file=sys.stderr)
                return 2
            request_id = rpc.get('id')
            method = rpc['method']
            params = rpc.get('params')
            if method == 'tools/call' and isinstance(params, dict):
                given = params.get('arguments')
                nested_registration = (params.get('name') == 'desk' and isinstance(given, dict)
                                       and given.get('tool') == 'register_session')
                if params.get('name') == 'register_session' or nested_registration:
                    reply = _error_reply(request_id, 'The launcher already registered this session.')
                    target.write(_safe_reply(reply, token, session_key) + '\n')
                    target.flush()
                    continue
                args = params.get('arguments')
                if not isinstance(args, dict):
                    args = {}
                rpc = dict(rpc)
                rpc['params'] = dict(params, arguments=dict(args, session_key=session_key))
            try:
                reply = _forward(base_url, rpc, token, session_key)
            except (OSError, ValueError, urllib.error.URLError, desk_http.DeskError):
                reply = _error_reply(request_id, 'Project Desk is unavailable; restart desk codex.')
            if request_id is not None:
                target.write(_safe_reply(reply or _error_reply(request_id, 'Project Desk sent no reply.'),
                                         token, session_key) + '\n')
                target.flush()
    finally:
        if hook_server is not None:
            hook_server.close()
            try:
                desk_http.call(base_url, 'end_session',
                               {'session_key': session_key, 'reason': 'Codex broker closed'},
                               token=token, timeout=3, component='codex-broker')
            except (OSError, ValueError, desk_http.DeskError):
                pass
    return 0


def main(argv=None):
    return run(sys.argv[1:] if argv is None else argv)


if __name__ == '__main__':
    raise SystemExit(main())
