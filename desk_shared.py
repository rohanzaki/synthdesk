"""Helpers for resource paths and for who counts as the human.

Standard library only, no database, no state."""
import os
import re
import unicodedata


def text(value, label, limit=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{label} must contain 1–{limit} characters')
    return value.strip()


def scopes(values):
    if not isinstance(values, list) or not values or len(values) > 100:
        raise ValueError('Supply 1–100 relative file/directory paths or service:name resources')
    result = []
    for value in values:
        value = text(value, 'resource', 500).replace('\\', '/')
        if value.startswith('service:'):
            if not re.fullmatch(r'service:[a-zA-Z0-9_.:-]+', value):
                raise ValueError('Invalid service resource')
        else:
            if ':' in value and not value.startswith('/') and not re.match(r'^[A-Za-z]:/', value):
                raise ValueError('Unsupported resource prefix; use repo-relative paths or service:name (local sessions only)')
            if value.startswith('/') or ':' in value or any(c in value for c in '*?'):
                raise ValueError('Use literal repo-relative paths, not absolute paths or globs')
            parts = value.split('/')
            if '..' in parts:
                raise ValueError('Parent traversal is not a valid resource')
            value = '/'.join(p for p in parts if p and p != '.') or '.'
        result.append(value)
    return sorted(set(result))


def _fold(path):
    """How two spellings of one file compare: composed Unicode, case-folded (a case-sensitive checkout's Makefile and makefile conflict)."""
    return unicodedata.normalize('NFC', path).casefold()


def overlaps(a, b):
    if a.startswith('service:') or b.startswith('service:'):
        return a == b
    a, b = _fold(a), _fold(b)
    return a == '.' or b == '.' or a == b or a.startswith(b + '/') or b.startswith(a + '/')


HUMAN = 'human'
HUMAN_ALIASES = frozenset({'human'})
HUMAN_IDS = ('human',)


def configure_human():
    """(Re)read the human identity from the environment. Runs at import; tests call it after changing the env."""
    global HUMAN, HUMAN_ALIASES, HUMAN_IDS
    HUMAN = os.environ.get('PROJECT_DESK_HUMAN_ID', '').strip() or 'human'
    extra = {a.strip() for a in os.environ.get('PROJECT_DESK_HUMAN_ALIASES', '').split(',') if a.strip()}
    HUMAN_ALIASES = frozenset({'human', HUMAN} | extra)
    HUMAN_IDS = (HUMAN, *sorted(HUMAN_ALIASES - {HUMAN}))


def is_human(actor):
    return actor in HUMAN_ALIASES


configure_human()
