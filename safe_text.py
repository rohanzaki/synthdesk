"""Sanitisers for text that other agents or humans wrote and that reaches a hook line or prompt."""
import json
import re

_SPACE = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029]')
_INVISIBLE = re.compile('[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069'
                        '\ufe00-\ufe0f\ufeff\u115f\u1160\u3164\uffa0'
                        '\U000e0000-\U000e007f\U000e0100-\U000e01ef]')


def line(value, limit=360):
    """One safe line: control and separator characters become a space, invisible ones go, capped at limit."""
    return _INVISIBLE.sub('', _SPACE.sub(' ', str(value)))[:limit]


def quoted(value, limit):
    return json.dumps(line(value, limit), ensure_ascii=False)
