"""A fixed character budget for what a hook injects into an agent's context.

What a hook says stays in the conversation and is read again on every later request until the
context is compacted, so the hook says only what is new and what the agent must act on, and never
more than the budget. No dependencies beyond the standard library: hooks run as a bare script.

Rules:
* nothing new, nothing sent: an empty allocation is the empty string;
* a line already shown (its content hash is in the seen-set) is not shown again until it changes;
* urgent lines go first however small the budget; at most URGENT_FULL of them in full, the next
  URGENT_SHORT as their short form, any beyond that counted (they stay unread on the desk);
* the others are ranked by relevance; each is full if it fits, else short if it fits, else counted
  in a final "+N more: refs (desk_open to read)" line;
* the output is a pure function of its input, so the same board produces the same bytes.
"""
import hashlib
import re
from dataclasses import dataclass

DEFAULT_BUDGET = 1500
URGENT_FULL = 3
URGENT_SHORT = 8
SEEN_CAP = 500
MORE_REFS = 12

SCORE_TO_YOU, SCORE_HUMAN, SCORE_YOUR_PATH, SCORE_YOUR_TASK, SCORE_PROJECT = 100, 90, 60, 50, 10
PRIORITY_SCORES = {0: 120, 1: 110, 2: 100, 3: 80}


@dataclass(frozen=True)
class Line:
    text: str
    short: str
    ref: str
    urgent: bool
    score: float


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


POINTER = re.compile(r'… \[\+\d+ chars: read_messages\(message_ids=\["[^"\n]*"\]\)\]\Z')


def shorten(text, limit=160):
    found = POINTER.search(text)
    tail = found.group(0) if found else ''
    first = next((part for part in text[:found.start() if found else len(text)].splitlines() if part.strip()), '')
    if len(first) + len(tail) <= limit:
        return first + tail
    return first[:max(limit - len(tail), 20)] + tail if tail else first[:limit - 1] + '…'


def allocate_detailed(lines, budget, seen):
    """(output, shown): the injected text and every line the agent was told about, whole, short or by
    ref in the "+N more" line. All of them go in the seen-set: what was offered by ref is not offered
    again next turn (a backlog must not drain into the conversation one budget at a time)."""
    fresh = [(i, line) for i, line in enumerate(lines) if digest(line.text) not in seen]
    urgent = sorted((p for p in fresh if p[1].urgent), key=lambda p: (-p[1].score, p[0]))
    rest = sorted((p for p in fresh if not p[1].urgent), key=lambda p: (-p[1].score, p[0]))
    out, shown, used, dropped = [], [], 0, []
    for n, (_, line) in enumerate(urgent):
        if n >= URGENT_FULL + URGENT_SHORT:
            dropped.append(line)
            continue
        text = line.text if n < URGENT_FULL else line.short
        out.append(text); shown.append(line); used += len(text) + 1
    for _, line in rest:
        for text in (line.text, line.short):
            if used + len(text) + 1 <= budget:
                out.append(text); shown.append(line); used += len(text) + 1
                break
        else:
            dropped.append(line)
    if dropped:
        refs = ' '.join(line.ref for line in dropped[:MORE_REFS] if line.ref)
        more = f'+{len(dropped)} more' + (f': {refs}' if refs else '') \
               + (' …' if len(dropped) > MORE_REFS else '') + ' (desk_open to read)'
        shown.extend(dropped)
        if digest(more) not in seen:
            out.append(more); shown.append(Line(more, more, '', False, 0))
    return '\n'.join(out), shown


def allocate(lines, budget, seen):
    """Urgent lines first (full text, even past budget, at most 3 of them full, then short).
    Then by score: full if it fits, else short if it fits, else counted.
    Lines whose sha256(text)[:16] is in `seen` are dropped. If anything was dropped for budget,
    the last line is '+N more: ref1 ref2 … (desk_open to read)'. Empty input -> ''."""
    return allocate_detailed(lines, budget, seen)[0]


def remember(seen_list, shown):
    """The binding's seen-set after `shown`: FIFO, at most SEEN_CAP hashes, no duplicates."""
    out = [h for h in seen_list if h not in {digest(line.text) for line in shown}]
    out.extend(digest(line.text) for line in shown)
    return out[-SEEN_CAP:]
