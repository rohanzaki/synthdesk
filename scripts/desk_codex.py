#!/usr/bin/env python3
"""Start Codex with a Project Desk session bound through an HTTP header."""
from __future__ import annotations

import ctypes
import json
import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import desk_launch
import desk_http
import desk_pocket
import codex_hooks

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MCP_NAME = 'project-desk'
MCP_NAME_ENV = 'PROJECT_DESK_MCP_NAME'
HEADER_NAME = 'X-Project-Desk-Session'
KEY_ENV = 'PROJECT_DESK_SESSION_KEY'
TOKEN_ENV = 'PROJECT_DESK_TOKEN'
SECRET_VARS = (TOKEN_ENV, KEY_ENV)
HANDOFF_AGE = 600
SESSION_ID_ENV = 'PROJECT_DESK_SESSION_ID'
SESSION_ENV = 'PROJECT_DESK_SESSION'
CLAUDE_SESSION_ENV = 'CLAUDE_CODE_SESSION_ID'
CLAUDE_PID_ENV = 'CLAUDE_PID'
CLAUDE_ENV_FILE_ENV = 'CLAUDE_ENV_FILE'
NO_SESSION_SUBCOMMANDS = {
    'apply', 'completion', 'doctor', 'features', 'login', 'logout', 'mcp', 'update',
}
OPTIONS_WITH_VALUES = {
    '-C', '--cd', '-a', '--ask-for-approval', '-c', '--config', '-i', '--image',
    '-m', '--model', '-p', '--profile', '-s', '--sandbox', '--disable', '--enable',
    '--remote',
}


class UnsafeCodexArgs(ValueError):
    pass


def _private_runtime(environ):
    given = environ.get('XDG_RUNTIME_DIR')
    if given:
        base = Path(given)
        if not base.is_absolute():
            raise UnsafeCodexArgs('desk codex: private runtime directory must be absolute.')
    else:
        account_home = desk_http._account_home()
        if not account_home:
            raise UnsafeCodexArgs('desk codex: private runtime directory is unavailable.')
        base = Path(account_home) / '.local/state/project-desk'
        try:
            created = desk_http._open_private_dir(base, create=True)
        except OSError:
            raise UnsafeCodexArgs('desk codex: private runtime directory is unavailable.') from None
        else:
            os.close(created)
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        for name in ('project-desk', 'broker') if given else ('broker',):
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise UnsafeCodexArgs('desk codex: private runtime directory is not owned and mode 0700.')
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise UnsafeCodexArgs('desk codex: private runtime directory is not owned and mode 0700.')
        return base / ('project-desk/broker' if given else 'broker')
    except UnsafeCodexArgs:
        raise
    except OSError:
        raise UnsafeCodexArgs('desk codex: private runtime directory is unavailable.') from None
    finally:
        if 'fd' in locals():
            os.close(fd)


def _sweep_handoffs(directory, now=None):
    now = time.time() if now is None else now
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for entry in os.scandir(fd):
            if not re.fullmatch(r'launch-[A-Za-z0-9_-]{3,80}\.json', entry.name):
                continue
            try:
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and now - info.st_mtime > HANDOFF_AGE):
                    os.unlink(entry.name, dir_fd=fd)
            except OSError:
                continue
    finally:
        os.close(fd)


def _write_handoff(environ, origin, project, token):
    if (desk_http.origin(origin) != origin or not token or not isinstance(token, str)
            or (project and not desk_pocket.SLUG.fullmatch(project))):
        raise UnsafeCodexArgs('desk codex: invalid handoff details.')
    directory = _private_runtime(environ)
    _sweep_handoffs(directory)
    name = 'launch-' + secrets.token_urlsafe(18) + '.json'
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'origin': origin, 'project': project, 'token': token}, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _hook_socket_path(environ):
    directory = _private_runtime(environ)
    path = directory / ('hook-' + secrets.token_urlsafe(6) + '.sock')
    if len(os.fsencode(path)) >= 108:
        raise UnsafeCodexArgs('desk codex: private runtime path is too long for the hook socket.')
    return path


def git_branch(cwd):
    """Return a safe 1-250 character Desk branch label for this worktree."""
    worktree = _git_root(cwd)
    try:
        marker = os.lstat(worktree / '.git')
        if not (stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode)):
            return 'no-git'
    except OSError:
        return 'no-git'
    git = shutil.which('git', path='/usr/bin:/bin')
    if not git:
        return 'no-git'
    env = {key: value for key, value in _subprocess_env(os.environ).items() if not key.startswith('GIT_')}
    env.update(PATH='/usr/bin:/bin', LC_ALL='C', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT='0',
               GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')

    def read(*args):
        try:
            out = subprocess.run([git, '-C', str(worktree), *args], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                 errors='replace', timeout=2, env=env, check=False)
            return out.stdout.strip() if out.returncode == 0 else ''
        except (OSError, subprocess.TimeoutExpired):
            return ''

    raw = read('symbolic-ref', '--quiet', '--short', 'HEAD')
    if raw:
        clean = re.sub(r'[^A-Za-z0-9._/-]+', '-', raw).strip('./-')
        if not clean:
            return 'no-git'
        if clean != raw or len(clean) > 250:
            digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
            clean = clean[:237].rstrip('./-') or 'branch'
            return f'{clean}-{digest}'
        return clean
    sha = read('rev-parse', '--verify', '--short=12', 'HEAD')
    return 'detached-' + sha if re.fullmatch(r'[0-9a-f]{7,40}', sha) else 'no-git'


def call_desk(base_url, tool, args, token='', timeout=6, component='codex-launch'):
    """One desk call through desk_http: no redirect is followed, a token never goes over plain http to a remote host
    (desk_loopback's rule), a loopback desk bypasses environment proxies, and the User-Agent names this launcher."""
    return desk_http.call(base_url, tool, args, token=token, timeout=timeout, component=component)


def _codex_config_home(environ):
    value = environ.get('CODEX_HOME')
    return Path(value).expanduser() if value else Path.home() / '.codex'


def _toml_file(path, place):
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise UnsafeCodexArgs(f'desk codex: cannot parse {place} ({path.name}); fix or simplify it.') from error
    try:
        return tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise UnsafeCodexArgs(f'desk codex: cannot parse {place} ({path.name}); fix or simplify it.') from error


def _codex_config(environ):
    return _toml_file(_codex_config_home(environ) / 'config.toml', 'user config')


def _coerce_excludes(exclude):
    if isinstance(exclude, str):
        exclude = [exclude]
    if not isinstance(exclude, list):
        return []
    return [item for item in exclude if isinstance(item, str) and item]


def _profiles_from_argv(argv):
    profiles = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            break
        if arg in ('-p', '--profile'):
            profiles.append(argv[index + 1] if index + 1 < len(argv) else '')
            index += 2
            continue
        if arg.startswith('--profile=') or arg.startswith('-p='):
            profiles.append(arg.split('=', 1)[1])
        elif arg.startswith('-p') and arg != '-p':
            profiles.append(arg[2:])
        elif arg in OPTIONS_WITH_VALUES:
            index += 2
            continue
        index += 1
    return profiles


def _shell_excludes(data):
    if not isinstance(data, dict):
        return []
    exclude = (data.get('shell_environment_policy') or {}).get('exclude') or []
    return _coerce_excludes(exclude)


def _profile_shell_excludes(environ, profile_name):
    if not profile_name:
        return []
    return _shell_excludes(_toml_file(_codex_config_home(environ) / f'{profile_name}.config.toml',
                                      'profile config'))


def _git_root(cwd):
    try:
        out = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=str(cwd), text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2,
                             env=_subprocess_env(os.environ), check=False)
    except (OSError, subprocess.SubprocessError):
        return cwd
    if out.returncode != 0:
        return cwd
    text = out.stdout.strip()
    return Path(text).resolve() if text else cwd


def _project_config_paths(cwd):
    cwd = cwd.resolve()
    root = _git_root(cwd).resolve()
    try:
        cwd.relative_to(root)
    except ValueError:
        root = cwd
    paths = []
    current = cwd
    while True:
        paths.append(current / '.codex' / 'config.toml')
        if current == root:
            break
        if current.parent == current:
            break
        current = current.parent
    return list(reversed(paths))


def _profile_candidates(environ, argv, cwd):
    """Inspect every selector; Codex may define profile flags at several command levels."""
    config = _codex_config(environ)
    selected = [config['profile']] if 'profile' in config else []
    for path in _project_config_paths(cwd):
        project = _toml_file(path, 'project config')
        if 'profile' in project:
            selected.append(project['profile'])
    selected.extend(_profiles_from_argv(argv))
    profiles = []
    for profile in selected:
        if not isinstance(profile, str) or not profile or '/' in profile or '\\' in profile or profile in ('.', '..'):
            raise UnsafeCodexArgs('desk codex: refusing invalid profile selector.')
        if profile not in profiles:
            profiles.append(profile)
    return profiles


def _active_profile(environ, argv, cwd):
    """Resolve Codex's effective profile: CLI, nearest project, then user."""
    profiles = _profile_candidates(environ, argv, cwd)
    return profiles[-1] if profiles else ''


def _inline_profile(data, profile, place):
    profiles = data.get('profiles') or {}
    if not isinstance(profiles, dict) or (profile in profiles and not isinstance(profiles[profile], dict)):
        raise UnsafeCodexArgs(f'desk codex: refusing invalid profile config in {place}.')
    return profiles.get(profile) or {}


def _validate_active_profile(data, name, place):
    _validate_mcp_layer(data, name, place, user_config=False)
    features = data.get('features') or {}
    if not isinstance(features, dict) or 'shell_snapshot' in features:
        raise UnsafeCodexArgs(f'desk codex: refusing shell_snapshot override in {place}.')


def _user_shell_excludes(environ, argv, cwd):
    data = _codex_config(environ)
    values = _shell_excludes(data)
    profile_name = _active_profile(environ, argv, cwd)
    if profile_name:
        values.extend(_profile_shell_excludes(environ, profile_name))
        values.extend(_shell_excludes(_inline_profile(data, profile_name, 'user config')))
    for path in _project_config_paths(cwd):
        project = _toml_file(path, 'project config')
        values.extend(_shell_excludes(project))
        if profile_name:
            values.extend(_shell_excludes(_inline_profile(project, profile_name, 'project config')))
    return values


def _normalized_key(value):
    text = value.strip()
    if '=' in text:
        text = text.split('=', 1)[0]
    text = ''.join(text.split()).replace('-', '_').lower()
    return '.'.join(part.strip('"\'') for part in text.split('.'))


def _unsafe_config_reason(value, name=DEFAULT_MCP_NAME):
    key = _normalized_key(value)
    if key == 'profile' or key == 'profiles' or key.startswith('profiles.'):
        return 'desk codex: refusing profile override in command line.'
    if key in ('features', 'features.shell_snapshot'):
        return 'desk codex: refusing shell_snapshot override (it would expose PROJECT_DESK_SESSION_KEY).'
    if key == 'shell_environment_policy' or key.startswith('shell_environment_policy.'):
        return 'desk codex: refusing shell_environment_policy override (it would expose PROJECT_DESK_SESSION_KEY).'
    server_key = 'mcp_servers.' + _normalized_key(name)
    if key == 'mcp_servers' or key == server_key or key.startswith(server_key + '.'):
        field = key[len(server_key) + 1:].split('.', 1)[0] if key.startswith(server_key + '.') else 'mcp_servers'
        return _desk_field_reason(field, 'command line')
    if key.startswith('mcp_servers.'):
        try:
            data = tomllib.loads(value)
        except tomllib.TOMLDecodeError:
            return 'desk codex: refusing invalid MCP server config in command line.'
        _validate_mcp_layer(data, name, 'command line', user_config=False)
    return ''


def _is_shell_snapshot_option(value):
    return any(part.strip().replace('-', '_').lower() == 'shell_snapshot'
               for part in value.split(','))


def _filter_unsafe_argv(argv, name=DEFAULT_MCP_NAME):
    filtered = []
    skip = False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == '--':
            filtered.extend(argv[index:])
            break
        if arg in ('-p', '--profile'):
            if index + 1 >= len(argv):
                raise UnsafeCodexArgs('desk codex: refusing invalid profile selector.')
            filtered.extend([arg, argv[index + 1]])
            skip = True
            continue
        if arg in ('-c', '--config') and index + 1 < len(argv):
            reason = _unsafe_config_reason(argv[index + 1], name)
            if reason:
                raise UnsafeCodexArgs(reason)
            filtered.extend([arg, argv[index + 1]])
            skip = True
            continue
        if arg.startswith('--config='):
            reason = _unsafe_config_reason(arg.split('=', 1)[1], name)
            if reason:
                raise UnsafeCodexArgs(reason)
        if arg.startswith('-c') and arg != '-c':
            reason = _unsafe_config_reason(arg[2:].removeprefix('='), name)
            if reason:
                raise UnsafeCodexArgs(reason)
        if arg in ('--enable', '--disable') and index + 1 < len(argv):
            if _is_shell_snapshot_option(argv[index + 1]):
                raise UnsafeCodexArgs(
                    'desk codex: refusing shell_snapshot override (it would expose PROJECT_DESK_SESSION_KEY).'
                )
            filtered.extend([arg, argv[index + 1]])
            skip = True
            continue
        if arg.startswith('--enable=') or arg.startswith('--disable='):
            if _is_shell_snapshot_option(arg.split('=', 1)[1]):
                raise UnsafeCodexArgs(
                    'desk codex: refusing shell_snapshot override (it would expose PROJECT_DESK_SESSION_KEY).'
                )
        filtered.append(arg)
    return filtered


def _exclude_config(environ, argv, cwd):
    values = []
    for item in [*_user_shell_excludes(environ, argv, cwd), *SECRET_VARS]:
        if item not in values:
            values.append(item)
    return 'shell_environment_policy.exclude=[' + ','.join(json.dumps(item) for item in values) + ']'


def _protect_process():
    """Disable Linux process inspection before preparing any launch state."""
    if sys.platform == 'linux':
        try:
            ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
        except (OSError, AttributeError):
            pass


def _subprocess_env(environ):
    """Keep Desk credentials out of every child, including preparatory git commands."""
    secret_parts = {'TOKEN', 'KEY', 'SECRET', 'PASSWORD', 'CODE'}
    return {key: value for key, value in environ.items()
            if key != 'CLAUDE_PLUGIN_OPTION_TOKEN'
            and not (key.startswith('PROJECT_DESK_') and secret_parts.intersection(key.split('_')))}


def _safe_env(environ):
    env = dict(environ)
    for key in (CLAUDE_SESSION_ENV, CLAUDE_PID_ENV, CLAUDE_ENV_FILE_ENV,
                SESSION_ENV, KEY_ENV, SESSION_ID_ENV, 'PROJECT_DESK_BROKER_SOCKET'):
        env.pop(key, None)
    return env


def _cd_candidates_from_argv(argv):
    candidates = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            break
        if arg in ('-C', '--cd'):
            if index + 1 >= len(argv):
                raise UnsafeCodexArgs('desk codex: refusing invalid directory selector.')
            value = argv[index + 1]
            index += 2
        elif arg.startswith('--cd=') or arg.startswith('-C='):
            value = arg.split('=', 1)[1]
            index += 1
        elif arg.startswith('-C') and arg != '-C':
            value = arg[2:]
            index += 1
        elif arg in OPTIONS_WITH_VALUES:
            index += 2
            continue
        else:
            index += 1
            continue
        if not value:
            raise UnsafeCodexArgs('desk codex: refusing invalid directory selector.')
        path = Path(value)
        candidates.append((Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve())
    return candidates or [Path.cwd().resolve()]


def _cd_from_argv(argv):
    return _cd_candidates_from_argv(argv)[-1]


def _subcommand(argv):
    return _subcommand_at(argv)[0]


def _subcommand_at(argv):
    """Return (subcommand, index of that token in argv); ('', -1) when there is none."""
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--':
            return '', -1
        if arg in OPTIONS_WITH_VALUES:
            index += 2
            continue
        if arg.startswith('--profile=') or arg.startswith('--config='):
            index += 1
            continue
        if arg.startswith('--cd='):
            index += 1
            continue
        if arg.startswith('-'):
            index += 1
            continue
        return arg, index
    return '', -1


def _mcp_name(environ):
    name = (environ.get(MCP_NAME_ENV) or DEFAULT_MCP_NAME).strip()
    if not name or any(part in name for part in ('.', '=', '"', "'", '[', ']')):
        raise ValueError('invalid MCP server name')
    return name


def _mcp_url(base_url):
    value = base_url.rstrip('/')
    return value if value.endswith('/mcp') else value + '/mcp'


def _references_secret(value):
    if isinstance(value, str):
        return any(variable in value for variable in SECRET_VARS)
    if isinstance(value, dict):
        return any(_references_secret(k) or _references_secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_references_secret(item) for item in value)
    return False


def _safe_field_label(field):
    return field if isinstance(field, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', field) else 'unknown'


def _desk_field_reason(field, place):
    return f'desk codex: refusing desk server field {_safe_field_label(field)} in {place}; remove that field.'


def _validate_mcp_layer(data, name, place, *, user_config):
    servers = data.get('mcp_servers') or {}
    if not isinstance(servers, dict):
        raise UnsafeCodexArgs(f'desk codex: refusing invalid MCP server config in {place}.')
    for server_name, server in servers.items():
        if not isinstance(server, dict):
            raise UnsafeCodexArgs(f'desk codex: refusing invalid MCP server config in {place}.')
        if server_name == name:
            forbidden = ('bearer_token_env_var', 'http_headers', 'env_http_headers',
                         'http_headers_helper', 'auth', 'oauth', 'command', 'args',
                         'env', 'env_vars', 'headers')
            if place == 'command line':
                field = next((item for item in forbidden if item in server), next(iter(server), 'mcp_servers'))
                raise UnsafeCodexArgs(_desk_field_reason(field, place))
            if not user_config:
                field = next((item for item in forbidden if item in server), next(iter(server), 'mcp_servers'))
                raise UnsafeCodexArgs(_desk_field_reason(field, place))
            for field in forbidden:
                if field in server:
                    raise UnsafeCodexArgs(_desk_field_reason(field, place))
        elif _references_secret(server_name) or _references_secret(server):
            field = next((item for item, value in server.items()
                          if _references_secret(item) or _references_secret(value)), 'server_name')
            raise UnsafeCodexArgs(
                f'desk codex: refusing secret reference in other server field {_safe_field_label(field)} in {place}.')


def _validate_config_layers(environ, argv, cwd, name):
    config = _codex_config(environ)
    _validate_mcp_layer(config, name, 'user config', user_config=False)
    server = (config.get('mcp_servers') or {}).get(name) or {}
    if 'url' in server:
        url = server['url']
        configured_origin = desk_http.origin(url) if isinstance(url, str) else None
        if not configured_origin or configured_origin != desk_http.origin(environ['PROJECT_DESK_URL']):
            if isinstance(url, str):
                try:
                    host = urlsplit(url).hostname or 'an invalid address'
                except (TypeError, ValueError):
                    host = 'an invalid address'
            else:
                host = 'an invalid address'
            host = _safe_reason(host)
            raise UnsafeCodexArgs(f"Codex's {name} entry points to {host}, not this desk. Nothing was sent.")
    profiles = _profile_candidates(environ, argv, cwd)
    for profile in profiles:
        _validate_active_profile(_inline_profile(config, profile, 'user config'), name, 'profile config')
        _validate_active_profile(_toml_file(_codex_config_home(environ) / f'{profile}.config.toml',
                                            'profile config'),
                                 name, 'profile config')
    for path in _project_config_paths(cwd):
        project = _toml_file(path, 'project config')
        _validate_mcp_layer(project, name, 'project config', user_config=False)
        for profile in profiles:
            _validate_active_profile(_inline_profile(project, profile, 'project config'),
                                     name, 'project profile config')


def _launcher_configs(environ, argv, *, project='', cwd=None, handoff=None, hook_socket=None,
                      test_home=None, test_mode=False):
    name = _mcp_name(environ)
    filtered_argv = _filter_unsafe_argv(argv, name)
    directories = _cd_candidates_from_argv(filtered_argv)
    for cwd in directories:
        _validate_config_layers(environ, filtered_argv, cwd, name)
    cwd = directories[-1] if cwd is None else cwd
    origin = desk_http.origin(environ['PROJECT_DESK_URL'])
    if not origin:
        raise UnsafeCodexArgs('desk codex: refusing invalid Desk origin.')
    broker_args = ['-I', str(ROOT / 'scripts' / 'desk_mcp_proxy.py'),
                   '--origin', origin, '--project', project, '--worktree', str(cwd),
                   '--branch', git_branch(cwd)]
    if handoff is not None:
        broker_args.extend(['--handoff', str(handoff)])
    if hook_socket is not None:
        broker_args.extend(['--hook-socket', str(hook_socket)])
    if test_home is not None:
        broker_args.extend(['--test-home', str(test_home)])
    if test_mode:
        broker_args.append('--test-mode')
    values = ['features.shell_snapshot=false']
    values.append(f'mcp_servers.{name}.command={json.dumps(sys.executable)}')
    values.append(f'mcp_servers.{name}.args={json.dumps(broker_args)}')
    values.append(f'mcp_servers.{name}.default_tools_approval_mode="approve"')
    values.append(_exclude_config(environ, filtered_argv, cwd))
    return values, filtered_argv


CONFIG_AFTER_SUBCOMMANDS = {'exec', 'review', 'resume'}


def _codex_argv(configs, argv):
    name, at = _subcommand_at(argv)
    flat = []
    for config in configs:
        flat.extend(['-c', config])
    if at < 0 or name not in CONFIG_AFTER_SUBCOMMANDS:
        return ['codex', *flat, *argv]
    return ['codex', *argv[:at + 1], *flat, *argv[at + 1:]]


def _register(environ, call, cwd, selected_project=''):
    branch = git_branch(cwd)
    declared_project = _declared_project(cwd)
    if declared_project and selected_project and declared_project != selected_project:
        raise UnsafeCodexArgs('desk codex: selected project does not match the worktree declaration.')
    project = declared_project or selected_project
    args = {'agent': 'codex', 'worktree': str(cwd), 'branch': branch,
            'name': f'codex {cwd.name} {branch}', 'project': project}
    if codex_hooks.hooks_installed(_codex_config_home(environ) / 'hooks.json'):
        args['hooked'] = True
    try:
        result = call(environ['PROJECT_DESK_URL'], 'register_session', args,
                      token=environ.get('PROJECT_DESK_TOKEN', ''), timeout=6, component='codex-launch')
    except Exception as error:
        if project and 'belongs to project' in str(error):
            raise UnsafeCodexArgs('desk codex: Registration rejected; Codex was not launched.') from error
        raise
    if project and result.get('project') != project:
        session_key = result.get('session_key')
        if not isinstance(session_key, str) or not session_key:
            raise UnsafeCodexArgs('desk codex: registration project mismatch; session cleanup could not be '
                                  'confirmed. Codex was not launched.')
        try:
            call(environ['PROJECT_DESK_URL'], 'end_session',
                 {'reason': 'launcher project mismatch', 'session_key': session_key},
                 token=environ.get('PROJECT_DESK_TOKEN', ''), timeout=6, component='codex-launch')
        except Exception:
            raise UnsafeCodexArgs('desk codex: registration project mismatch; session cleanup could not be '
                                  'confirmed. Codex was not launched.') from None
        raise UnsafeCodexArgs('desk codex: registration project mismatch; new session ended. '
                              'Codex was not launched.')
    return result


def _declared_project(cwd):
    try:
        raw = desk_pocket._declaration_bytes(_git_root(cwd))
        if raw is None:
            return ''
        data = json.loads(raw)
    except desk_pocket.Refused as error:
        try:
            folder_is_foreign = os.lstat(_git_root(cwd)).st_uid != os.getuid()
        except OSError:
            folder_is_foreign = False
        if folder_is_foreign:
            raise UnsafeCodexArgs('desk codex: this folder is not owned by you; '
                                  'Project Desk cannot trust its declaration.') from error
        raise UnsafeCodexArgs('desk codex: cannot read the worktree declaration.') from error
    except (OSError, UnicodeError, ValueError) as error:
        raise UnsafeCodexArgs('desk codex: cannot read the worktree declaration.') from error
    project = data.get('project') if isinstance(data, dict) else None
    if not isinstance(project, str) or not desk_pocket.SLUG.fullmatch(project):
        raise UnsafeCodexArgs('desk codex: invalid project in .project-desk.json.')
    return project


def _optional_pin(source_env):
    """A missing pin allows an explicitly paired token or a local loopback desk."""
    try:
        return desk_pocket._pin(None, source_env)
    except desk_pocket.Refused as error:
        if str(error) == desk_pocket.NOT_SETUP:
            return ''
        raise UnsafeCodexArgs(f'desk codex: {error}') from error
    except (OSError, ValueError) as error:
        raise UnsafeCodexArgs(f'desk codex: {error}') from error


def _broker_credential_path():
    """Match the broker's account-home store, ignoring path overrides."""
    account_home = desk_http._account_home()
    if not account_home:
        raise UnsafeCodexArgs('desk codex: account home is unavailable.')
    return Path(account_home) / '.local/state/project-desk/credentials.json'


def _joined_desk(source_env, cwd):
    """Choose only a token bound to the PC pin and the selected project."""
    origin = _optional_pin(source_env)
    base_url = desk_pocket._url(origin) if origin else desk_launch.desk_url(source_env)
    env_token = desk_http.env_token_for(base_url, source_env)
    if env_token:
        return base_url, env_token, '', ''
    if not origin:
        return base_url, '', '', ''
    project = _declared_project(cwd)
    path = _broker_credential_path()
    announcement = ''
    if not project:
        joined = desk_http.joined_projects(origin, path=path)
        if len(joined) > 1:
            raise UnsafeCodexArgs('desk codex: multiple projects are joined here; add .project-desk.json '
                                  'with the project before launching. Nothing was sent.')
        if len(joined) == 1:
            if not desk_pocket.SLUG.fullmatch(joined[0]):
                raise UnsafeCodexArgs('desk codex: saved project name is invalid; add .project-desk.json. '
                                      'Nothing was sent.')
            project = joined[0]
            announcement = f'desk codex: using sole joined project {project}; add .project-desk.json to pin it.'
    token = desk_http.token_for(origin, source_env, path=path, project=project)
    return base_url, token, announcement, project


def _desk_for_launch(source_env, cwd):
    """Pair an explicit environment token with its own URL; otherwise use the pinned PC origin."""
    if not source_env.get(TOKEN_ENV):
        return _joined_desk(source_env, cwd)
    origin = _optional_pin(source_env)
    base_url = desk_pocket._url(origin) if origin else desk_launch.desk_url(source_env)
    token = desk_http.env_token_for(base_url, source_env)
    if not token:
        raise UnsafeCodexArgs('desk codex: PROJECT_DESK_TOKEN has no matching PROJECT_DESK_URL. Nothing was sent.')
    return base_url, token, '', ''


def _safe_reason(error, secrets=()):
    text = ' '.join(str(error).split())
    for secret in sorted((value for value in secrets if isinstance(value, str) and value), key=len, reverse=True):
        for fragment in (secret, secret[:20], secret[-20:]) if len(secret) >= 24 else (secret,):
            text = text.replace(fragment, '[redacted]')
    text = re.sub(r'pd[jpr]_[A-Za-z0-9_-]+', '[redacted]', text)
    text = re.sub(r'[A-Za-z0-9_-]{43,}', '[redacted]', text)
    return text[:160] or type(error).__name__


def _exec_codex(argv, env, execvp):
    try:
        execvp('codex', argv, _subprocess_env(env))
    except FileNotFoundError:
        print('codex not found on PATH.', file=sys.stderr)
        return 127
    return 0


def _exec_plain_codex(argv, env, execvp):
    env = dict(env)
    env.pop(TOKEN_ENV, None)
    env.pop(KEY_ENV, None)
    env.pop('PROJECT_DESK_BROKER_SOCKET', None)
    env.pop('CLAUDE_PLUGIN_OPTION_TOKEN', None)
    return _exec_codex(['codex', *argv], env, execvp)


def _spawn_handoff_watchdog(path):
    """An independent 15-second cleanup path if Codex never starts the broker."""
    return subprocess.Popen([sys.executable, '-I', str(ROOT / 'scripts' / 'desk_handoff_watchdog.py'),
                             str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            env={}, start_new_session=True, close_fds=True)


def _remove_unconsumed_handoff(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    print('desk codex: desk connection failed; broker did not start.', file=sys.stderr)
    return True


def _exec_with_handoff(argv, env, handoff, execvp):
    """Keep a parent alive to remove an unconsumed handoff when Codex exits."""
    _protect_process()
    os.environ.pop(TOKEN_ENV, None)
    os.environ.pop(KEY_ENV, None)
    try:
        try:
            _spawn_handoff_watchdog(handoff)
        except OSError:
            print('desk codex: desk connection failed; cleanup watchdog could not start.', file=sys.stderr)
            return 2
        if execvp is not os.execvpe:
            return _exec_codex(argv, env, execvp)
        try:
            return subprocess.run(argv, env=_subprocess_env(env), check=False).returncode
        except FileNotFoundError:
            print('codex not found on PATH.', file=sys.stderr)
            return 127
    finally:
        _remove_unconsumed_handoff(handoff)


def main(argv=None, environ=None, call=call_desk, execvp=os.execvpe):
    _protect_process()
    argv = list(sys.argv[1:] if argv is None else argv)
    source_env = os.environ if environ is None else environ
    env = _safe_env(desk_launch.hook_env(source_env))
    env.pop('CLAUDE_PLUGIN_OPTION_TOKEN', None)
    if _subcommand(argv) in NO_SESSION_SUBCOMMANDS:
        return _exec_plain_codex(argv, env, execvp)
    handoff = None
    try:
        name = _mcp_name(env)
        filtered = _filter_unsafe_argv(argv, name)
        cwd = _cd_from_argv(filtered)
        base_url, token, announcement, selected_project = _desk_for_launch(source_env, cwd)
        env['PROJECT_DESK_URL'] = base_url
        project = _declared_project(cwd) or selected_project
        if selected_project and project != selected_project:
            raise UnsafeCodexArgs('desk codex: selected project does not match the worktree declaration.')
        _launcher_configs(env, argv, project=project, cwd=cwd)
        test_home = source_env.get('PROJECT_DESK_TEST_HOME')
        if test_home and (source_env.get('PROJECT_DESK_TEST_MODE') != '1'
                          or not desk_http._loopback(base_url)
                          or not desk_http.env_token_for(base_url, source_env)):
            raise UnsafeCodexArgs('desk codex: test account home requires an explicit local token pair.')
        hook_socket = _hook_socket_path(source_env)
        if token and desk_http.env_token_for(base_url, source_env):
            handoff = _write_handoff(source_env, desk_http.origin(base_url), project, token)
        configs, user_argv = _launcher_configs(env, argv, project=project, cwd=cwd,
                                               handoff=handoff, hook_socket=hook_socket,
                                               test_home=test_home, test_mode=bool(test_home))
    except UnsafeCodexArgs as error:
        if handoff is not None:
            Path(handoff).unlink(missing_ok=True)
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        if handoff is not None:
            Path(handoff).unlink(missing_ok=True)
        print(f'Project Desk unavailable ({_safe_reason(error, (env.get(TOKEN_ENV), env.get(KEY_ENV)))}); '
              'starting plain codex.', file=sys.stderr)
        return _exec_plain_codex(argv, env, execvp)
    if not token and not desk_http._loopback(base_url):
        print('Project Desk token unavailable; starting plain codex.', file=sys.stderr)
        return _exec_plain_codex(argv, env, execvp)
    env.pop(TOKEN_ENV, None)
    env.pop(KEY_ENV, None)
    env.pop('PROJECT_DESK_CREDENTIALS', None)
    env['PROJECT_DESK_AUTO_REGISTER'] = '0'
    env['PROJECT_DESK_BROKER_SOCKET'] = str(hook_socket)
    if announcement:
        print(announcement, file=sys.stderr)
    command = _codex_argv(configs, user_argv)
    if handoff is not None:
        return _exec_with_handoff(command, env, handoff, execvp)
    return _exec_codex(command, env, execvp)


if __name__ == '__main__':
    sys.exit(main())
