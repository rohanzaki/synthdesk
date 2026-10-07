#!/usr/bin/env python3
"""Project Desk standing wait loop for claude.ai/code cloud sessions. `desk init` writes it to
.claude/desk-wait.py. Run it in the background after every step:

    python3 .claude/desk-wait.py        (Bash run_in_background, timeout 7200000 ms)

It waits on the desk until priority mail for THIS session arrives (a direct message, question, approval decision or
blocker; never a broadcast), prints one line per item and exits 0, so the cloud client wakes the agent with a
background-task notification. The agent reads the mail through the desk, acts, and starts the loop again. Exit 3 at
the deadline (1 h 50 min, under the 2 h cap on a background command; the default 30 min timeout would kill it
early), exit 4 when this folder has no desk session yet. It reads only the inbox digest and wait_for, never a body,
never acknowledges, and never prints the session key. Stdlib only: a kit repo carries no desk code.
"""
import glob
import fcntl
import json
import os
import sys
import time
import urllib.request as u
import ipaddress
from urllib.parse import urlsplit


def unsafe_text(value):
    return any(c.isspace() or ord(c) < 32 or ord(c) in (127, 133, 0x200b, 0xfeff) for c in value)


def unsafe_authority(netloc):
    if "\\" in netloc:
        return True
    authority = netloc.rsplit("@", 1)[-1]
    if authority.startswith("["):
        close = authority.find("]")
        return close >= 0 and authority[close + 1:] == ":"
    return authority.endswith(":")


def loopback_host(host):
    if not host or unsafe_text(host) or "%" in host:
        return False
    if host.lower() in ("localhost", "localhost."):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def bearer_parts(url):
    """The URL parts, or None when it is ambiguous (control characters, userinfo, bad port, query, fragment)."""
    if not isinstance(url, str) or unsafe_text(url):
        return None
    try:
        parts = urlsplit(url)
        parts.port
    except (TypeError, ValueError):
        return None
    if (not parts.hostname or "@" in parts.netloc or unsafe_authority(parts.netloc)
            or parts.username or parts.password or parts.query or parts.fragment):
        return None
    return parts


def http_loopback(url):
    parts = bearer_parts(url)
    return bool(parts and parts.scheme.lower() == "http" and loopback_host(parts.hostname or ""))


def bearer_allowed(url):
    """True when a bearer token may go to this URL: https, or http to true loopback."""
    parts = bearer_parts(url)
    return bool(parts and (parts.scheme.lower() == "https" or (parts.scheme.lower() == "http" and loopback_host(parts.hostname or ""))))

EXIT_MAIL, EXIT_DEADLINE, EXIT_NO_BINDING, EXIT_ALREADY = 0, 3, 4, 5
WAKE_DEADLINE = 6600
WAIT_SECONDS = 50
NO_BINDING_SLEEP = 30
BACKOFF_START, BACKOFF_CAP = 5, 300
FIRED_MAX = 500
LINE_MAX = 200
KIT_DIR = os.path.join(os.path.expanduser("~"), ".project-desk-kit")


def desk_base(url):
    """PROJECT_DESK_URL without a trailing /mcp (some environments set it with /mcp on the end)."""
    url = (url or "").rstrip("/")
    return url[:-len("/mcp")] if url.endswith("/mcp") else url


class FiredStore:
    """Ids this loop already woke the agent for, kept across re-arms in a private file (oldest dropped first)."""
    def __init__(self, path, limit=FIRED_MAX):
        self.path, self.limit = str(path), limit
        try:
            with open(self.path) as f:
                self.ids = [str(ref) for ref in json.load(f)][-limit:]
        except (OSError, ValueError, TypeError):
            self.ids = []

    def __contains__(self, ref):
        return ref in self.ids

    def lock(self):
        """Hold the per-session loop lock (a file beside the store); False when another loop holds it."""
        if getattr(self, "_lock", None) is not None:
            return True
        fd = os.open(str(self.path) + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock = fd
        return True

    def unlock(self):
        if getattr(self, "_lock", None) is not None:
            os.close(self._lock)
            self._lock = None

    def add(self, ref):
        if ref in self.ids:
            return
        self.ids = (self.ids + [ref])[-self.limit:]
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(self.ids, out)
        os.chmod(self.path, 0o600)


def priority_of(item):
    """The desk class as an int: it sends P0..P4 strings (an int is accepted too); None when absent or malformed."""
    value = item.get("priority")
    if isinstance(value, str) and len(value) == 2 and value[0] in "Pp" and value[1] in "01234":
        value = int(value[1])
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 4 else None


def wakes(item, max_priority):
    """Mail addressed to this session; once the desk sends a priority class (P0 blocker .. P4 broadcast), only classes
    up to max_priority. A broadcast never wakes anyone."""
    if item.get("to") != "you" or not item.get("id"):
        return False
    priority = priority_of(item)
    return True if priority is None and "priority" not in item else priority is not None and priority <= max_priority and priority < 4


def label(item):
    priority = priority_of(item)
    kind = item.get("kind") or "message"
    return "P%d %s" % (priority, kind) if priority is not None else kind


def line_for(kind, sender, text, ref):
    text = " ".join(str(text or "").splitlines()[:1])
    head, tail = "DESK: %s from %s: " % (str(kind)[:20], str(sender)[:60]), " (ref %s)" % ref
    room = LINE_MAX - len(head) - len(tail)
    return head + (text if len(text) <= room else text[:max(0, room - 1)] + "\u2026") + tail


def until_mail(call, find, emit, fired, deadline=WAKE_DEADLINE, max_priority=3, clock=time.monotonic, sleep=time.sleep):
    """See _until_mail. One loop per session: a second one exits EXIT_ALREADY without calling the desk, so two
    armed loops never wake the agent twice for the same mail; the lock is released when the loop ends."""
    locked = []
    try:
        return _until_mail(call, find, emit, fired, deadline, max_priority, clock, sleep, locked)
    finally:
        for store in locked:
            store.unlock()


def _until_mail(call, find, emit, fired, deadline=WAKE_DEADLINE, max_priority=3, clock=time.monotonic, sleep=time.sleep, locked=None):
    """The same contract as desk_watch.until_mail: 0 on mail (one line each), 3 at the deadline, 4 with no session."""
    end = clock() + deadline
    found, failures, need_digest, direct = False, 0, True, 0
    while clock() < end:
        binding = find()
        if not binding:
            sleep(max(0, min(NO_BINDING_SLEEP, end - clock())))
            continue
        found, key = True, binding["session_key"]
        store = fired(binding) if callable(fired) else fired
        if locked is not None and store not in locked:
            if not store.lock():
                emit(" DESK: a wait loop is already armed for this session; not starting a second one.")
                return EXIT_ALREADY
            locked.append(store)
        say = lambda line: emit(line.replace(key, "[private]"))
        try:
            digest = call(binding, "check_in", {"session_key": key, "include": ["inbox_digest"]}).get("inbox_digest", []) if need_digest else []
            need_digest = False
            fresh = [item for item in digest if wakes(item, max_priority) and item["id"] not in store]
            if fresh:
                for item in fresh:
                    say(line_for(label(item), item.get("sender_name") or item.get("sender", "?"), item.get("first_line", ""), item["id"]))
                    store.add(item["id"])
                return EXIT_MAIL
            waited = call(binding, "wait_for", {"session_key": key, "timeout": max(1, min(WAIT_SECONDS, int(end - clock())))})
            if waited.get("restarting"):
                retry = waited.get("retry_in", 5)
                sleep(max(0.0, float(retry)) if isinstance(retry, (int, float)) and not isinstance(retry, bool) else 5)
                need_digest = True
                continue
            unread = waited.get("unread_direct") if isinstance(waited.get("unread_direct"), int) else 0
            need_digest = any(c.get("kind") != "decision" for c in waited.get("changed", [])) or unread > direct
            direct = unread
            decisions = [ref for c in waited.get("changed", []) if c.get("kind") == "decision" for ref in c.get("ids", []) if "d:" + ref not in store]
            for ref in decisions:
                say(line_for("decision", "the human", "a decision on your approval request", ref))
                store.add("d:" + ref)
            if decisions:
                return EXIT_MAIL
            failures = 0
        except (OSError, ValueError, KeyError, TypeError) as error:
            need_digest = True
            if failures == 0:
                emit("DESK: wait loop cannot reach the desk, retrying (%s)" % type(error).__name__)
            sleep(max(0, min(BACKOFF_START * 2 ** failures, BACKOFF_CAP, end - clock())))
            failures += 1
    if not found:
        emit("DESK: no Project Desk session found for this folder; start the session first.")
        return EXIT_NO_BINDING
    span = "%d min" % (deadline // 60) if deadline >= 120 else "%d s" % deadline
    emit("DESK: no priority mail in %s; re-arm the wait loop." % span)
    return EXIT_DEADLINE


def find_binding(folder, cwd, url):
    """The kit binding session_start wrote for this folder: same desk, the longest matching folder, then the newest."""
    best = None
    for path in glob.glob(os.path.join(folder, "s-*.json")):
        try:
            info = os.lstat(path)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                continue
            with open(path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            continue
        where = saved.get("cwd") or ""
        if not saved.get("session_key") or not where or desk_base(saved.get("url")) != desk_base(url):
            continue
        if cwd == where or cwd.startswith(where.rstrip(os.sep) + os.sep):
            rank = (len(where), os.path.getmtime(path))
            if best is None or rank > best[0]:
                best = (rank, saved)
    return best[1] if best else None


class Stay(u.HTTPRedirectHandler):
    def redirect_request(self, *args, **kw):
        return None


def make_call(url, token):
    base = desk_base(url)
    opener = u.build_opener(Stay, u.ProxyHandler({})) if http_loopback(base) else u.build_opener(Stay)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "X-Project-Desk-Passive": "1"}
    if token and bearer_allowed(base):
        headers["Authorization"] = "Bearer " + token

    def call(binding, tool, args):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        request = u.Request(base + "/mcp", body.encode(), headers={**headers, "User-Agent": "project-desk/0.1.0 (kit wait)"})
        res = json.load(opener.open(request, timeout=WAIT_SECONDS + 15))["result"]
        if res.get("isError"):
            raise ValueError("desk refused " + tool)
        return res.get("structuredContent") or json.loads(res["content"][0]["text"])
    return call


def main():
    url, token = os.environ.get("PROJECT_DESK_URL", ""), os.environ.get("PROJECT_DESK_TOKEN", "")
    if not url or not token:
        print("DESK: PROJECT_DESK_URL and PROJECT_DESK_TOKEN must be set in the cloud environment.")
        return EXIT_NO_BINDING
    if not bearer_allowed(desk_base(url)):
        print("DESK: refusing to send the token to a non-https desk.")
        return EXIT_NO_BINDING
    deadline = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else WAKE_DEADLINE
    cwd = os.getcwd()
    try:
        return until_mail(make_call(url, token), lambda: find_binding(KIT_DIR, cwd, url), lambda line: print(line, flush=True),
                          lambda binding: FiredStore(os.path.join(KIT_DIR, "fired-%s.json" % binding["session_id"])), deadline)
    except KeyboardInterrupt:
        return EXIT_DEADLINE


if __name__ == "__main__":
    sys.exit(main())
