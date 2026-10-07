"""The one strict rule for sending a bearer token over plain http: only to this machine.

True loopback is the host exactly `localhost` or `localhost.`, or an IP literal that `ipaddress` parses as 127.0.0.0/8,
::1 or an IPv4-mapped loopback. Shorthand forms a resolver accepts (127.1, 0x7f000001, 2130706433), zone ids, whitespace,
userinfo, a query or a fragment all fail. https passes anywhere. A caller that sends a token to a loopback http desk
must also bypass environment proxies (an empty ProxyHandler), so the proxy never sees it.

Copies: the kit scripts (inlined into one `python3 -c '...'`, so no single quote below) cannot import this module
and carry the block below verbatim.
"""

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
