#project-desk-kit pre-tool: inlined by desk init, so no single quotes anywhere in this file
import glob, json, os, sys
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


def desk_origin(url):
    """The verified credential origin, with a default port made explicit."""
    parts = bearer_parts(url)
    if not parts or not bearer_allowed(url):
        return ""
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
    if not host or not port:
        return ""
    return "%s://%s:%s" % (scheme, "[" + host + "]" if ":" in host else host, port)


try:
    if os.environ.get("PROJECT_DESK_KEY_MODE", "").strip().lower() != "inject":
        sys.exit(0)
    event = json.load(sys.stdin)
    tool = str(event.get("tool_name", ""))
    if not tool.startswith("mcp__project-desk__"):
        sys.exit(0)
    url = os.environ.get("PROJECT_DESK_URL", "").rstrip("/")
    destination_origin = desk_origin(url)
    if not destination_origin:
        sys.exit(0)
    cwd = os.getcwd()
    mine = None
    for path in glob.glob(os.path.join(os.path.expanduser("~"), ".project-desk-kit", "s-*.json")):
        try:
            saved = json.load(open(path))
            where = saved.get("cwd") or ""
            if saved.get("session_key") and where and desk_origin(saved.get("url")) == destination_origin and (cwd == where or cwd.startswith(where.rstrip(os.sep) + os.sep)):
                rank = (len(where), os.path.getmtime(path))
                if mine is None or rank > mine[0]:
                    mine = (rank, saved)
        except Exception:
            pass
    if mine is None:
        sys.exit(0)
    saved = mine[1]
    given = event.get("tool_input")
    given = given if isinstance(given, dict) else {}
    inner = str(given.get("tool", "")).lower().replace("-", "_") if tool.endswith("__desk") else ""
    if tool.endswith("__register_session") or inner == "register_session":
        out = {"permissionDecision": "deny", "permissionDecisionReason": "You are already registered as %s. Desk tools are pre-bound: call them without session_key." % saved.get("session_id")}
    else:
        out = {"permissionDecision": "allow", "updatedInput": dict(given, session_key=saved["session_key"])}
    print(json.dumps({"hookSpecificOutput": dict(out, hookEventName="PreToolUse")}))
except Exception:
    pass
