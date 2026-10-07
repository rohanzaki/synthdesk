"""Fallback CLI for existing sessions whose MCP catalog has not reloaded,
plus `desk join`, which connects a repo to a Project Desk project."""
import argparse
import asyncio
import json
import os
import re
import stat
import sys
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from onboarding import ANTIGRAVITY_RULE, BEGIN, END, agents_block, claude_block

DEFAULT_DESK = os.environ.get('PROJECT_DESK_URL', 'http://127.0.0.1:7331').rstrip('/')
LINK = re.compile(r'(https?://[^/\s]+)/p/([a-z0-9][a-z0-9-]*)(?:/onboard)?/?')


def upsert_block(path, block):
    """Insert or replace the marked Project Desk block. Returns True when the file changed."""
    path = Path(path)
    original = ''
    if path.exists():
        with open(path, newline='') as stream:
            original = stream.read()
    newline = '\r\n' if '\r\n' in original else '\n'
    body = block.replace('\r\n', '\n').strip('\n')
    if not (body.startswith(BEGIN) and body.endswith(END)):
        raise ValueError('Block must start and end with the Project Desk markers')
    body = body.replace('\n', newline)
    start = original.find(BEGIN)
    if start != -1:
        end = original.find(END, start)
        if end == -1:
            raise ValueError(f'{path.name} has a Project Desk begin marker without an end marker; fix it by hand')
        updated = original[:start] + body + original[end + len(END):]
    elif original.strip():
        updated = original.rstrip('\r\n') + newline + newline + body + newline
    else:
        updated = body + newline
    if updated == original:
        return False
    with open(path, 'w', newline='') as stream:
        stream.write(updated)
    return True


def user_agent(component='cli'):
    """`project-desk/<version> (<component>)`; some proxies block the default Python-urllib agent.
    Standard library only: this file runs on a plain python3."""
    try:
        version = json.loads((Path(__file__).resolve().parent / '.claude-plugin/plugin.json').read_text()).get('version', '0')
    except (OSError, ValueError):
        version = '0'
    return f'project-desk/{version} ({component})'


def fetch_text(url, timeout=10):
    request = urllib.request.Request(url, headers={'User-Agent': user_agent('cli')})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8')


def machine_status(desk, home=None):
    """One-time setup steps still missing on this computer (never changed here)."""
    home = Path(home or Path.home())
    root = Path(__file__).resolve().parent

    def read(relative):
        try:
            return (home / relative).read_text()
        except OSError:
            return ''
    try:
        claude_servers = json.loads(read('.claude.json') or '{}').get('mcpServers', {})
    except ValueError:
        claude_servers = {}
    missing = []
    if 'project-desk' not in claude_servers:
        missing.append({'what': 'Claude Code MCP server',
                        'run': f'claude mcp add --transport http --scope user project-desk {desk}/mcp'})
    if '[mcp_servers.project-desk]' not in read('.codex/config.toml'):
        missing.append({'what': 'Codex MCP server', 'add_to': str(home / '.codex/config.toml'),
                        'text': f'[mcp_servers.project-desk]\nurl = "{desk}/mcp"\n'})
    if 'codex_hooks.py' not in read('.claude/settings.json'):
        missing.append({'what': 'Claude hooks', 'run': f'python3 {root}/codex_hooks.py install --agent claude'})
    if 'codex_hooks.py' not in read('.codex/hooks.json'):
        missing.append({'what': 'Codex hooks', 'run': f'python3 {root}/codex_hooks.py install --agent codex'})
    if (home / '.gemini' / 'antigravity').is_dir():
        try:
            antigravity = json.loads(read('.gemini/antigravity/mcp_config.json') or '{}').get('mcpServers') or {}
        except ValueError:
            antigravity = {}
        if 'project-desk' not in antigravity:
            missing.append({'what': 'Antigravity MCP server',
                            'add_to': str(home / '.gemini/antigravity/mcp_config.json'),
                            'text': f'"project-desk": {{"serverUrl": "{desk}/mcp"}}  '
                                    '(inside "mcpServers"; keep the entries already there)'})
    return missing


def uses_gemini(repo, home=None):
    """Write Gemini's files only where Gemini works: it is on this computer, or the repo already
    has Gemini or Antigravity files. A Claude-only repo gets nothing new."""
    home = Path(home or Path.home())
    return ((home / '.gemini').is_dir() or (repo / 'GEMINI.md').exists()
            or (repo / '.agents').is_dir() or (repo / '.agent').is_dir())


def upsert_rule(path, text):
    """Create the Antigravity rule file whole (front matter + block), or refresh only its block."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return True
    return upsert_block(path, text[text.index(BEGIN):])


def join(link, repo='.', fetch=fetch_text, home=None):
    match = LINK.fullmatch(str(link).strip())
    if not match:
        raise ValueError('Expected a link like http://127.0.0.1:7331/p/<project>')
    desk, slug = match.group(1), match.group(2)
    base = f'{desk}/p/{slug}'
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError(f'{repo} is not a folder')
    declaration = fetch(f'{base}/files/.project-desk.json')
    if json.loads(declaration).get('project') != slug:
        raise ValueError('The desk returned a declaration for a different project')
    target = repo / '.project-desk.json'
    if target.exists():
        try:
            existing = json.loads(target.read_text()).get('project')
        except ValueError:
            existing = None
        if existing and existing != slug:
            raise ValueError(f'This repo already declares project {existing}. '
                             'Remove .project-desk.json first if you really mean to move it.')
    changed = []
    if not target.exists() or target.read_text() != declaration:
        target.write_text(declaration)
        changed.append('.project-desk.json')
    if upsert_block(repo / 'AGENTS.md', fetch(f'{base}/files/AGENTS.project-desk.md')):
        changed.append('AGENTS.md')
    if upsert_block(repo / 'CLAUDE.md', fetch(f'{base}/files/CLAUDE.project-desk.md')):
        changed.append('CLAUDE.md')
    if uses_gemini(repo, home):
        if upsert_block(repo / 'GEMINI.md', fetch(f'{base}/files/GEMINI.project-desk.md')):
            changed.append('GEMINI.md')
        if upsert_rule(repo / ANTIGRAVITY_RULE, fetch(f'{base}/files/antigravity-rule.project-desk.md')):
            changed.append(ANTIGRAVITY_RULE)
    return {'repo': str(repo), 'project': slug, 'changed': changed,
            'next': 'Commit the changed files. Then call register_session without a project.',
            'machine_setup_missing': machine_status(desk, home)}

KIT = Path(__file__).resolve().parent / 'kit'
SLUG = re.compile(r'[a-z0-9][a-z0-9-]*')
HOOK_MARK = 'project-desk-kit'


def _kit_url(url):
    parts = urlsplit(str(url))
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or re.search(r"[\s'\"`$\\{}]", str(url))):
        raise ValueError('--url must be a plain http(s) address like https://desk.example.com')
    return str(url).rstrip('/')


def _write_json(path, data):
    text = json.dumps(data, indent=2) + '\n'
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def _load_json(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        raise ValueError(f'{path} is not valid JSON; fix it by hand first')
    if not isinstance(data, dict):
        raise ValueError(f'{path} must hold a JSON object')
    return data


def _kit_command(name):
    """A cloud-only hook command: guard, then the inlined kit script. Its output is shown, never hidden: only
    the final `|| exit 0` stays, so a failure is reported but can never block a session."""
    script = (KIT / name).read_text().strip()
    return ('[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0; command -v python3 >/dev/null 2>&1 || exit 0; '
            f"python3 -c '{script}' || exit 0")


def session_start_command():
    """The cloud-only SessionStart hook: the bounded register (or reuse) and check_in script."""
    return _kit_command('session_start.py')


def pre_tool_command():
    """The cloud-only PreToolUse hook: supplies the session key to desk tool calls from the kit's own binding file."""
    return _kit_command('pre_tool.py')


PRE_TOOL_MATCHER = 'mcp__project-desk__.*'
WAIT_LOOP = '.claude/desk-wait.py'


def init_kit(project, url, repo='.'):
    """Write the repo kit that lets claude.ai/code cloud sessions reach a hosted desk. Never writes a token."""
    if not SLUG.fullmatch(str(project)):
        raise ValueError('--project must be a slug like my-app (lowercase letters, digits, dashes)')
    url = _kit_url(url)
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError(f'{repo} is not a folder')
    pocket_dir = repo / '.project-desk'
    pocket_copy = pocket_dir / 'desk.py'
    if pocket_dir.is_symlink() or pocket_copy.is_symlink():
        raise ValueError('The cloud pocket kit path must not be a symlink')
    declared = repo / '.project-desk.json'
    existing = _load_json(declared).get('project')
    if existing and existing != project:
        raise ValueError(f'This repo already declares project {existing}. '
                         'Remove .project-desk.json first if you really mean to move it.')
    mcp_path, settings_path = repo / '.mcp.json', repo / '.claude/settings.json'
    mcp, settings = _load_json(mcp_path), _load_json(settings_path)
    mcp.setdefault('mcpServers', {})['project-desk'] = {
        'type': 'http', 'url': '${PROJECT_DESK_URL}/mcp',
        'headers': {'Authorization': 'Bearer ${PROJECT_DESK_TOKEN:-}'}}
    command = session_start_command()
    groups = settings.setdefault('hooks', {}).setdefault('SessionStart', [])
    ours = [h for g in groups for h in g.get('hooks', []) if HOOK_MARK in h.get('command', '')]
    if ours:
        for hook in ours:
            hook.update(type='command', command=command, timeout=15)
    else:
        groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 15}]})
    pocket_marker = '.project-desk/desk.py" hook '
    for event in ('SessionStart', 'UserPromptSubmit', 'SessionEnd'):
        command = ('[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0; '
                   '[ -z "$PROJECT_DESK_TOKEN" ] || exit 0; '
                   f'python3 "$CLAUDE_PROJECT_DIR/.project-desk/desk.py" hook {event} || exit 0')
        event_groups = settings['hooks'].setdefault(event, [])
        found = [h for group in event_groups for h in group.get('hooks', [])
                 if pocket_marker in h.get('command', '')]
        if found:
            for hook in found:
                hook.update(type='command', command=command, timeout=15)
        else:
            event_groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 15}]})
    pre_groups = settings['hooks'].setdefault('PreToolUse', [])
    if os.environ.get('PROJECT_DESK_KEY_MODE', '').strip().lower() == 'inject':
        ours = [(g, h) for g in pre_groups for h in g.get('hooks', []) if HOOK_MARK in h.get('command', '')]
        if ours:
            for group, hook in ours:
                group['matcher'] = PRE_TOOL_MATCHER
                hook.update(type='command', command=pre_tool_command(), timeout=10)
        else:
            pre_groups.append({'matcher': PRE_TOOL_MATCHER,
                               'hooks': [{'type': 'command', 'command': pre_tool_command(), 'timeout': 10}]})
    else:
        for group in pre_groups:
            group['hooks'] = [h for h in group.get('hooks', []) if HOOK_MARK not in h.get('command', '')]
        settings['hooks']['PreToolUse'] = [g for g in pre_groups if g.get('hooks')]
        if not settings['hooks']['PreToolUse']:
            del settings['hooks']['PreToolUse']
    meta = {'slug': project, 'name': project, 'rules_path': ''}
    doc = (KIT / 'project-desk-cloud.md').read_text().replace('{url}', url).replace('{project}', project)
    doc = doc.replace('{host}', urlsplit(url).netloc)
    changed = []
    for name, data in (('.project-desk.json', {'project': project, 'name': project, 'desk': url}),
                       ('.mcp.json', mcp), ('.claude/settings.json', settings)):
        if _write_json(repo / name, data):
            changed.append(name)
    for name, block in (('CLAUDE.md', claude_block(meta, url)), ('AGENTS.md', agents_block(meta, url))):
        if upsert_block(repo / name, block):
            changed.append(name)
    target = repo / 'docs/project-desk-cloud.md'
    if not target.exists() or target.read_text() != doc:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(doc)
        changed.append('docs/project-desk-cloud.md')
    loop, script = repo / WAIT_LOOP, (KIT / 'wait_loop.py').read_text()
    if not loop.exists() or loop.read_text() != script:
        loop.parent.mkdir(parents=True, exist_ok=True)
        loop.write_text(script)
        changed.append(WAIT_LOOP)
    source = (Path(__file__).resolve().parent / 'desk_pocket.py').read_bytes()
    if not pocket_copy.exists() or pocket_copy.read_bytes() != source:
        pocket_dir.mkdir(parents=True, exist_ok=True)
        folder = os.open(pocket_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        name = '.desk-' + uuid.uuid4().hex
        try:
            try:
                existing = os.stat('desk.py', dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing and not stat.S_ISREG(existing.st_mode):
                raise ValueError('The cloud pocket kit path must be a regular file, not a symlink')
            child = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=folder)
            with os.fdopen(child, 'wb') as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, 'desk.py', src_dir_fd=folder, dst_dir_fd=folder)
            os.fsync(folder)
        finally:
            try:
                os.unlink(name, dir_fd=folder)
            except FileNotFoundError:
                pass
            os.close(folder)
        changed.append('.project-desk/desk.py')
    return {'repo': str(repo), 'project': project, 'changed': changed,
            'next': f'Commit these files. In the cloud environment, allow {urlsplit(url).netloc} on the Custom '
                    'network, set PROJECT_DESK_URL, and leave PROJECT_DESK_TOKEN unset for invite joining; '
                    'see docs/project-desk-cloud.md for the advanced environment-token route.'}


def install_guard(repo):
    """`desk join` also installs the commit guard; a refusal is reported, never a failed join."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
    try:
        import desk_git
        result = desk_git.install(Path(repo))
    except Exception as error:
        return f'skipped ({error})'
    return 'chained' if result['chained'] else 'installed'


def _masked(text, data):
    """text with every session_key found anywhere in the call's arguments (and its first 8 and last 20 characters,
    which a validation error can echo) replaced by [private]."""
    found, stack = set(), [data]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
            found.update(v for k, v in value.items() if k == 'session_key' and isinstance(v, str) and len(v) >= 8)
        elif isinstance(value, list):
            stack.extend(value)
    for key in sorted(found, key=len, reverse=True):
        for part in (key, key[:20], key[-20:], key[:8]):
            text = text.replace(part, '[private]')
    return text


async def call_tools():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    parser = argparse.ArgumentParser()
    parser.add_argument('tool', nargs='?', default='list')
    parser.add_argument('--json-file', help='Arguments JSON file; use - to read stdin')
    parser.add_argument('--url', default=DEFAULT_DESK + '/mcp')
    args = parser.parse_args()
    data = {}
    if args.json_file:
        if args.json_file == '-':
            data = json.load(sys.stdin)
        else:
            with open(args.json_file) as f:
                data = json.load(f)
    async with create_mcp_http_client(headers={'User-Agent': user_agent('cli')}) as http_client, \
            streamable_http_client(args.url, http_client=http_client) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if args.tool == 'list':
                result = await session.list_tools()
                print(json.dumps([{'name': t.name, 'description': t.description, 'inputSchema': t.inputSchema}
                                  for t in result.tools], indent=2))
            else:
                result = await session.call_tool(args.tool, data)
                if result.isError:
                    print(_masked(' '.join(getattr(c, 'text', '') or '' for c in result.content), data), file=sys.stderr)
                    raise SystemExit(1)
                print(json.dumps(result.structuredContent or json.loads(result.content[0].text), indent=2))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'join' and (
            len(sys.argv) == 2 or not LINK.fullmatch(sys.argv[2].strip())):
        import desk_pocket
        raise SystemExit(desk_pocket.main(sys.argv[1:]))
    if len(sys.argv) > 1 and sys.argv[1] in ('install', 'status', 'leave', 'header', 'hook', 'token'):
        import desk_pocket
        raise SystemExit(desk_pocket.main(sys.argv[1:]))
    if len(sys.argv) > 1 and sys.argv[1] == 'join':
        parser = argparse.ArgumentParser(prog='desk join', description='Connect this repo to a Project Desk project.')
        parser.add_argument('link')
        parser.add_argument('--repo', default='.')
        parser.add_argument('--no-guard', action='store_true', help='Do not install the git commit guard.')
        args = parser.parse_args(sys.argv[2:])
        try:
            result = join(args.link, args.repo)
            result['guard'] = 'skipped (--no-guard)' if args.no_guard else install_guard(result['repo'])
            print(json.dumps(result, indent=2))
        except (ValueError, OSError) as error:
            print(f'desk join: {error}', file=sys.stderr)
            raise SystemExit(1)
        return
    if len(sys.argv) > 2 and sys.argv[1] == 'guard' and sys.argv[2] == 'install':
        parser = argparse.ArgumentParser(prog='desk guard install',
                                         description='Install the git pre-commit guard and post-commit task log.')
        parser.add_argument('--repo', default='.')
        args = parser.parse_args(sys.argv[3:])
        sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
        import desk_git
        try:
            print(json.dumps(desk_git.install(Path(args.repo)), indent=2))
        except (ValueError, OSError) as error:
            print(f'desk guard: {error}', file=sys.stderr)
            raise SystemExit(1)
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'init':
        parser = argparse.ArgumentParser(prog='desk init', description='Write the cloud-session kit for a hosted desk.')
        parser.add_argument('--project', required=True)
        parser.add_argument('--url', required=True)
        parser.add_argument('--repo', default='.')
        args = parser.parse_args(sys.argv[2:])
        try:
            print(json.dumps(init_kit(args.project, args.url, args.repo), indent=2))
        except (ValueError, OSError) as error:
            print(f'desk init: {error}', file=sys.stderr)
            raise SystemExit(1)
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'codex':
        sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
        try:
            import desk_codex
        except ModuleNotFoundError as error:
            if error.name != 'desk_codex':
                raise
            print('desk codex is not installed yet', file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(desk_codex.main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == 'doctor':
        sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
        import desk_doctor
        raise SystemExit(desk_doctor.main(sys.argv[2:]))
    if len(sys.argv) > 1 and '--json-file' not in sys.argv and '--url' not in sys.argv:
        import desk_pocket
        try:
            origin = desk_pocket._pin(None, os.environ)
            project = desk_pocket._active_project(None, origin)
            if desk_pocket._entry(None, origin, project) or desk_pocket._pocket(None, origin, project):
                raise SystemExit(desk_pocket.main(sys.argv[1:]))
        except desk_pocket.Refused as error:
            if str(error) != desk_pocket.NOT_SETUP:
                print(str(error), file=sys.stderr)
                raise SystemExit(1)
        except (OSError, ValueError):
            print('Project Desk could not read its private state.', file=sys.stderr)
            raise SystemExit(1)
    asyncio.run(call_tools())


if __name__ == '__main__':
    main()
