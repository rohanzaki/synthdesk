"""Running git on a path an agent chose.

The path comes from an agent's own registration, so the repo's config must never be able to run a command in
this process (core.fsmonitor, a hooks path, a partial clone's lazy fetch or its uploadpack, a transport)."""
import os
import subprocess


def env():
    """A minimal environment: nothing inherited (no GIT_DIR, no GIT_CONFIG_*), no lazy fetching of missing
    objects, no transports at all, no prompts."""
    keep = {k: os.environ[k] for k in ('PATH', 'HOME', 'XDG_CONFIG_HOME') if k in os.environ}
    return {**keep, 'LC_ALL': 'C', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0',
            'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': 'none'}


def argv(root, *args):
    return ['git', '-c', 'core.fsmonitor=', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args]


def run(root, *args, timeout=2):
    """The CompletedProcess, or None when git is missing, slow or the path is unusable."""
    if not os.path.isabs(str(root)):
        return None
    try:
        return subprocess.run(argv(root, *args), capture_output=True, text=True, timeout=timeout,
                              env=env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
