"""Which Project Desk project a directory belongs to.

A repo declares its project in a committed `.project-desk.json` at its root.
Used by the hooks (enrollment hint). Pure
lookups: reads one small JSON file and may run one read-only git command.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

import safe_git

DECLARATION = '.project-desk.json'
DEFAULT_DESK = 'http://127.0.0.1:7331'
MAX_DECLARATION_BYTES = 4096
SLUG = re.compile(r'[a-z0-9][a-z0-9-]*')


@dataclass(frozen=True)
class Resolution:
    project: str
    source: str
    root: str
    name: str = ''
    desk: str = ''


def valid_slug(value):
    return isinstance(value, str) and len(value) <= 100 and bool(SLUG.fullmatch(value))


def display_name(slug):
    return 'Unsorted' if slug == 'default' else ' '.join(part.capitalize() for part in slug.split('-'))


def read_declaration(directory):
    """The parsed declaration in `directory`, or None when absent or invalid."""
    path = Path(directory) / DECLARATION
    try:
        if not path.is_file() or path.stat().st_size > MAX_DECLARATION_BYTES:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not valid_slug(data.get('project')):
        return None
    return data


def git_main_worktree(path, timeout=2):
    """The main worktree of the git repo containing `path`, or None."""
    result = safe_git.run(path, 'rev-parse', '--path-format=absolute', '--git-common-dir', timeout=timeout)
    if result is None or result.returncode:
        return None
    common = Path(result.stdout.strip())
    return common.parent if common.name == '.git' else None


def _within(path, root):
    return path == root or root in path.parents


def _from(data, source, root):
    return Resolution(data['project'], source, str(root), str(data.get('name') or ''), str(data.get('desk') or ''))


def resolve_project(path, registry=None, timeout=2):
    """Resolve the project for `path`. `registry` maps slug -> list of repo roots."""
    try:
        start = Path(path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    for directory in (start, *start.parents):
        data = read_declaration(directory)
        if data:
            return _from(data, 'file', directory)
        if (directory / '.git').exists():
            break
    main = git_main_worktree(start, timeout)
    if main is not None:
        data = read_declaration(main)
        if data:
            return _from(data, 'git-main', main)
    for slug, roots in (registry or {}).items():
        for root in roots:
            try:
                root_path = Path(root).resolve()
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if _within(start, root_path) or (main is not None and _within(main, root_path)):
                return Resolution(slug, 'registry', str(root_path))
    return None
