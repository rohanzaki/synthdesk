#!/usr/bin/env python3
"""Remove an unconsumed Codex broker token handoff after 15 seconds.

This separate process outlives the launcher if it is killed before cleanup.
It never reads the handoff contents or receives the token in its environment.
"""
from __future__ import annotations

import os
import re
import stat
import sys
import time
from pathlib import Path

DEADLINE = 15
NAME = re.compile(r'launch-[A-Za-z0-9_-]{3,80}\.json\Z')


def expire(path, delay=DEADLINE):
    time.sleep(delay)
    path = Path(path)
    if not path.is_absolute() or not NAME.fullmatch(path.name):
        return False
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            return False
        try:
            info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except OSError:
            return False
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            return False
        try:
            os.unlink(path.name, dir_fd=directory)
        except OSError:
            return False
    finally:
        os.close(directory)
    print('desk codex: desk connection failed; broker did not start.', file=sys.stderr)
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        return 2
    expire(argv[0])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
